"""Keep the built-in ACP server alive independently of terminal clients."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest, token_urlsafe

import acp
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket

from ..local_server import (
    Endpoint,
    publish_daemon_process,
    publish_metadata,
    runtime_directory,
    try_store_lock,
)
from .acp import AcpServer
from .fastapi import _ACP_MESSAGE_LIMIT, _serve_acp

_IDLE_GRACE = 5.0


def serve(
    server: AcpServer[None],
    adapter: Callable[[], acp.Agent],
    store: Path,
    configuration: str,
    *,
    has_pending_runs: Callable[[], bool],
) -> None:
    with try_store_lock(store) as acquired:
        if not acquired:
            return
        directory = runtime_directory(store)
        endpoint_path = directory / "endpoint.json"
        with publish_daemon_process(directory), socket.create_server(("127.0.0.1", 0)) as listener:
            endpoint = Endpoint(
                port=listener.getsockname()[1], token=token_urlsafe(32), configuration=configuration
            )
            _serve(server, adapter, listener, endpoint, endpoint_path, has_pending_runs)


def _serve(
    server: AcpServer[None],
    adapter: Callable[[], acp.Agent],
    listener: socket.socket,
    endpoint: Endpoint,
    endpoint_path: Path,
    has_pending_runs: Callable[[], bool],
) -> None:
    clients = 0
    idle_since: float | None = None

    async def stop_when_idle() -> None:
        nonlocal idle_since
        loop = asyncio.get_running_loop()
        while not runner.should_exit:
            now = loop.time()
            if clients or has_pending_runs():
                idle_since = None
            elif idle_since is None:
                idle_since = now
            elif now - idle_since >= _IDLE_GRACE:
                runner.should_exit = True
                return
            await asyncio.sleep(0.1)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with server:
            with publish_metadata(endpoint_path, endpoint):
                idle_monitor = asyncio.create_task(stop_when_idle())
                try:
                    yield
                finally:
                    idle_monitor.cancel()
                    await asyncio.gather(idle_monitor, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(header: str) -> bool:
        return compare_digest(header.encode(), f"Bearer {endpoint.token}".encode())

    @app.get("/health")
    async def health(request: Request) -> Response:
        nonlocal idle_since
        if not authorized(request.headers.get("authorization", "")):
            return Response(status_code=401)
        if runner.should_exit:
            return Response(status_code=503)
        # Give a launcher that discovered this endpoint time to open its WebSocket.
        idle_since = None
        return Response(status_code=204)

    @app.post("/shutdown")
    async def shutdown(request: Request) -> Response:
        if not authorized(request.headers.get("authorization", "")):
            return Response(status_code=401)
        runner.should_exit = True
        return Response(status_code=202)

    @app.websocket("/acp/agent")
    async def connect(websocket: WebSocket) -> None:
        nonlocal clients, idle_since
        if not authorized(websocket.headers.get("authorization", "")):
            await websocket.close(code=1008)
            return
        if runner.should_exit:
            await websocket.close(code=1012)
            return
        clients += 1
        idle_since = None
        try:
            await _serve_acp(websocket, adapter())
        finally:
            clients -= 1

    runner = uvicorn.Server(uvicorn.Config(app, access_log=False, ws_max_size=_ACP_MESSAGE_LIMIT))
    runner.run(sockets=[listener])
