"""Mount ACP on a FastAPI application."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, TypeAlias

import acp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection

from ._types import PrincipalT

if TYPE_CHECKING:
    from .acp.host import AcpServer

Authenticator: TypeAlias = Callable[[HTTPConnection], Awaitable[PrincipalT | None]]

# A 9 MiB prompt can expand up to sixfold when JSON escapes control characters.
_ACP_MESSAGE_LIMIT = 64 * 1024 * 1024


class _AcpMessageTooLarge(Exception):
    """An ACP message exceeded the WebSocket bridge's byte limit."""


@asynccontextmanager
async def _acp_streams(
    agent: acp.Agent,
) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    """Bridge an ACP adapter to asyncio streams over a socketpair.

    ``acp.AgentSideConnection`` requires real asyncio streams, so the WebSocket
    handler talks to the agent through one end of a socketpair.
    """
    local, remote = socket.socketpair()
    local.setblocking(False)
    remote.setblocking(False)
    local_reader, local_writer = await asyncio.open_connection(sock=local, limit=_ACP_MESSAGE_LIMIT)
    remote_reader, remote_writer = await asyncio.open_connection(sock=remote)

    task = asyncio.create_task(
        acp.run_agent(
            agent,
            input_stream=remote_writer,
            output_stream=remote_reader,
            use_unstable_protocol=True,
        )
    )
    try:
        yield local_reader, local_writer
    finally:
        detach = getattr(agent, "detach", None)
        if detach is not None:
            detach()
        local_writer.close()
        with suppress(Exception):
            await local_writer.wait_closed()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        remote_writer.close()
        with suppress(Exception):
            await remote_writer.wait_closed()


async def _pump_acp(
    websocket: WebSocket,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    async def client_to_agent() -> None:
        try:
            while True:
                text = await websocket.receive_text()
                payload = text.encode("utf-8")
                if len(payload) > _ACP_MESSAGE_LIMIT:
                    raise _AcpMessageTooLarge
                writer.write(payload + b"\n")
                await writer.drain()
        except WebSocketDisconnect:
            return

    async def agent_to_client() -> None:
        while True:
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                raise _AcpMessageTooLarge from exc
            except asyncio.IncompleteReadError:
                return
            await websocket.send_text(line.decode("utf-8").rstrip("\n"))

    tasks = [
        asyncio.create_task(client_to_agent()),
        asyncio.create_task(agent_to_client()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in pending:
        with suppress(asyncio.CancelledError):
            await task
    for task in done:
        with suppress(WebSocketDisconnect, OSError):
            task.result()


async def _serve_acp(
    websocket: WebSocket,
    adapter: acp.Agent,
) -> None:
    await websocket.accept()
    code, reason = 1000, ""
    try:
        async with _acp_streams(adapter) as (reader, writer):
            await _pump_acp(websocket, reader, writer)
    except _AcpMessageTooLarge:
        code, reason = 1009, "ACP message exceeds the 64 MiB limit"
    except Exception:
        code, reason = 1011, "ACP WebSocket bridge failed"
        raise
    finally:
        with suppress(OSError, RuntimeError, WebSocketDisconnect):
            await websocket.close(code=code, reason=reason)


def mount_acp_routes(
    server: AcpServer[PrincipalT],
    app: FastAPI,
    *,
    prefix: str,
    authenticate: Authenticator[PrincipalT] | None = None,
) -> None:
    @app.websocket(prefix.rstrip("/") + "/{agent_id}")
    async def acp_endpoint(websocket: WebSocket, agent_id: str) -> None:
        principal = await authenticate(websocket) if authenticate is not None else None
        if authenticate is not None and principal is None:
            await websocket.close(code=1008)
            return
        entry = server._registrations.get(agent_id)
        if entry is None or not server._running:
            await websocket.close(code=1008)
            return
        await _serve_acp(websocket, entry.adapter(principal))
