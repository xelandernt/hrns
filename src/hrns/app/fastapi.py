"""Mount ACP and AG-UI on one FastAPI application."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

import acp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic_ai.agent import AbstractAgent
from starlette.requests import Request
from starlette.responses import Response

from .acp import PydanticAIACPAgent, create_acp_agent
from .agent import build_agent
from .agui import PydanticAIAGUIAgent

__all__ = [
    "create_app",
    "mount_acp",
    "mount_acp_adapter",
    "mount_agui",
    "mount_agui_adapter",
]


@asynccontextmanager
async def _acp_streams(
    agent: PydanticAIACPAgent[None, str],
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
        local_writer.close()
        with suppress(Exception):
            await local_writer.wait_closed()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
    adapter: PydanticAIACPAgent[None, str],
) -> None:
    await websocket.accept()
    async with _acp_streams(adapter) as (reader, writer):
        await _pump_acp(websocket, reader, writer)
    with suppress(Exception):
        await websocket.close()


def mount_acp_adapter(
    app: FastAPI,
    adapter: PydanticAIACPAgent[None, str],
    *,
    path: str = "/acp",
) -> None:
    """Mount one pre-built ACP adapter.

    The upstream adapter holds one connection and one session namespace, so this
    endpoint is intended for a single WebSocket. Use :func:`mount_acp` when each
    connection should get its own adapter.
    """

    @app.websocket(path)
    async def acp_adapter_endpoint(websocket: WebSocket) -> None:
        await _serve_acp(websocket, adapter)


def mount_acp(
    app: FastAPI,
    agent_factory: Callable[[], AbstractAgent[None, str]] = build_agent,
    *,
    path: str = "/acp",
) -> None:
    """Expose an ACP agent over a WebSocket endpoint.

    Each WebSocket gets its own adapter and session namespace. Sharing one ACP
    session across several sockets needs a fan-out layer above this; the upstream
    adapter itself is single-connection.
    """

    @app.websocket(path)
    async def acp_endpoint(websocket: WebSocket) -> None:
        await _serve_acp(websocket, create_acp_agent(agent_factory()))


def mount_agui_adapter(
    app: FastAPI,
    adapter: PydanticAIAGUIAgent,
    *,
    path: str = "/agui",
) -> None:
    """Mount a pre-built AG-UI adapter."""

    @app.post(path)
    async def agui_adapter_endpoint(request: Request) -> Response:
        return await adapter.dispatch_request(request)


def mount_agui(
    app: FastAPI,
    agent: AbstractAgent[None, str],
    *,
    path: str = "/agui",
) -> None:
    """Expose an agent over an AG-UI POST endpoint."""
    mount_agui_adapter(app, PydanticAIAGUIAgent(agent), path=path)


def create_app(agent: AbstractAgent[None, str] | None = None) -> FastAPI:
    """Create the FastAPI app with both protocol adapters mounted."""
    app = FastAPI(title="hrns")
    shared = agent or build_agent()
    mount_acp(app, (lambda: shared) if agent is not None else build_agent)
    mount_agui(app, shared)
    return app
