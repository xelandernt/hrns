"""Mount ACP on a FastAPI application."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import TypeAlias, TypeVar

import acp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic_ai.agent import AbstractAgent

from .acp import ModelResolver, PydanticAIACPAgent, SessionStore, create_acp_agent
from .agent import build_agent

__all__ = [
    "AgentFactory",
    "Authenticator",
    "SessionStoreFactory",
    "create_app",
    "mount_acp",
]

Principal = TypeVar("Principal")
"""The identity type produced by an :data:`Authenticator`."""

AgentFactory: TypeAlias = Callable[[Principal | None], AbstractAgent[None, str]]
"""Build the agent for one connection or request.

The factory always takes exactly one argument: the authenticated principal, or
``None`` when no authenticator is configured.
"""

Authenticator: TypeAlias = Callable[[WebSocket], Awaitable[Principal | None]]
"""Resolve the principal for an incoming connection.

Return the principal to admit it, or ``None`` to reject it. Raise to report a
server-side failure; the exception is never turned into an accepted connection.
"""

SessionStoreFactory: TypeAlias = Callable[[Principal | None], SessionStore]
"""Choose the ACP session store for one connection.

Return the store that belongs to the principal. Sessions saved in one store are
invisible to every other principal.
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
    connection: WebSocket,
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
    session_store_factory: SessionStoreFactory[Principal] | None = None,
    model_resolver: ModelResolver | None = None,
) -> None:
    """Expose an ACP agent over a WebSocket endpoint.

    Authentication runs before the WebSocket is accepted. A rejected peer gets
    close code ``1008`` and its factory is never called. Each accepted WebSocket
    gets its own adapter.

    Pass ``session_store_factory`` to keep sessions per principal: the store it
    returns for one connection is the only store that connection can load from.
    Without it, every connection starts with a fresh in-memory store.

    Pass ``model_resolver`` to advertise ACP's ``model`` session config option and
    the ``providers/*`` methods. It supplies the advertised models, resolves a
    selection, advertises the setting options each model accepts (reasoning effort,
    for example), and owns the provider routing a client may change; one resolver is
    shared by every connection.
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
        store = session_store_factory(principal) if session_store_factory is not None else None
        adapter = create_acp_agent(
            agent_factory(principal),
            session_store=store,
            model_resolver=model_resolver,
        )
        await _serve_acp(websocket, adapter)


def _default_agent_factory(_principal: object) -> AbstractAgent[None, str]:
    return build_agent()


def create_app(
    agent_factory: AgentFactory[Principal] | None = None,
    *,
    authenticate: Authenticator[Principal] | None = None,
    session_store_factory: SessionStoreFactory[Principal] | None = None,
    model_resolver: ModelResolver | None = None,
) -> FastAPI:
    """Create the FastAPI app with the ACP adapter mounted.

    Without an ``agent_factory`` the packaged :func:`build_agent` default is
    used. Without an ``authenticate`` callback every peer is admitted and the
    factory receives ``None`` as its principal. ``session_store_factory`` and
    ``model_resolver`` are forwarded to the ACP mount, which uses the resolver for
    model switching, session settings, and provider configuration.
    """
    app = FastAPI(title="hrns")
    factory: AgentFactory[Principal] = agent_factory or _default_agent_factory
    mount_acp(
        app,
        factory,
        authenticate=authenticate,
        session_store_factory=session_store_factory,
        model_resolver=model_resolver,
    )
    return app
