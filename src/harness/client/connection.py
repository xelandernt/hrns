"""Connect, disconnect, transport loss, and connection-state publication."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from acp import RequestError
from acp.schema import Implementation

from .constants import CLIENT_NAME, CLIENT_TITLE, CLIENT_VERSION, PROTOCOL_VERSION
from .errors import ConnectionLost, HarnessClientError, ProtocolError, TransportError
from .events import ConnectionEvent
from .extension import HarnessExtension, parse_advertisement
from .transport import AgentTransport, ConnectionBundle

if TYPE_CHECKING:
    from .client import HarnessAcpClient


class ConnectionLifecycle:
    """Owns the transport and ACP connection for one client."""

    def __init__(
        self,
        client: HarnessAcpClient,
        transport_factory: Callable[[], AgentTransport] | AgentTransport,
        connect_timeout: float,
    ) -> None:
        self.client = client
        if callable(transport_factory):
            self._transport_factory = transport_factory
        else:
            # Convenience for one-shot callers; a factory is required for reconnects.
            single = transport_factory
            self._transport_factory = lambda: single
        self._transport: AgentTransport | None = None
        self._bundle: ConnectionBundle | None = None
        self._closing = False
        self.state = "idle"
        self.connect_timeout = connect_timeout

    @property
    def bundle(self) -> ConnectionBundle:
        if self._bundle is None:
            raise ConnectionLost("The client is not connected.")
        return self._bundle

    @property
    def connected(self) -> bool:
        return self._bundle is not None and not self._closing and self.state == "ready"

    async def connect(self) -> Any:
        client = self.client
        if self._bundle is not None:
            if self.state != "lost":
                return client.initialized
            # The transport reported loss but the old bundle/transport still own
            # cleanup. Drop them before opening a fresh connection.
            stale, self._bundle = self._bundle, None
            await self._abort(stale)
        self._publish_connection("connecting", "Connecting to agent…")
        self._closing = False
        transport = self._transport_factory()
        self._transport = transport
        try:
            bundle = await asyncio.wait_for(
                transport.open(client, on_loss=self._on_transport_loss),
                timeout=self.connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            self._publish_connection("error", f"Timed out connecting to {transport.describe()}")
            raise TransportError(f"Timed out connecting to {transport.describe()}") from exc
        except HarnessClientError as exc:
            self._publish_connection("error", exc.message)
            raise
        except Exception as exc:  # unexpected launch errors stay actionable
            self._publish_connection("error", str(exc))
            raise TransportError(f"Cannot connect to {transport.describe()}: {exc}") from exc

        self._bundle = bundle
        try:
            response = await asyncio.wait_for(
                bundle.conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=client.capabilities,
                    client_info=Implementation(
                        name=CLIENT_NAME, title=CLIENT_TITLE, version=CLIENT_VERSION
                    ),
                    **client._initialize_meta(),
                ),
                timeout=self.connect_timeout,
            )
        except RequestError as exc:
            await self._abort(bundle)
            self._publish_connection("error", f"Agent rejected initialize: {exc}")
            raise ProtocolError(f"The agent rejected ACP initialize: {exc}") from exc
        except (ConnectionError, asyncio.IncompleteReadError) as exc:
            tail = await self._bundle_stderr_tail()
            await self._abort(bundle)
            detail = f"Agent closed the connection during initialize: {exc}"
            if tail:
                detail += f"\n{tail}"
            self._publish_connection("error", detail)
            raise TransportError(detail) from exc
        except BaseException:
            await self._abort(bundle)
            raise

        client.initialized = response
        if getattr(response, "protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            negotiated = response.protocol_version
            await self._abort(bundle)
            raise ProtocolError(
                f"The agent negotiated ACP protocol version {negotiated}; this client supports "
                f"version {PROTOCOL_VERSION}."
            )
        info = getattr(response, "agent_info", None)
        if info is not None:
            client.agent_name = info.name or CLIENT_NAME
            client.agent_title = info.title or client.agent_name
            client.agent_version = info.version
        client.auth_methods = list(getattr(response, "auth_methods", []) or [])
        client.extension = (
            parse_advertisement(getattr(response, "field_meta", None))
            if client.advertise_extension
            else HarnessExtension()
        )
        if client.auth_method:
            advertised = {str(getattr(method, "id", "")) for method in client.auth_methods}
            if client.auth_method not in advertised:
                await self._abort(bundle)
                raise HarnessClientError(
                    f"The agent does not advertise auth method {client.auth_method!r}; "
                    f"available methods: {', '.join(sorted(advertised)) or '(none)'}."
                )
            try:
                await client.authenticate(client.auth_method)
            except BaseException:
                await self._abort(bundle)
                raise
        self._publish_connection(
            "ready",
            f"{client.agent_title} {client.agent_version}".strip(),
        )
        return response

    async def disconnect(self) -> None:
        client = self.client
        self._closing = True
        bundle, self._bundle = self._bundle, None
        transport, self._transport = self._transport, None
        client.initialized = None
        if bundle is not None:
            await bundle.close()
        if transport is not None:
            await transport.close()
        self.state = "closed"
        self._closing = False

    async def _abort(self, bundle: ConnectionBundle) -> None:
        await bundle.close()
        if self._bundle is bundle:
            self._bundle = None
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()

    async def _bundle_stderr_tail(self) -> str:
        transport = self._transport
        getter = getattr(transport, "_stderr_diagnostic", None)
        if callable(getter):
            return str(getter())
        return ""

    def _on_transport_loss(self, detail: str, exception: BaseException | None) -> None:
        if self._closing:
            return
        self.state = "lost"
        self.client.bus.publish(ConnectionEvent(state="lost", detail=detail, exception=exception))

    def _publish_connection(self, state: str, detail: str) -> None:
        self.state = state
        location = {
            "kind": "local"
            if self._transport and "stdio" in type(self._transport).__name__.lower()
            else "remote"
        }
        self.client.bus.publish(
            ConnectionEvent(
                state=state,
                detail=detail,
                location=location,
                agent_name=self.client.agent_title,
            )
        )
