"""Serve an MCP SDK server over the stateless MCP-over-ACP binding.

This is the Python counterpart of the TypeScript SDK's ``fromMcpServer``. It
uses the official ``mcp`` SDK's single-exchange modern path
(``mcp.server.runner.serve_one``) for each ``mcp/message`` request, so
``server/discover``, capability checks, tool calls, prompts, resources, and
input-required results behave as they do over MCP 2026-07-28 Streamable HTTP.

The factory runs per request, matching the MCP 2026-07-28 requirement that a
request's context is not inferred from an earlier request. Keep shared state
(database pools, upstream clients, caches) outside the factory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp.server.connection import Connection
from mcp.server.lowlevel import Server as LowLevelServer
from mcp.server.mcpserver import MCPServer
from mcp.server.runner import serve_one
from mcp.shared.dispatcher import CallOptions
from mcp.shared.exceptions import MCPError, NoBackChannelError
from mcp.shared.jsonrpc_dispatcher import progress_token_from_params
from mcp.shared.message import ServerMessageMetadata
from mcp.shared.transport_context import TransportContext
from mcp_types import ProgressToken
from pydantic import TypeAdapter, ValidationError

from .core import (
    JsonObject,
    McpError,
    McpErrorOutcome,
    McpOperationContext,
    McpOutcome,
    McpResultOutcome,
)

__all__ = ["McpServerAdapter", "from_mcp_server"]

_JSON_OBJECT = TypeAdapter(JsonObject)

__all__ = ["McpServerAdapter", "from_mcp_server"]

McpServerLike = LowLevelServer | MCPServer
McpServerFactory = Callable[[], McpServerLike]


@dataclass(frozen=True)
class McpServerAdapter:
    """An MCP SDK server adapted to serve MCP-over-ACP requests."""

    handler: Callable[[McpOperationContext], Awaitable[McpOutcome]]
    """Request handler to pass to :meth:`McpOverAcpProvider.register_server`."""

    close: Callable[[], Awaitable[None]] | None = None
    """Closes open subscriptions and in-flight server instances, if any."""

    async def dispose(self) -> None:
        if self.close is not None:
            await self.close()


@dataclass(kw_only=True)
class _AcpDispatchContext:
    """``DispatchContext`` for one ``mcp/message`` request.

    Structurally satisfies ``mcp.shared.dispatcher.DispatchContext``. The
    back-channel is closed by construction: MCP 2026-07-28 has no
    server-originated requests. Notifications travel back as request-scoped
    ACP notifications.
    """

    transport: TransportContext
    request_id: str
    message_metadata: ServerMessageMetadata
    progress_token: ProgressToken | None
    notify_cb: Callable[[str, JsonObject | None], Awaitable[None]]
    cancel_requested: anyio.Event = field(default_factory=anyio.Event)
    can_send_request: bool = False

    async def send_raw_request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        opts: CallOptions | None = None,
    ) -> dict[str, Any]:
        raise NoBackChannelError(method)

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        opts: CallOptions | None = None,
    ) -> None:
        body = dict(params) if params is not None else None
        await self.notify_cb(method, _json_object(body))

    async def progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        if self.progress_token is None:
            return
        params: JsonObject = {
            "progressToken": progress_token_value(self.progress_token),
            "progress": progress,
        }
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        await self.notify("notifications/progress", params)


def progress_token_value(token: ProgressToken) -> str | int:
    """Render a progress token as JSON."""
    return token


def _json_object(value: Mapping[str, Any] | None) -> JsonObject | None:
    if value is None:
        return None
    return _JSON_OBJECT.validate_python(dict(value))


def from_mcp_server(factory: McpServerFactory) -> McpServerAdapter:
    """Adapt an MCP SDK server factory to the MCP-over-ACP binding."""

    async def handler(context: McpOperationContext) -> McpOutcome:
        server = factory()
        lowlevel = _lowlevel(server)
        connection = Connection.from_envelope(
            context.protocol_version,
            context.client_info,
            context.client_capabilities,
        )
        dctx = _AcpDispatchContext(
            transport=TransportContext(kind="acp", can_send_request=False),
            request_id=context.request_id,
            message_metadata=ServerMessageMetadata(),
            progress_token=progress_token_from_params(context.params),
            notify_cb=context.notify,
        )
        watcher = asyncio.create_task(
            _watch_cancel(context.signal, dctx.cancel_requested),
            name=f"mcp-over-acp-cancel:{context.request_id}",
        )
        try:
            async with lowlevel.lifespan(lowlevel) as lifespan_state:
                result = await serve_one(
                    lowlevel,
                    dctx,
                    context.method,
                    context.params,
                    connection=connection,
                    lifespan_state=lifespan_state,
                )
        except asyncio.CancelledError:
            dctx.cancel_requested.set()
            raise
        except MCPError as exc:
            return McpErrorOutcome(
                error=McpError(
                    code=exc.code,
                    message=exc.message,
                    data=exc.data,
                )
            )
        except ValidationError:
            return McpErrorOutcome(
                error=McpError(code=-32602, message="Invalid request parameters")
            )
        finally:
            watcher.cancel()
            with suppress(BaseException):
                await watcher
        return McpResultOutcome(result=result)

    return McpServerAdapter(handler=handler)


def _lowlevel(server: McpServerLike) -> LowLevelServer:
    if isinstance(server, MCPServer):
        return server._lowlevel_server
    return server


async def _watch_cancel(signal: asyncio.Event, target: anyio.Event) -> None:
    await signal.wait()
    target.set()
