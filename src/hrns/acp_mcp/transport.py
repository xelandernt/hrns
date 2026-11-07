"""An MCP client transport that carries MCP-over-ACP requests.

The agent side of the binding is an ordinary MCP client: FastMCP's
``MCPToolset`` (or any MCP client) expects a ``ClientSession``. This transport
gives it one, then pumps the session's JSON-RPC frames through
:class:`~hrns.acp_mcp.core.McpOverAcpCaller` and the ACP connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Unpack

import anyio
from acp.exceptions import RequestError
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastmcp.client.transports.base import ClientTransport, SessionKwargs, TransportOptions
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp_types import (
    ErrorData,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)
from pydantic import TypeAdapter

from .core import (
    AcpChannel,
    JsonObject,
    McpErrorOutcome,
    McpNotification,
    McpOverAcpCaller,
    McpRequestOptions,
    McpResultOutcome,
)

logger = logging.getLogger(__name__)

__all__ = ["AcpMcpClientTransport"]

_JSON_OBJECT = TypeAdapter(JsonObject)


class AcpMcpClientTransport(ClientTransport):
    """An MCP client transport for one ``type: "acp"`` server declaration."""

    def __init__(
        self,
        caller: McpOverAcpCaller,
        channel: AcpChannel,
        server_id: str,
        *,
        name: str | None = None,
    ) -> None:
        self._caller = caller
        self._channel = channel
        self._server_id = server_id
        self._name = name or server_id
        self._active: dict[str, asyncio.Event] = {}

    @property
    def name(self) -> str:
        return self._name

    @asynccontextmanager
    async def connect_session(
        self,
        *,
        transport_options: TransportOptions | None = None,
        **session_kwargs: Unpack[SessionKwargs],
    ) -> AsyncGenerator[ClientSession, None]:
        options = transport_options or TransportOptions()
        session_in_send, session_in_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](64)
        session_out_send, session_out_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](64)
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._pump, session_out_recv, session_in_send)
            try:
                async with options.session_class(
                    session_in_recv, session_out_send, **session_kwargs
                ) as session:
                    yield session
            finally:
                tg.cancel_scope.cancel()

    async def _pump(
        self,
        incoming: MemoryObjectReceiveStream[SessionMessage | Exception],
        outgoing: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> None:
        async with anyio.create_task_group() as tg:
            async for item in incoming:
                if isinstance(item, Exception):
                    continue
                message = item.message
                if isinstance(message, JSONRPCRequest):
                    tg.start_soon(self._handle_request, message, outgoing)
                elif isinstance(message, JSONRPCNotification):
                    self._handle_notification(message)
                # Responses from the MCP client are not expected: the client
                # session does not answer server-initiated requests in the
                # stateless binding.

    def _handle_notification(self, message: JSONRPCNotification) -> None:
        """Translate MCP's request cancellation into the ACP request signal."""
        if message.method != "notifications/cancelled":
            return
        params = message.params or {}
        request_id = params.get("requestId")
        if request_id is None:
            return
        signal = self._active.get(str(request_id))
        if signal is not None:
            signal.set()

    async def _handle_request(
        self,
        request: JSONRPCRequest,
        outgoing: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> None:
        request_id = str(request.id)
        signal = asyncio.Event()
        self._active[request_id] = signal

        def on_notification(notification: McpNotification) -> None:
            if notification.params is None:
                return
            try:
                outgoing.send_nowait(
                    SessionMessage(
                        JSONRPCNotification(
                            jsonrpc="2.0",
                            method=notification.method,
                            params=_json_object(notification.params),
                        )
                    )
                )
            except anyio.WouldBlock:
                logger.warning("dropped MCP notification for %s: stream is full", request_id)

        try:
            try:
                outcome = await self._caller.request(
                    self._channel,
                    self._server_id,
                    request.method,
                    _json_object(request.params),
                    McpRequestOptions(
                        request_id=request_id,
                        signal=signal,
                        on_notification=on_notification,
                    ),
                )
            except RequestError as exc:
                response: JSONRPCResponse | JSONRPCError = JSONRPCError(
                    jsonrpc="2.0",
                    id=request.id,
                    error=ErrorData(code=exc.code, message=str(exc), data=exc.data),
                )
            except Exception as exc:
                logger.exception("MCP-over-ACP request failed")
                response = JSONRPCError(
                    jsonrpc="2.0",
                    id=request.id,
                    error=ErrorData(code=-32603, message=f"MCP-over-ACP request failed: {exc}"),
                )
            else:
                response = _response_for(request.id, outcome)
            try:
                await outgoing.send(SessionMessage(response))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                return
        finally:
            self._active.pop(request_id, None)


def _response_for(
    request_id: int | str, outcome: McpResultOutcome | McpErrorOutcome
) -> JSONRPCResponse | JSONRPCError:
    if isinstance(outcome, McpErrorOutcome):
        return JSONRPCError(
            jsonrpc="2.0",
            id=request_id,
            error=ErrorData(
                code=outcome.error.code,
                message=outcome.error.message,
                data=outcome.error.data,
            ),
        )
    result = outcome.result
    if not isinstance(result, dict):
        return JSONRPCError(
            jsonrpc="2.0",
            id=request_id,
            error=ErrorData(code=-32603, message="MCP result is not an object"),
        )
    return JSONRPCResponse(
        jsonrpc="2.0", id=request_id, result=_JSON_OBJECT.validate_python(result)
    )


def _json_object(value: object) -> JsonObject | None:
    if value is None:
        return None
    return _JSON_OBJECT.validate_python(value)
