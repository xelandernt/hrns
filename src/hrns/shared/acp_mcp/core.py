"""Stateless MCP-over-ACP binding for MCP 2026-07-28.

This module mirrors the TypeScript SDK's ``McpOverAcpCaller`` and
``McpOverAcpProvider``. It speaks the current ACP RFD, not the older
connection-oriented prototype: ``mcp/message`` carries ``serverId`` and a
logical ``requestId``, with no ``mcp/connect`` or ``mcp/disconnect`` exchange.

The wire shape is standard ACP; the local Python SDK does not generate the
stateless schema yet, so :mod:`hrns.shared.acp_mcp.router` installs these routes on an
SDK connection and sends raw ``mcp/message`` frames.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Protocol

from acp.exceptions import RequestError
from acp.schema import AcpMcpServer
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

MCP_MESSAGE_METHOD = "mcp/message"
"""ACP method carrying MCP requests and request-scoped notifications."""

MCP_PROTOCOL_VERSION = "2026-07-28"
"""The only MCP revision this binding supports."""

MCP_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
MCP_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
MCP_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"

JsonObject = dict[str, JsonValue]
"""A JSON object, as carried in MCP params and metadata."""

_JSON_OBJECT = TypeAdapter(JsonObject)


class McpBindingErrorCode(IntEnum):
    """Outer ACP error codes reserved for failures of this binding.

    These are never inner MCP error codes. An inner MCP error travels inside
    a successful ACP response's ``error`` carrier instead.
    """

    INVALID_PARAMS = -32602
    REQUEST_CANCELLED = -32800
    RESOURCE_LIMIT_EXCEEDED = -33000
    SERVER_UNAVAILABLE = -33001
    BACKEND_FAILED = -33002


class McpError(BaseModel):
    """An inner MCP error object."""

    model_config = ConfigDict(extra="allow")

    code: int
    message: str
    data: JsonValue = None


class McpResultOutcome(BaseModel):
    """The result branch of an ``mcp/message`` response carrier."""

    model_config = ConfigDict(populate_by_name=True)

    result: JsonValue
    meta: JsonObject | None = Field(default=None, alias="_meta")


class McpErrorOutcome(BaseModel):
    """The error branch of an ``mcp/message`` response carrier."""

    model_config = ConfigDict(populate_by_name=True)

    error: McpError
    meta: JsonObject | None = Field(default=None, alias="_meta")


McpOutcome = McpResultOutcome | McpErrorOutcome


def parse_mcp_outcome(value: object) -> McpOutcome | None:
    """Parse a received response carrier.

    Returns ``None`` if the value is not a valid carrier. When both ``result``
    and ``error`` are present, ``result`` wins, matching the RFD.
    """
    if not isinstance(value, dict):
        return None
    meta = value.get("_meta")
    carrier_meta = meta if isinstance(meta, dict) else None
    if "result" in value:
        return McpResultOutcome(result=value["result"], meta=carrier_meta)
    error = value.get("error")
    if _is_mcp_error(error):
        return McpErrorOutcome(error=McpError.model_validate(error), meta=carrier_meta)
    return None


def outcome_to_wire(outcome: McpOutcome) -> JsonObject:
    """Convert an outcome to the wire carrier."""
    if isinstance(outcome, McpResultOutcome):
        wire: JsonObject = {"result": outcome.result}
    else:
        error = _JSON_OBJECT.validate_python(
            outcome.error.model_dump(mode="json", exclude_unset=True)
        )
        wire = {"error": error}
    if outcome.meta is not None:
        wire["_meta"] = outcome.meta
    return wire


def _is_mcp_error(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("code"), int)
        and not isinstance(value.get("code"), bool)
        and isinstance(value.get("message"), str)
    )


class McpProtocolError(Exception):
    """An inner MCP protocol error raised by a provider handler.

    It is returned in the response's ``error`` carrier, never as an outer ACP
    error.
    """

    def __init__(self, code: int, message: str, data: JsonValue = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_mcp_error(self) -> McpError:
        if self.data is None:
            return McpError(code=self.code, message=self.message)
        return McpError(code=self.code, message=self.message, data=self.data)


class McpMessageParams(BaseModel):
    """Wire params of an ``mcp/message`` request or notification."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    server_id: str = Field(alias="serverId")
    request_id: str = Field(alias="requestId")
    method: str
    params: JsonObject | None = None
    meta: JsonObject | None = Field(default=None, alias="_meta")


@dataclass(frozen=True, kw_only=True)
class McpNotification:
    """A request-scoped MCP notification for an in-flight request."""

    method: str
    params: JsonObject | None = None
    meta: JsonObject | None = None


@dataclass(frozen=True, kw_only=True)
class McpCallerOptions:
    """Defaults used by an MCP-over-ACP caller."""

    client_capabilities: JsonObject = field(default_factory=dict)
    client_info: JsonObject | None = None


@dataclass(frozen=True, kw_only=True)
class McpRequestOptions:
    """Options for one MCP-over-ACP request."""

    request_id: str | None = None
    signal: asyncio.Event | None = None
    on_notification: Callable[[McpNotification], None] | None = None
    meta: JsonObject | None = None


@dataclass(frozen=True, kw_only=True)
class McpOperationContext:
    """Context passed to a provider handler for one operation."""

    server_id: str
    request_id: str
    method: str
    params: JsonObject | None
    protocol_version: str
    client_capabilities: JsonObject
    client_info: JsonValue
    envelope_meta: JsonObject | None
    signal: asyncio.Event
    notify: Callable[[str, JsonObject | None], Awaitable[None]]


McpRequestHandler = Callable[[McpOperationContext], Awaitable[McpOutcome | JsonObject]]
"""Handles one MCP request for a registered server.

Return an outcome, or raise :class:`McpProtocolError` to answer with an inner
MCP error. Any other raised error is reported as an outer binding failure
(``-33002``), or as ``-32800`` when the request was cancelled.
"""


@dataclass(frozen=True, kw_only=True)
class McpServerOptions:
    """Options for registering an MCP server on an ACP connection."""

    name: str
    handler: McpRequestHandler
    server_id: str | None = None
    max_active_requests: int = 64


class AcpChannel(Protocol):
    """The transport surface the binding needs from an ACP connection."""

    @property
    def key(self) -> object:
        """A stable identity for the underlying ACP connection."""
        ...

    async def send_request(self, method: str, params: JsonObject) -> JsonValue:
        """Send an ACP request and return its raw result."""
        ...

    async def send_notification(self, method: str, params: JsonObject) -> None:
        """Send an ACP notification."""
        ...


@dataclass(kw_only=True)
class _ServerEntry:
    server_id: str
    name: str
    handler: McpRequestHandler
    max_active_requests: int
    active: dict[str, asyncio.Task[McpOutcome]] = field(default_factory=dict)
    disposed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _ProviderConnection:
    servers: dict[str, _ServerEntry] = field(default_factory=dict)
    used_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, kw_only=True)
class _ValidatedMeta:
    protocol_version: str
    client_capabilities: JsonObject
    client_info: JsonValue


class McpServerRegistration:
    """A registered MCP server provided over an ACP connection."""

    def __init__(
        self, *, name: str, server_id: str, release: Callable[[], Awaitable[None]]
    ) -> None:
        self.name = name
        self.server_id = server_id
        self._release = release

    @property
    def declaration(self) -> AcpMcpServer:
        """The ACP declaration to include in a session's ``mcpServers``."""
        return AcpMcpServer(name=self.name, server_id=self.server_id)

    async def dispose(self) -> None:
        """Remove the registration and cancel its active requests."""
        await self._release()

    async def __aenter__(self) -> McpServerRegistration:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.dispose()


class McpOverAcpProvider:
    """Serves MCP servers to an ACP peer over ``mcp/message``."""

    def __init__(self) -> None:
        self._connections: dict[object, _ProviderConnection] = {}

    def _connection(self, channel: AcpChannel) -> _ProviderConnection:
        return self._connections.setdefault(channel.key, _ProviderConnection())

    def register_server(
        self, channel: AcpChannel, options: McpServerOptions
    ) -> McpServerRegistration:
        """Register an MCP server on ``channel``.

        The server is ready to serve requests as soon as this returns, so
        publish its :attr:`McpServerRegistration.declaration` only afterwards.
        """
        if not options.name:
            raise ValueError("MCP server name must not be empty")
        if not isinstance(options.max_active_requests, int) or options.max_active_requests < 1:
            raise ValueError("max_active_requests must be a positive integer")
        if not inspect.iscoroutinefunction(options.handler):
            raise TypeError("MCP request handlers must be async functions")
        state = self._connection(channel)
        server_id = options.server_id or _generate_id(options.name)
        if server_id in state.used_ids:
            raise ValueError(
                f"MCP server ID {server_id!r} was already registered on this connection"
            )
        state.used_ids.add(server_id)
        entry = _ServerEntry(
            server_id=server_id,
            name=options.name,
            handler=options.handler,
            max_active_requests=options.max_active_requests,
        )
        state.servers[server_id] = entry

        async def release() -> None:
            if state.servers.get(server_id) is not entry:
                return
            state.servers.pop(server_id, None)
            entry.disposed.set()
            tasks = list(entry.active.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        return McpServerRegistration(name=options.name, server_id=server_id, release=release)

    async def handle_request(self, channel: AcpChannel, params: object) -> McpOutcome:
        """Handle an incoming ``mcp/message`` request."""
        try:
            envelope = McpMessageParams.model_validate(params)
        except ValidationError as exc:
            raise RequestError.invalid_params({"errors": exc.errors()}) from exc

        state = self._connection(channel)
        server = state.servers.get(envelope.server_id)
        if server is None:
            raise RequestError(
                int(McpBindingErrorCode.SERVER_UNAVAILABLE),
                "MCP server registration unavailable",
                {"serverId": envelope.server_id},
            )
        if envelope.request_id in server.active:
            raise RequestError.invalid_params(
                {"serverId": envelope.server_id, "requestId": envelope.request_id}
            )
        if len(server.active) >= server.max_active_requests:
            raise RequestError(
                int(McpBindingErrorCode.RESOURCE_LIMIT_EXCEEDED),
                "MCP-over-ACP active request limit exceeded",
                {"serverId": envelope.server_id},
            )

        cancel = asyncio.Event()
        done = False

        async def notify(method: str, notify_params: JsonObject | None) -> None:
            if done:
                return
            message: JsonObject = {
                "serverId": envelope.server_id,
                "requestId": envelope.request_id,
                "method": method,
            }
            if notify_params is not None:
                message["params"] = notify_params
            await channel.send_notification(MCP_MESSAGE_METHOD, message)

        task = asyncio.create_task(
            _run_server_handler(server, envelope, cancel, notify),
            name=f"mcp-over-acp:{envelope.server_id}:{envelope.request_id}",
        )
        server.active[envelope.request_id] = task
        watcher = asyncio.create_task(_watch_disposal(server, cancel))
        try:
            return await task
        except asyncio.CancelledError:
            cancel.set()
            task.cancel()
            with suppress(BaseException):
                await task
            raise
        except Exception as exc:
            raise _to_outer_error(exc, cancel) from exc
        finally:
            done = True
            watcher.cancel()
            with suppress(BaseException):
                await watcher
            server.active.pop(envelope.request_id, None)

    def handle_notification(self, _channel: AcpChannel, _params: object) -> None:
        """The provider does not receive provider-to-consumer notifications."""
        return None


async def _watch_disposal(server: _ServerEntry, cancel: asyncio.Event) -> None:
    await server.disposed.wait()
    cancel.set()


async def _run_server_handler(
    server: _ServerEntry,
    envelope: McpMessageParams,
    signal: asyncio.Event,
    notify: Callable[[str, JsonObject | None], Awaitable[None]],
) -> McpOutcome:
    # Yield so the operation is recorded as active before the handler starts.
    await asyncio.sleep(0)
    validated = _validate_inner_meta(envelope.params)
    if isinstance(validated, McpErrorOutcome):
        return validated
    if signal.is_set():
        raise asyncio.CancelledError()
    context = McpOperationContext(
        server_id=envelope.server_id,
        request_id=envelope.request_id,
        method=envelope.method,
        params=envelope.params,
        protocol_version=validated.protocol_version,
        client_capabilities=validated.client_capabilities,
        client_info=validated.client_info,
        envelope_meta=envelope.meta,
        signal=signal,
        notify=notify,
    )
    try:
        result = await server.handler(context)
    except McpProtocolError as exc:
        return McpErrorOutcome(error=exc.to_mcp_error())
    outcome = _coerce_outcome(result)
    if outcome is None:
        raise _BindingFailure(
            "MCP server handler produced an invalid outcome: expected exactly one of a "
            "defined `result` or an `error` with an integer `code` and string `message`"
        )
    return outcome


def _validate_inner_meta(params: JsonObject | None) -> _ValidatedMeta | McpErrorOutcome:
    raw: JsonObject = params or {}
    meta = raw.get("_meta")
    if not isinstance(meta, dict):
        return _invalid_mcp_params("missing params._meta")
    protocol_version = meta.get(MCP_META_PROTOCOL_VERSION)
    if not isinstance(protocol_version, str):
        return _invalid_mcp_params(f"missing {MCP_META_PROTOCOL_VERSION} in params._meta")
    if protocol_version != MCP_PROTOCOL_VERSION:
        return McpErrorOutcome(
            error=McpError(
                code=-32022,
                message="Unsupported protocol version",
                data={"supported": [MCP_PROTOCOL_VERSION], "requested": protocol_version},
            )
        )
    client_capabilities = meta.get(MCP_META_CLIENT_CAPABILITIES)
    if not isinstance(client_capabilities, dict):
        return _invalid_mcp_params(f"missing {MCP_META_CLIENT_CAPABILITIES} in params._meta")
    return _ValidatedMeta(
        protocol_version=protocol_version,
        client_capabilities=_JSON_OBJECT.validate_python(client_capabilities),
        client_info=meta.get(MCP_META_CLIENT_INFO),
    )


def _invalid_mcp_params(message: str) -> McpErrorOutcome:
    return McpErrorOutcome(error=McpError(code=-32602, message=f"Invalid params: {message}"))


class _BindingFailure(Exception):
    """A failure detected by the binding itself, reported as ``-33002``."""


def _coerce_outcome(value: object) -> McpOutcome | None:
    if isinstance(value, (McpResultOutcome, McpErrorOutcome)):
        return value
    if isinstance(value, dict):
        return parse_mcp_outcome(value)
    return None


def _to_outer_error(error: Exception, signal: asyncio.Event) -> RequestError:
    if signal.is_set() or (
        isinstance(error, RequestError) and error.code == int(McpBindingErrorCode.REQUEST_CANCELLED)
    ):
        return RequestError.request_cancelled()
    if isinstance(error, _BindingFailure):
        return RequestError(int(McpBindingErrorCode.BACKEND_FAILED), str(error))
    return RequestError(
        int(McpBindingErrorCode.BACKEND_FAILED),
        "MCP server handler failed",
        {"details": str(error)},
    )


def with_mcp_request_meta(
    params: JsonObject | None, options: McpCallerOptions | None = None
) -> JsonObject:
    """Add the per-request MCP metadata required by MCP 2026-07-28."""
    effective = options or McpCallerOptions()
    base = dict(params or {})
    existing = base.get("_meta")
    meta = dict(existing) if isinstance(existing, dict) else {}
    meta.setdefault(MCP_META_PROTOCOL_VERSION, MCP_PROTOCOL_VERSION)
    meta.setdefault(MCP_META_CLIENT_CAPABILITIES, effective.client_capabilities)
    if effective.client_info is not None:
        meta.setdefault(MCP_META_CLIENT_INFO, effective.client_info)
    base["_meta"] = meta
    return base


def _generate_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


@dataclass
class _CallerConnection:
    routes: dict[tuple[str, str], Callable[[McpNotification], None]] = field(default_factory=dict)


class McpOverAcpCaller:
    """Calls MCP servers that an ACP peer declared with ``"type": "acp"``."""

    def __init__(self, options: McpCallerOptions | None = None) -> None:
        self._options = options or McpCallerOptions()
        self._connections: dict[object, _CallerConnection] = {}

    def _connection(self, channel: AcpChannel) -> _CallerConnection:
        return self._connections.setdefault(channel.key, _CallerConnection())

    async def request(
        self,
        channel: AcpChannel,
        server_id: str,
        method: str,
        params: JsonObject | None = None,
        options: McpRequestOptions | None = None,
    ) -> McpOutcome:
        """Send one MCP request to a declared server and return its outcome.

        Required MCP 2026-07-28 metadata is added to ``params._meta`` unless
        already present. Outer ACP failures reject with a ``RequestError``;
        inner MCP errors resolve as :class:`McpErrorOutcome`.
        """
        effective = options or McpRequestOptions()
        request_id = effective.request_id or _generate_id("mcp-request")
        key = (server_id, request_id)
        routes = self._connection(channel).routes
        if key in routes:
            raise ValueError(
                f"MCP request {request_id!r} is already active for server {server_id!r}"
            )
        routes[key] = effective.on_notification or _ignore_notification
        envelope: JsonObject = {
            "serverId": server_id,
            "requestId": request_id,
            "method": method,
            "params": with_mcp_request_meta(params, self._options),
        }
        if effective.meta is not None:
            envelope["_meta"] = effective.meta
        try:
            response = await _send_with_signal(channel, envelope, effective.signal)
        finally:
            routes.pop(key, None)
        outcome = parse_mcp_outcome(response)
        if outcome is None:
            raise RequestError(
                int(McpBindingErrorCode.BACKEND_FAILED),
                "Invalid MCP-over-ACP response carrier",
                {"response": response},
            )
        return outcome

    def handle_notification(self, channel: AcpChannel, params: object) -> None:
        """Handle an incoming ``mcp/message`` notification.

        Notifications for unknown or already completed requests are dropped.
        """
        try:
            envelope = McpMessageParams.model_validate(params)
        except ValidationError:
            return
        listener = self._connection(channel).routes.get((envelope.server_id, envelope.request_id))
        if listener is None:
            return
        try:
            listener(
                McpNotification(
                    method=envelope.method,
                    params=envelope.params,
                    meta=envelope.meta,
                )
            )
        except Exception:
            logger.exception("MCP-over-ACP notification listener failed")


def _ignore_notification(_notification: McpNotification) -> None:
    return None


async def _send_with_signal(
    channel: AcpChannel, envelope: JsonObject, signal: asyncio.Event | None
) -> JsonValue:
    if signal is None:
        return await channel.send_request(MCP_MESSAGE_METHOD, envelope)
    send_task = asyncio.create_task(channel.send_request(MCP_MESSAGE_METHOD, envelope))
    signal_task = asyncio.create_task(signal.wait())
    try:
        done, _pending = await asyncio.wait(
            {send_task, signal_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if signal.is_set() and not send_task.done():
            send_task.cancel()
            with suppress(BaseException):
                await send_task
            raise RequestError.request_cancelled()
        signal_task.cancel()
        with suppress(BaseException):
            await signal_task
        return await send_task
    finally:
        for task in (send_task, signal_task):
            if not task.done():
                task.cancel()
