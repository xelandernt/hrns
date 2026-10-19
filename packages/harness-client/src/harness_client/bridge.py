"""Stdio ACP bridge to a hosted agent (``harness bridge``).

The bridge is a framing adapter: newline-delimited ACP JSON-RPC on stdin and
stdout, one complete JSON object per WebSocket text message on the remote
side. Any stdio-only ACP client can launch it.

With ``--remote-cwd``, the local client's ``cwd`` on ``session/new``,
``session/load`` and ``session/resume`` is *replaced* with the remote
directory. Nothing else is rewritten; paths returned by the agent remain
clearly remote.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from harness_protocol import HarnessError, decode_frame, encode_frame
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .constants import (
    MAX_MESSAGE_BYTES,
    WS_PING_INTERVAL,
    WS_PING_TIMEOUT,
    WS_SUBPROTOCOL,
    WS_WRITE_LIMIT,
)
from .errors import HarnessClientError, ProtocolError
from .profiles import secret_value
from .transport import require_secure_ws_url

__all__ = ["BridgeSession", "rewrite_session_cwd", "run_bridge", "run_bridge_pump"]

SESSION_METHODS = frozenset({"session/new", "session/load", "session/resume"})

EXIT_BRIDGE_OK = 0
EXIT_BRIDGE_FAILURE = 1
EXIT_BRIDGE_PROTOCOL = 4


@dataclass(frozen=True, slots=True)
class BridgeSession:
    """Everything needed to reach the hosted agent."""

    url: str  # ws(s) URL of /v1/agents/{id}/acp
    token_ref: str = ""
    token: str | None = None
    remote_cwd: str | None = None
    agent_id: str = ""

    def __post_init__(self) -> None:
        require_secure_ws_url(self.url)

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        token = self.token if self.token is not None else (secret_value(self.token_ref) if self.token_ref else None)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


def rewrite_session_cwd(message: dict[str, Any], remote_cwd: str) -> bool:
    """Replace ``params.cwd`` on ACP session setup/load/resume. Returns True if rewritten."""
    if message.get("method") not in SESSION_METHODS:
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    params["cwd"] = remote_cwd
    return True


async def _default_ws_connect(session: BridgeSession) -> ClientConnection:
    return await ws_connect(
        session.url,
        subprotocols=[WS_SUBPROTOCOL],
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=WS_PING_INTERVAL,
        ping_timeout=WS_PING_TIMEOUT,
        write_limit=WS_WRITE_LIMIT,
        additional_headers=session.headers(),
    )


async def run_bridge_pump(
    read_line: Callable[[], Awaitable[bytes]],
    write_local: Callable[[bytes], Awaitable[None]],
    connect_remote: Callable[[], Awaitable[ClientConnection]],
    *,
    remote_cwd: str | None = None,
    stderr: Callable[[str], None] | None = None,
    max_remote_seconds: float = 5.0,
) -> int:
    """Run both directions until stdin closes or the remote side goes away."""
    report = stderr or (lambda line: print(f"bridge: {line}", file=sys.stderr, flush=True))
    try:
        ws = await connect_remote()
    except InvalidStatus as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        report(f"the server rejected the connection{f' (HTTP {status})' if status else ''}")
        return EXIT_BRIDGE_FAILURE
    except (OSError, WebSocketException, asyncio.TimeoutError, TimeoutError) as exc:
        report(f"cannot connect to the remote agent: {exc}")
        return EXIT_BRIDGE_FAILURE
    selected = getattr(ws, "subprotocol", None)
    if selected != WS_SUBPROTOCOL:
        report(
            f"the server did not accept the '{WS_SUBPROTOCOL}' subprotocol"
            + (f" (selected {selected!r})" if selected else "")
        )
        await ws.close()
        return EXIT_BRIDGE_PROTOCOL

    rewritten = False
    remote_error = ""

    async def local_to_remote() -> None:
        nonlocal rewritten
        while True:
            line = await read_line()
            if not line:
                return
            try:
                message = decode_frame(line)
            except (HarnessClientError, HarnessError) as exc:
                report(f"invalid local ACP frame: {exc}")
                raise ProtocolError(str(exc)) from exc
            if remote_cwd and rewrite_session_cwd(message, remote_cwd):
                rewritten = True
            wire = encode_frame(message)
            if len(wire.encode("utf-8")) > MAX_MESSAGE_BYTES:
                report("local message exceeds the 16 MiB transport limit")
                raise ProtocolError("message too large")
            await ws.send(wire)

    async def remote_to_local() -> None:
        nonlocal remote_error
        try:
            while True:
                try:
                    message = await ws.recv()
                except ConnectionClosed as exc:
                    received = getattr(exc, "rcvd", None)
                    code = getattr(received, "code", None)
                    remote_error = f"remote connection closed{f' (code {code})' if code else ''}"
                    if code == 1009:
                        remote_error = "the server closed with status 1009 after an oversized message"
                    return
                if isinstance(message, bytes):
                    report("remote sent a binary WebSocket frame; ACP frames must be text")
                    raise ProtocolError("binary frame")
                try:
                    decode_frame(message)
                except (HarnessClientError, HarnessError) as exc:
                    report(f"invalid remote ACP frame: {exc}")
                    raise ProtocolError(str(exc)) from exc
                await write_local(message.rstrip().encode("utf-8") + b"\n")
        except WebSocketException as exc:
            remote_error = f"remote transport failed: {exc}"

    local_task = asyncio.create_task(local_to_remote(), name="harness-bridge-local")
    remote_task = asyncio.create_task(remote_to_local(), name="harness-bridge-remote")
    errors: list[BaseException] = []
    try:
        done, _pending = await asyncio.wait(
            {local_task, remote_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if remote_task in done:
            # Give a final in-flight local write a moment, then stop.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(local_task, timeout=0.2)
        else:
            # Local EOF: allow trailing responses already in flight, then close.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(remote_task, timeout=0.2)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                errors.append(exc)
    finally:
        for task in (local_task, remote_task):
            task.cancel()
        for task in (local_task, remote_task):
            with contextlib.suppress(BaseException):
                await task
        with contextlib.suppress(Exception):
            await ws.close(code=1000)
    if rewritten:
        report(f"session cwd rewritten to remote path {remote_cwd}")
    if errors:
        report(str(errors[0]))
        return EXIT_BRIDGE_PROTOCOL
    if remote_error:
        report(remote_error)
        return EXIT_BRIDGE_FAILURE
    return EXIT_BRIDGE_OK


async def run_bridge(
    session: BridgeSession,
    *,
    reader: asyncio.StreamReader | None = None,
    writer: asyncio.StreamWriter | None = None,
    stderr: Callable[[str], None] | None = None,
) -> int:
    """Run the bridge over real (or injected) stdio streams."""
    own_streams = reader is None and writer is None
    if own_streams:
        from acp.stdio import stdio_streams

        reader, writer = await stdio_streams(limit=MAX_MESSAGE_BYTES)
    assert reader is not None and writer is not None

    async def read_line() -> bytes:
        return await reader.readline()

    async def write_local(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async def connect_remote() -> ClientConnection:
        return await _default_ws_connect(session)

    try:
        return await run_bridge_pump(
            read_line,
            write_local,
            connect_remote,
            remote_cwd=session.remote_cwd,
            stderr=stderr,
        )
    finally:
        if own_streams:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
