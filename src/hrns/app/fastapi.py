"""Mount ACP and AG-UI on one FastAPI application."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import TypeAlias, TypeVar

import acp
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic_ai.agent import AbstractAgent
from starlette.requests import Request
from starlette.responses import Response

from .acp import PydanticAIACPAgent, create_acp_agent
from .agent import build_agent
from .agui import PydanticAIAGUIAgent

__all__ = [
    "AgentFactory",
    "Authenticator",
    "create_app",
    "mount_acp",
    "mount_agui",
]

Principal = TypeVar("Principal")
"""The identity type produced by an :data:`Authenticator`."""

AgentFactory: TypeAlias = Callable[[Principal | None], AbstractAgent[None, str]]
"""Build the agent for one connection or request.

The factory always takes exactly one argument: the authenticated principal, or
``None`` when no authenticator is configured.
"""

Authenticator: TypeAlias = Callable[[WebSocket | Request], Awaitable[Principal | None]]
"""Resolve the principal for an incoming connection or request.

Return the principal to admit it, or ``None`` to reject it. Raise to report a
server-side failure; the exception is never turned into an accepted connection.
"""


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


async def _authorize(
    connection: WebSocket | Request,
    authenticate: Authenticator[Principal] | None,
) -> tuple[bool, Principal | None]:
    """Run the authenticator and report whether the peer may proceed."""
    if authenticate is None:
        return True, None
    principal = await authenticate(connection)
    return principal is not None, principal


def mount_acp(
    app: FastAPI,
    agent_factory: AgentFactory[Principal],
    *,
    path: str = "/acp",
    authenticate: Authenticator[Principal] | None = None,
) -> None:
    """Expose an ACP agent over a WebSocket endpoint.

    Authentication runs before the WebSocket is accepted. A rejected peer gets
    close code ``1008`` and its factory is never called. Each accepted WebSocket
    gets its own adapter and session namespace.
    """

    @app.websocket(path)
    async def acp_endpoint(websocket: WebSocket) -> None:
        try:
            admitted, principal = await _authorize(websocket, authenticate)
        except Exception:
            with suppress(Exception):
                await websocket.close(code=1011)
            raise
        if not admitted:
            await websocket.close(code=1008)
            return
        await _serve_acp(websocket, create_acp_agent(agent_factory(principal)))


def mount_agui(
    app: FastAPI,
    agent_factory: AgentFactory[Principal],
    *,
    path: str = "/agui",
    authenticate: Authenticator[Principal] | None = None,
) -> None:
    """Expose an agent over an AG-UI POST endpoint.

    Authentication runs before the agent is built. A rejected request gets
    ``401`` and its factory is never called. Each accepted request builds one
    agent and dispatches one AG-UI run.
    """

    @app.post(path)
    async def agui_endpoint(request: Request) -> Response:
        admitted, principal = await _authorize(request, authenticate)
        if not admitted:
            raise HTTPException(status_code=401, detail="Unauthorized")
        adapter = PydanticAIAGUIAgent(agent_factory(principal))
        return await adapter.dispatch_request(request)


def _default_agent_factory(_principal: object) -> AbstractAgent[None, str]:
    return build_agent()


def create_app(
    agent_factory: AgentFactory[Principal] | None = None,
    *,
    authenticate: Authenticator[Principal] | None = None,
) -> FastAPI:
    """Create the FastAPI app with both protocol adapters mounted.

    Without an ``agent_factory`` the packaged :func:`build_agent` default is
    used. Without an ``authenticate`` callback every peer is admitted and the
    factory receives ``None`` as its principal.
    """
    app = FastAPI(title="hrns")
    factory: AgentFactory[Principal] = agent_factory or _default_agent_factory
    mount_acp(app, factory, authenticate=authenticate)
    mount_agui(app, factory, authenticate=authenticate)
    return app
