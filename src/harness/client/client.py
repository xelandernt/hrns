"""The ACP client core: one connection, sessions, extension calls, events.

The official ACP SDK performs dispatch and type handling. This module owns
session lifecycle, event normalization, extension negotiation, and the
interaction policy boundary. No runtime or Pydantic AI imports live here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from acp import RequestError
from acp.client.connection import ClientSideConnection
from acp.schema import (
    AcceptElicitationResponse,
    AllowedOutcome,
    BooleanConfigOptionCapabilities,
    CancelElicitationResponse,
    ClientCapabilities,
    ClientSessionCapabilities,
    DeclineElicitationResponse,
    DeniedOutcome,
    ElicitationCapabilities,
    FileSystemCapabilities,
    Implementation,
    PermissionOption,
    PlanCapabilities,
    RequestPermissionResponse,
    SessionConfigOptionsCapabilities,
    TextContentBlock,
    ToolCallUpdate,
)

from harness.protocol import (
    CommandCatalogChanged,
    CommandInvokeResponse,
    ConfigurationSnapshot,
    PresentationUpdated,
    ToolPresentationSnapshot,
)

from .bus import EventBus
from .constants import (
    CLIENT_NAME,
    CLIENT_TITLE,
    CLIENT_VERSION,
    CONNECT_TIMEOUT,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
)
from .errors import (
    ConnectionLost,
    ExtensionUnavailable,
    HarnessClientError,
    InteractionRejected,
    InteractionRequired,
    ProtocolError,
    TransportError,
)
from .events import (
    ConfigOptionsUpdated,
    ConfigurationChanged,
    ConnectionEvent,
    PluginCommandsListed,
    SessionsListed,
    ToolPresentationUpdated,
    TurnCompleted,
    TurnStarted,
)
from .extension import (
    CapabilityListResult,
    CommandListResult,
    HarnessExtension,
    PluginListResult,
    advertisement_meta,
    parse_advertisement,
    parse_configuration_snapshot,
    strip_prefix,
    validate_changes,
)
from .extensions_api import ExtensionsApi
from .interactions import (
    AskUserResponse,
    InteractionPolicy,
    RequireInteractions,
    ask_user_payload,
    parse_ask_user_request,
)
from .payloads import absolute_path, elicitation_record, normalize_permission
from .session import SessionProxy
from .transport import AgentTransport, ConnectionBundle
from .updates import dispatch_update

__all__ = ["HarnessAcpClient", "DEFAULT_INTERACTION_POLICY"]

DEFAULT_INTERACTION_POLICY: InteractionPolicy = RequireInteractions()

#: Text content maximum when the caller has not pre-validated an attachment.
_TOTAL_PROMPT_BYTES = MAX_MESSAGE_BYTES


class HarnessAcpClient:
    """A single ACP connection to one agent.

    ``transport_factory`` is re-invoked for every (re)connect, so transports
    may be stateful (a process launch or a WebSocket).
    """

    def __init__(
        self,
        transport_factory: Callable[[], AgentTransport] | AgentTransport,
        *,
        policy: InteractionPolicy | None = None,
        advertise_extension: bool = True,
        session_new_meta: dict[str, Any] | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        auth_method: str = "",
    ) -> None:
        if callable(transport_factory):
            self._transport_factory = transport_factory
        else:
            # Convenience for one-shot callers; a factory is required for reconnects.
            single = transport_factory
            self._transport_factory = lambda: single
        self._transport: AgentTransport | None = None
        self._bundle: ConnectionBundle | None = None
        self.policy: InteractionPolicy = policy or DEFAULT_INTERACTION_POLICY
        self.advertise_extension = advertise_extension
        self.session_new_meta = dict(session_new_meta or {})
        self.connect_timeout = connect_timeout
        self.auth_method = auth_method.strip()

        self.bus = EventBus()
        self.initialized: Any = None
        self.extension = HarnessExtension()
        self.capabilities: ClientCapabilities = self._build_capabilities()
        self.agent_name = CLIENT_NAME
        self.agent_version = CLIENT_VERSION
        self.agent_title = CLIENT_NAME
        self.auth_methods: list[Any] = []
        self.connection_state = "idle"

        self._inflight_updates = 0
        self.extensions = ExtensionsApi(
            request=self._ext_request,
            check_local_add=self._check_local_add,
        )
        self._closing = False
        self._stderr_tail: list[str] = []
        self._pending_interaction_error: HarnessClientError | None = None

    # ------------------------------------------------------------------ setup

    def _build_capabilities(self) -> ClientCapabilities:
        return ClientCapabilities(
            fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
            terminal=False,
            session=ClientSessionCapabilities(
                config_options=SessionConfigOptionsCapabilities(
                    boolean=BooleanConfigOptionCapabilities(),
                )
            ),
            plan=PlanCapabilities(),
            elicitation=ElicitationCapabilities(form={}, url={}),
        )

    def _initialize_meta(self) -> dict[str, Any]:
        meta = advertisement_meta(advertise=self.advertise_extension)
        return meta

    @property
    def bundle(self) -> ConnectionBundle:
        if self._bundle is None:
            raise ConnectionLost("The client is not connected.")
        return self._bundle

    @property
    def connection(self) -> ClientSideConnection:
        return self.bundle.conn

    @property
    def connected(self) -> bool:
        return self._bundle is not None and not self._closing and self.connection_state == "ready"

    # --------------------------------------------------------------- lifecycle

    async def connect(self) -> Any:
        if self._bundle is not None:
            if self.connection_state != "lost":
                return self.initialized
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
                transport.open(self, on_loss=self._on_transport_loss),
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
                    client_capabilities=self.capabilities,
                    client_info=Implementation(
                        name=CLIENT_NAME, title=CLIENT_TITLE, version=CLIENT_VERSION
                    ),
                    **self._initialize_meta(),
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

        self.initialized = response
        if getattr(response, "protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            negotiated = response.protocol_version
            await self._abort(bundle)
            raise ProtocolError(
                f"The agent negotiated ACP protocol version {negotiated}; this client supports "
                f"version {PROTOCOL_VERSION}."
            )
        info = getattr(response, "agent_info", None)
        if info is not None:
            self.agent_name = info.name or CLIENT_NAME
            self.agent_title = info.title or self.agent_name
            self.agent_version = info.version or CLIENT_VERSION
        self.auth_methods = list(getattr(response, "auth_methods", []) or [])
        self.extension = (
            parse_advertisement(getattr(response, "field_meta", None))
            if self.advertise_extension
            else HarnessExtension()
        )
        if self.auth_method:
            advertised = {str(getattr(method, "id", "")) for method in self.auth_methods}
            if self.auth_method not in advertised:
                await self._abort(bundle)
                raise HarnessClientError(
                    f"The agent does not advertise auth method {self.auth_method!r}; "
                    f"available methods: {', '.join(sorted(advertised)) or '(none)'}."
                )
            try:
                await self.authenticate(self.auth_method)
            except BaseException:
                await self._abort(bundle)
                raise
        self.connection_state = "ready"
        self._publish_connection(
            "ready",
            f"{self.agent_title} {self.agent_version}",
        )
        return response

    async def disconnect(self) -> None:
        self._closing = True
        bundle, self._bundle = self._bundle, None
        transport, self._transport = self._transport, None
        self.initialized = None
        if bundle is not None:
            await bundle.close()
        if transport is not None:
            await transport.close()
        self.connection_state = "closed"
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
        self.connection_state = "lost"
        self.bus.publish(ConnectionEvent(state="lost", detail=detail, exception=exception))

    def _publish_connection(self, state: str, detail: str) -> None:
        self.connection_state = state
        location = {
            "kind": "local"
            if self._transport and "stdio" in type(self._transport).__name__.lower()
            else "remote"
        }
        self.bus.publish(
            ConnectionEvent(
                state=state,
                detail=detail,
                location=location,
                agent_name=self.agent_title,
            )
        )

    def has_session_capability(self, name: str) -> bool:
        """Ask whether the agent advertised a session lifecycle feature."""
        if self.initialized is None:
            return False
        caps = getattr(self.initialized, "agent_capabilities", None)
        sessions = getattr(caps, "session_capabilities", None)
        return bool(getattr(sessions, name, None))

    @property
    def supports_load(self) -> bool:
        if self.initialized is None:
            return False
        caps = getattr(self.initialized, "agent_capabilities", None)
        return bool(getattr(caps, "load_session", False))

    def _check_additional_directories(self, directories: list[str] | None) -> None:
        if not directories:
            return
        for directory in directories:
            absolute_path(directory, label="additional directory")
        if not self.has_session_capability("additional_directories"):
            raise ProtocolError(
                "This agent does not advertise additionalDirectories support; "
                "start the session inside its workspace instead."
            )

    def _check_local_add(self, changes: list[Any]) -> None:
        if not any(getattr(change, "type", None) == "add" for change in changes):
            return
        if not self.extension.local_binding_creation:
            raise HarnessClientError(
                "The host did not advertise local binding creation; 'add' changes are "
                "unavailable. Enable an existing offered binding instead."
            )

    # ---------------------------------------------------------------- sessions

    async def new_session(
        self,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
        initial_configuration: list[dict[str, Any]] | None = None,
    ) -> SessionProxy:
        absolute_path(cwd, label="working directory")
        self._check_additional_directories(additional_directories)
        meta: dict[str, Any] = {}
        if initial_configuration:
            try:
                validated = validate_changes(initial_configuration)
            except ValueError as exc:
                raise HarnessClientError(f"Invalid initial configuration: {exc}") from exc
            self._check_local_add(validated)
            meta = {
                "harness": {
                    "initialConfiguration": [
                        change.model_dump(mode="json", by_alias=True, exclude_none=True)
                        for change in validated
                    ]
                }
            }
        if self.session_new_meta:
            meta = _deep_merge(meta, self.session_new_meta)
        try:
            response = await self.connection.new_session(
                cwd=cwd,
                additional_directories=additional_directories,
                mcp_servers=[],
                **(meta or {}),
            )
        except RequestError as exc:
            raise HarnessClientError(self._describe_rpc_error("session/new", exc)) from exc
        session = SessionProxy(self, response.session_id, cwd)
        session.apply_state(response.modes, response.config_options)
        return session

    async def load_session(
        self,
        session_id: str,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
        replay: bool = True,
    ) -> SessionProxy:
        absolute_path(cwd, label="working directory")
        self._check_additional_directories(additional_directories)
        if not self.supports_load:
            raise ProtocolError(
                "This agent does not advertise session/load; reopening the conversation is unavailable."
            )
        if not replay:
            raise ProtocolError("session/load always replays history in ACP v1.")
        try:
            response = await self.connection.load_session(
                cwd=cwd,
                session_id=session_id,
                mcp_servers=[],
                additional_directories=additional_directories,
            )
        except RequestError as exc:
            raise HarnessClientError(self._describe_rpc_error("session/load", exc)) from exc
        session = SessionProxy(self, session_id, cwd)
        if response is not None:
            session.apply_state(response.modes, response.config_options)
        return session

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
    ) -> SessionProxy:
        absolute_path(cwd, label="working directory")
        self._check_additional_directories(additional_directories)
        if not self.has_session_capability("resume"):
            raise ProtocolError("This agent does not advertise session/resume; use session/load.")
        try:
            response = await self.connection.resume_session(
                session_id=session_id,
                cwd=cwd,
                mcp_servers=[],
                additional_directories=additional_directories,
            )
        except RequestError as exc:
            raise HarnessClientError(self._describe_rpc_error("session/resume", exc)) from exc
        session = SessionProxy(self, session_id, cwd)
        session.apply_state(response.modes, response.config_options)
        return session

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None
    ) -> SessionsListed:
        if cwd is not None:
            absolute_path(cwd, label="working directory filter")
        if not self.has_session_capability("list"):
            raise ProtocolError("This agent does not advertise session/list.")
        try:
            response = await self.connection.list_sessions(cwd=cwd, cursor=cursor)
        except RequestError as exc:
            raise HarnessClientError(self._describe_rpc_error("session/list", exc)) from exc
        sessions = tuple(
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in response.sessions
        )
        result = SessionsListed(sessions=sessions, next_cursor=response.next_cursor)
        self.bus.publish(result)
        return result

    async def close_session(self, session_id: str) -> None:
        if not self.has_session_capability("close"):
            raise ProtocolError(
                "This agent does not advertise session/close; use session/cancel for "
                "portable turn cleanup."
            )
        try:
            await self.connection.close_session(session_id=session_id)
        except RequestError as exc:
            raise HarnessClientError(self._describe_rpc_error("session/close", exc)) from exc

    async def delete_session(self, session_id: str) -> None:
        if not self.has_session_capability("delete"):
            raise ProtocolError("This agent does not advertise session/delete.")
        # ACP SDK 0.11.1 exposes the request model but not a convenience method.
        try:
            result = await self._raw_request("session/delete", {"sessionId": session_id})
        except RequestError as exc:
            raise HarnessClientError(self._describe_rpc_error("session/delete", exc)) from exc
        if result is not None and not isinstance(result, dict):
            raise ProtocolError("session/delete returned an unexpected response")

    async def authenticate(self, method_id: str) -> Any:
        try:
            return await self.connection.authenticate(method_id=method_id)
        except RequestError as exc:
            raise HarnessClientError(self._describe_rpc_error("authenticate", exc)) from exc

    # ------------------------------------------------------------- turn control

    def _check_prompt_capabilities(self, blocks: list[Any]) -> None:
        """Never send a content kind the agent did not advertise."""
        capabilities = getattr(
            getattr(self.initialized, "agent_capabilities", None),
            "prompt_capabilities",
            None,
        )
        allowed = {
            "image": bool(getattr(capabilities, "image", False)),
            "audio": bool(getattr(capabilities, "audio", False)),
            "resource": bool(getattr(capabilities, "embedded_context", False)),
        }
        for block in blocks:
            kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if kind in allowed and not allowed[kind]:
                label = "embedded context" if kind == "resource" else kind
                raise ProtocolError(
                    f"The agent did not advertise {label} prompt support; "
                    "attach text in the prompt instead."
                )

    async def prompt(
        self,
        session_id: str,
        prompt: str | list[Any],
        *,
        display_text: str | None = None,
    ) -> Any:
        self._pending_interaction_error = None
        blocks = (
            prompt if isinstance(prompt, list) else [TextContentBlock(type="text", text=prompt)]
        )
        self._check_prompt_capabilities(blocks)
        payloads: list[Any] = []
        for block in blocks:
            try:
                payloads.append(block.model_dump(mode="json", by_alias=True, exclude_none=True))
            except AttributeError:
                payloads.append(block)
        # Match the SDK's wire serialization (ASCII-escaped JSON, compact
        # separators) and include the JSON-RPC envelope so multi-byte text and
        # escaped characters cannot slip past the frame limit.
        envelope = {
            "jsonrpc": "2.0",
            "id": 2**63 - 1,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": payloads},
        }
        total = len(
            json.dumps(envelope, ensure_ascii=True, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        )
        if total > _TOTAL_PROMPT_BYTES:
            raise TransportError(
                f"The prompt payload is {total:,} bytes; the transport accepts at most "
                f"{MAX_MESSAGE_BYTES:,} bytes. Attach a smaller file."
            )
        hint = (
            display_text
            if display_text is not None
            else (prompt if isinstance(prompt, str) else "")
        )
        self.bus.publish(TurnStarted(session_id=session_id, prompt_text=hint))
        try:
            response = await self.connection.prompt(session_id=session_id, prompt=blocks)
        except RequestError as exc:
            await self.drain_events()
            detail = self._describe_rpc_error("session/prompt", exc)
            self.bus.publish(
                TurnCompleted(session_id=session_id, stop_reason="error", detail=detail)
            )
            raise HarnessClientError(detail) from exc
        except asyncio.CancelledError:
            self.bus.publish(TurnCompleted(session_id=session_id, stop_reason="cancelled"))
            raise
        except (ConnectionError, asyncio.IncompleteReadError) as exc:
            await self.drain_events()
            detail = f"The connection was lost during the turn: {exc}"
            self.bus.publish(
                TurnCompleted(session_id=session_id, stop_reason="connection_lost", detail=detail)
            )
            raise ConnectionLost(detail) from exc
        await self.drain_events()
        if self._pending_interaction_error is not None:
            interaction = self._pending_interaction_error
            self._pending_interaction_error = None
            self.bus.publish(
                TurnCompleted(
                    session_id=session_id,
                    stop_reason="interaction_required",
                    detail=interaction.message,
                )
            )
            raise interaction
        usage = (
            response.usage.model_dump(mode="json", by_alias=True, exclude_none=True)
            if response.usage
            else None
        )
        self.bus.publish(
            TurnCompleted(session_id=session_id, stop_reason=response.stop_reason, usage=usage)
        )
        return response

    async def cancel(self, session_id: str) -> None:
        try:
            await self.connection.cancel(session_id=session_id)
        except Exception as exc:  # cancellation is best-effort at the wire level
            raise HarnessClientError(f"Cancellation could not be delivered: {exc}") from exc

    async def set_mode(self, session_id: str, mode_id: str) -> None:
        try:
            await self.connection.set_session_mode(session_id=session_id, mode_id=mode_id)
        except RequestError as exc:
            raise HarnessClientError(self._describe_rpc_error("session/set_mode", exc)) from exc

    async def set_config_option(
        self, session_id: str, config_id: str, value: str | bool
    ) -> tuple[dict[str, Any], ...]:
        try:
            response = await self.connection.set_config_option(
                config_id=config_id, session_id=session_id, value=value
            )
        except RequestError as exc:
            raise HarnessClientError(
                self._describe_rpc_error("session/set_config_option", exc)
            ) from exc
        options = tuple(
            option.model_dump(mode="json", by_alias=True, exclude_none=True)
            for option in response.config_options
        )
        self.bus.publish(ConfigOptionsUpdated(session_id=session_id, options=options))
        return options

    # --------------------------------------------------------------- extension

    async def _ext_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.extension.supports(method):
            raise ExtensionUnavailable(method)
        try:
            result = await self.connection.ext_method(strip_prefix(method), params)
        except RequestError as exc:
            if exc.code == -32601:
                raise ExtensionUnavailable(method) from exc
            raise HarnessClientError(self._describe_rpc_error(method, exc)) from exc
        if not isinstance(result, dict):
            raise ProtocolError(f"{method} returned an unexpected response type")
        return result

    async def harness_capabilities(
        self, session_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> CapabilityListResult:
        return await self.extensions.capabilities(session_id, cursor=cursor, limit=limit)

    async def harness_plugins(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> PluginListResult:
        return await self.extensions.plugins(cursor=cursor, limit=limit)

    async def harness_configuration(self, session_id: str) -> ConfigurationSnapshot:
        return await self.extensions.configuration(session_id)

    async def harness_update_configuration(
        self,
        session_id: str,
        changes: list[dict[str, Any]] | dict[str, Any],
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> ConfigurationSnapshot:
        return await self.extensions.update_configuration(
            session_id,
            changes,
            expected_revision=expected_revision,
            request_id=request_id,
        )

    async def harness_commands(
        self, session_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> CommandListResult:
        return await self.extensions.commands(session_id, cursor=cursor, limit=limit)

    async def harness_steer(self, session_id: str, text: str) -> bool:
        """Inject a follow-up into the turn that is currently running."""
        return await self.extensions.steer(session_id, text)

    async def harness_invoke_command(
        self,
        session_id: str,
        binding_id: str,
        command_name: str,
        *,
        text: str | None = None,
        arguments: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> CommandInvokeResponse:
        return await self.extensions.invoke_command(
            session_id,
            binding_id,
            command_name,
            text=text,
            arguments=arguments,
            expected_revision=expected_revision,
            request_id=request_id,
        )

    async def harness_cancel_command(self, session_id: str, request_id: str) -> dict[str, Any]:
        return await self.extensions.cancel_command(session_id, request_id)

    async def harness_presentations(
        self, session_id: str, tool_call_ids: list[str]
    ) -> tuple[ToolPresentationSnapshot, ...]:
        return await self.extensions.presentations(session_id, tool_call_ids)

    async def _raw_request(self, method: str, params: dict[str, Any]) -> Any:
        """Send one request through the SDK connection's public JSON-RPC layer."""
        conn = getattr(self.bundle.conn, "_conn", None)
        if conn is None or not hasattr(conn, "send_request"):
            raise ProtocolError(f"The installed ACP SDK cannot send '{method}' requests.")
        return await conn.send_request(method, params)

    async def drain_events(self, *, timeout: float = 1.0) -> None:
        """Let notification handlers and event consumers catch up with a prompt response."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stable = 0
        while loop.time() < deadline:
            await asyncio.sleep(0)
            await self.bus.drain()
            if self._inflight_updates == 0:
                stable += 1
                if stable >= 3:
                    return
                await asyncio.sleep(0.01)
            else:
                stable = 0
        await self.bus.drain()

    # ------------------------------------------------------- client callbacks

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._inflight_updates += 1
        try:
            dispatch_update(self.bus, session_id, update)
        finally:
            self._inflight_updates -= 1

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        record = normalize_permission(session_id, tool_call, options)
        try:
            decision = await self.policy.choose_permission(record)
        except (InteractionRequired, InteractionRejected) as exc:
            # Remember the policy failure so the turn fails with a clear error, and
            # tell the agent "cancelled" so it can wind down instead of waiting.
            self._pending_interaction_error = exc
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if decision.option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=decision.option_id)
        )

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> Any:
        record = elicitation_record(message, mode)
        try:
            answer = await self.policy.answer_elicitation(record)
        except (InteractionRequired, InteractionRejected) as exc:
            self._pending_interaction_error = exc
            return DeclineElicitationResponse(action="decline")
        if answer.action == "accept":
            return AcceptElicitationResponse(action="accept", content=answer.content or None)
        if answer.action == "cancel":
            return CancelElicitationResponse(action="cancel")
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    async def _answer_questions(self, params: dict[str, Any]) -> dict[str, Any]:
        request = parse_ask_user_request(params)
        try:
            response = await self.policy.answer_questions(request)
        except (InteractionRequired, InteractionRejected) as exc:
            self._pending_interaction_error = exc
            return ask_user_payload(AskUserResponse(cancelled=True))
        return ask_user_payload(response)

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        raise RequestError.method_not_found("fs/read_text_file")  # not advertised by this client

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/write_text_file")  # not advertised by this client

    async def ext_method(self, method: str, params: dict[str, Any]) -> Any:
        name = method.lstrip("_")
        if name == "harness/ask_user":
            return await self._answer_questions(params)
        if method in {"harness/configuration/changed", "_harness/configuration/changed"}:
            snapshot = parse_configuration_snapshot(params)
            self.bus.publish(
                ConfigurationChanged(session_id=snapshot.session_id, snapshot=snapshot)
            )
            return {}
        raise RequestError.method_not_found(f"_{method.lstrip('_')}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        name = method.lstrip("_")
        if name == "harness/configuration/changed":
            try:
                snapshot = parse_configuration_snapshot(params)
            except Exception:
                return
            self.bus.publish(
                ConfigurationChanged(session_id=snapshot.session_id, snapshot=snapshot)
            )
            return
        if name == "harness/commands/changed":
            try:
                changed = CommandCatalogChanged.model_validate(params)
            except Exception:
                return
            self.bus.publish(
                PluginCommandsListed(
                    session_id=changed.session_id,
                    commands=tuple(changed.commands),
                    selected_revision=changed.selected_revision,
                    supported_operations=tuple(changed.supported_operations),
                )
            )
            return
        if name == "harness/presentation/updated":
            try:
                updated = PresentationUpdated.model_validate(params)
            except Exception:
                return
            self.bus.publish(ToolPresentationUpdated(snapshots=tuple(updated.snapshots)))
            return
        return None

    def _describe_rpc_error(self, method: str, exc: RequestError) -> str:
        data = exc.data if isinstance(exc.data, dict) else {}
        code = data.get("code")
        details = data.get("details") or {}
        detail_text = ""
        if isinstance(details, dict):
            for key in ("bindingId", "bindingIds", "currentRevision", "message", "reason"):
                if key in details:
                    detail_text += f" {key}={details[key]}"
        elif details:
            detail_text = f" {details}"
        suffix = f" [{code}]" if code else ""
        return f"{method} failed: {exc}{suffix}{detail_text}".strip()


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge metadata dictionaries without losing sibling keys."""
    result = dict(base)
    for key, value in extra.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
