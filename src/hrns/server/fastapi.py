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

if TYPE_CHECKING:
    from .acp.host import AcpServer

Authenticator: TypeAlias = Callable[[HTTPConnection], Awaitable[object | None]]


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
    local_reader, local_writer = await asyncio.open_connection(sock=local)
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
                writer.write(text.encode("utf-8") + b"\n")
                await writer.drain()
        except WebSocketDisconnect:
            return

    async def agent_to_client() -> None:
        while True:
            line = await reader.readline()
            if not line:
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
        with suppress(Exception):
            task.result()


async def _serve_acp(
    websocket: WebSocket,
    adapter: acp.Agent,
) -> None:
    await websocket.accept()
    async with _acp_streams(adapter) as (reader, writer):
        await _pump_acp(websocket, reader, writer)
    with suppress(Exception):
        await websocket.close()


async def authorize(connection: HTTPConnection, authenticate: Authenticator | None) -> bool:
    return authenticate is None or await authenticate(connection) is not None


def mount_acp_routes(
    server: AcpServer, app: FastAPI, *, prefix: str, authenticate: Authenticator | None = None
) -> None:
    @app.websocket(prefix.rstrip("/") + "/{agent_id}")
    async def acp_endpoint(websocket: WebSocket, agent_id: str) -> None:
        if not await authorize(websocket, authenticate):
            await websocket.close(code=1008)
            return
        entry = server._registrations.get(agent_id)
        if entry is None or not server._running:
            await websocket.close(code=1008)
            return
        await _serve_acp(websocket, entry.adapter())
