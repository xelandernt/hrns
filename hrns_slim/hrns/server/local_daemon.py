"""Keep the built-in ACP server alive independently of terminal clients."""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from tempfile import NamedTemporaryFile
from typing import BinaryIO

import acp
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket

from ..local_server import Endpoint, runtime_directory
from .acp import AcpServer
from .fastapi import _serve_acp


def _try_lock(lock: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError:
        if os.name == "nt":
            return False
        raise
    return True


def serve(
    server: AcpServer[None],
    adapter: Callable[[], acp.Agent],
    store: Path,
    configuration: str,
) -> None:
    directory = runtime_directory(store)
    endpoint_path = directory / "endpoint.json"
    with (directory / "server.lock").open("ab") as lock:
        if not _try_lock(lock):
            return
        with socket.create_server(("127.0.0.1", 0)) as listener:
            endpoint = Endpoint(
                port=listener.getsockname()[1], token=token_urlsafe(32), configuration=configuration
            )
            _serve(server, adapter, listener, endpoint, endpoint_path)


def _serve(
    server: AcpServer[None],
    adapter: Callable[[], acp.Agent],
    listener: socket.socket,
    endpoint: Endpoint,
    endpoint_path: Path,
) -> None:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with server:
            with NamedTemporaryFile(dir=endpoint_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(endpoint.model_dump_json().encode())
                    stream.close()
                    temporary.replace(endpoint_path)
                finally:
                    temporary.unlink(missing_ok=True)
            try:
                yield
            finally:
                endpoint_path.unlink(missing_ok=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(header: str) -> bool:
        return compare_digest(header.encode(), f"Bearer {endpoint.token}".encode())

    @app.get("/health")
    async def health(request: Request) -> Response:
        return Response(
            status_code=204 if authorized(request.headers.get("authorization", "")) else 401
        )

    @app.websocket("/acp/agent")
    async def connect(websocket: WebSocket) -> None:
        if not authorized(websocket.headers.get("authorization", "")):
            await websocket.close(code=1008)
            return
        await _serve_acp(websocket, adapter())

    runner = uvicorn.Server(uvicorn.Config(app, access_log=False))
    runner.run(sockets=[listener])
