"""The two client transports, in one module.

* :class:`StdioAgentTransport` launches a local ACP agent as a direct argv
  subprocess (never through a shell). stdout is protocol-only; stderr is
  forwarded line-by-line to the client diagnostics stream.
* :class:`WebSocketAgentTransport` implements the project transport
  ``harness.acp.v1``: one complete UTF-8 JSON-RPC object per text message,
  no batching/binary, 16 MiB per message, 30 s ping/pong, bounded buffering.

The ACP SDK's dispatch layer expects newline-framed asyncio streams, so the
WebSocket transport bridges frames into an ``asyncio.StreamReader`` and a
write-only ``asyncio.StreamWriter``. That keeps all protocol handling inside
the official SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from acp.client.connection import ClientSideConnection
from acp.transports import spawn_stdio_transport
from harness_protocol import HarnessError, decode_frame
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, WebSocketException

from .constants import (
    MAX_MESSAGE_BYTES,
    WS_PING_INTERVAL,
    WS_PING_TIMEOUT,
    WS_SUBPROTOCOL,
    WS_WRITE_LIMIT,
)
from .errors import ProtocolError, TransportError
from .profiles import secret_value

__all__ = [
    "AgentTransport",
    "ConnectionBundle",
    "StdioAgentTransport",
    "WebSocketAgentTransport",
    "check_outbound_size",
]

StderrHandler = Callable[[str], None]
LossHandler = Callable[[str, BaseException | None], None]


def check_outbound_size(data: bytes | str, *, context: str = "message") -> int:
    """Reject an outbound frame larger than the transport limit before sending."""
    size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
    if size > MAX_MESSAGE_BYTES:
        raise TransportError(
            f"The {context} is {size:,} bytes; the ACP transport limit is {MAX_MESSAGE_BYTES:,} bytes. "
            "Remove or shrink the attachment before sending.",
            details={"size": size, "limit": MAX_MESSAGE_BYTES},
        )
    return size


def _default_stderr(line: str) -> None:
    try:
        print(f"agent: {line}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - broken stderr
        pass


class ConnectionBundle:
    """An open transport plus the official SDK connection over it."""

    def __init__(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        handle: Any,
        conn: ClientSideConnection,
        reader_handle: Any = None,
        closer: Callable[[], Any] | None = None,
        name: str = "agent",
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.handle = handle
        self.conn = conn
        self.reader_handle = reader_handle
        self._closer = closer
        self.name = name
        self.closed = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await self.conn.close()
        if self._closer is not None:
            with contextlib.suppress(Exception):
                result = self._closer()
                if asyncio.iscoroutine(result):
                    await result
        if self.reader_handle is not None:
            with contextlib.suppress(Exception):
                await self.reader_handle


class AgentTransport(Protocol):
    """Structural protocol implemented by both transports."""

    name: str

    def describe(self) -> str: ...

    async def open(self, client: Any, *, on_loss: LossHandler | None = None) -> ConnectionBundle: ...

    async def close(self) -> None: ...


class StdioAgentTransport:
    """Launch an independently installed ACP agent over direct argv stdio."""

    def __init__(
        self,
        command: str,
        args: list[str] | tuple[str, ...] = (),
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        env_refs: Mapping[str, str] | None = None,
        stderr_handler: StderrHandler = _default_stderr,
        name: str = "local",
        connect_timeout: float = 20.0,
    ) -> None:
        if not command:
            raise ValueError("A stdio profile needs a command to launch")
        self.command = command
        self.args = list(args)
        self.cwd = cwd or None
        self.env = dict(env or {})
        self.env_refs = dict(env_refs or {})
        self.stderr_handler = stderr_handler
        self.name = name
        self.connect_timeout = connect_timeout
        self._cm: Any = None
        self._bundle: ConnectionBundle | None = None
        self._stderr_tail: list[str] = []
        self._stderr_task: asyncio.Task[None] | None = None
        self._closing = False

    def describe(self) -> str:
        argv = " ".join([_quote(self.command), *( _quote(a) for a in self.args)])
        where = f" (cwd {self.cwd})" if self.cwd else ""
        return f"local stdio · {argv}{where}"

    def resolve_environment(self) -> dict[str, str]:
        resolved = {name: secret_value(ref) or "" for name, ref in self.env_refs.items()}
        resolved.update(self.env)
        return resolved

    async def open(self, client: Any, *, on_loss: LossHandler | None = None) -> ConnectionBundle:
        env = self.resolve_environment()
        try:
            self._cm = spawn_stdio_transport(
                self.command,
                *self.args,
                env=env or None,
                cwd=self.cwd,
            )
            reader, writer, process = await asyncio.wait_for(
                self._cm.__aenter__(), timeout=self.connect_timeout
            )
        except FileNotFoundError as exc:
            raise TransportError(
                f"Cannot launch '{self.command}': executable not found. "
                "Check the profile command, or install the agent in the environment it names.",
                details={"command": self.command},
            ) from exc
        except PermissionError as exc:
            raise TransportError(f"Cannot launch '{self.command}': permission denied.", details={"command": self.command}) from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise TransportError(f"Timed out launching agent process '{self.command}'.") from exc
        except OSError as exc:
            raise TransportError(f"Cannot launch '{self.command}': {exc}") from exc
        self._process = process
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            self._stderr_task = asyncio.create_task(self._pump_stderr(stderr), name="harness-client.agent-stderr")

        async def watch() -> None:
            code = await process.wait()
            if self._closing:
                return
            detail = f"agent process exited with status {code}"
            tail = self._stderr_diagnostic()
            if tail:
                detail += f": {tail}"
            if on_loss is not None:
                on_loss(detail, None)

        watcher = asyncio.create_task(watch(), name="harness-client.stdio-watch")
        conn = ClientSideConnection(client, writer, reader, use_unstable_protocol=True)
        bundle = ConnectionBundle(
            reader=reader,
            writer=writer,
            handle=process,
            conn=conn,
            reader_handle=watcher,
            closer=self._exit_context,
            name=self.name,
        )
        self._bundle = bundle
        return bundle

    async def _pump_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await stream.readline()
            except asyncio.CancelledError:
                return
            except Exception:
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\n")
            self._stderr_tail.append(text)
            del self._stderr_tail[:-20]
            with contextlib.suppress(Exception):
                self.stderr_handler(text)

    def _stderr_diagnostic(self) -> str:
        if not self._stderr_tail:
            return ""
        return " | ".join(self._stderr_tail[-3:])

    async def _exit_context(self) -> None:
        self._closing = True
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await self._stderr_task
            self._stderr_task = None
        cm, self._cm = self._cm, None
        if cm is not None:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    async def close(self) -> None:
        self._closing = True
        if self._bundle is not None:
            await self._bundle.close()
            self._bundle = None


class _WsWriteTransport(asyncio.BaseTransport):
    """Minimal write transport that hands framed bytes to the WebSocket."""

    def __init__(self, enqueue: Callable[[bytes], None]) -> None:
        self._enqueue = enqueue
        self._closing = False

    def write(self, data: bytes) -> None:  # type: ignore[override]
        if not self._closing:
            self._enqueue(bytes(data))

    def can_write_eof(self) -> bool:  # type: ignore[override]
        return False

    def is_closing(self) -> bool:  # type: ignore[override]
        return self._closing

    def close(self) -> None:  # type: ignore[override]
        self._closing = True

    def abort(self) -> None:  # type: ignore[override]
        self.close()


class _WriteProtocol(asyncio.BaseProtocol):
    """Protocol that satisfies ``StreamWriter.drain`` without being paused."""

    def pause_writing(self) -> None:  # pragma: no cover - the queue bounds itself
        pass

    def resume_writing(self) -> None:  # pragma: no cover
        pass

    async def _drain_helper(self) -> None:
        return None


def require_secure_ws_url(url: str) -> None:
    """Reject bearer-bearing plaintext WebSockets except explicit loopback development."""
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        raise TransportError(f"WebSocket URL must start with ws:// or wss://, got '{url}'")
    if parsed.scheme == "wss":
        return
    host = parsed.hostname
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise TransportError(
        "Refusing to send an authorization token over plaintext ws:// to a remote host; "
        "use wss:// (plain ws is allowed only for loopback development)."
    )


class WebSocketAgentTransport:
    """ACP over the versioned ``harness.acp.v1`` WebSocket framing."""

    def __init__(
        self,
        url: str,
        *,
        token_ref: str = "",
        token: str | None = None,
        headers: Mapping[str, str] | None = None,
        name: str = "remote",
        connect_timeout: float = 20.0,
    ) -> None:
        require_secure_ws_url(url)
        if not url.startswith(("ws://", "wss://")):
            raise ValueError(f"WebSocket URL must start with ws:// or wss://, got '{url}'")
        self.url = url
        self.token_ref = token_ref
        self._token = token
        self.headers = dict(headers or {})
        self.name = name
        self.connect_timeout = connect_timeout
        self._ws: ClientConnection | None = None
        self._bundle: ConnectionBundle | None = None
        self._closing = False

    def describe(self) -> str:
        return f"remote · {self.url}"

    def _auth_headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        token = self._token if self._token is not None else (secret_value(self.token_ref) if self.token_ref else None)
        if token:
            headers.setdefault("Authorization", f"Bearer {token}")
        return headers

    async def open(self, client: Any, *, on_loss: LossHandler | None = None) -> ConnectionBundle:
        if self._ws is not None:
            raise TransportError("This WebSocket transport is already connected; create a new one to reconnect.")
        try:
            ws = await asyncio.wait_for(
                ws_connect(
                    self.url,
                    subprotocols=[WS_SUBPROTOCOL],
                    max_size=MAX_MESSAGE_BYTES,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    write_limit=WS_WRITE_LIMIT,
                    additional_headers=self._auth_headers(),
                    open_timeout=self.connect_timeout,
                ),
                timeout=self.connect_timeout + 2,
            )
        except InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise TransportError(
                f"The server rejected the WebSocket upgrade{f' (HTTP {status})' if status else ''}. "
                "Check the bearer token and agent id.",
                details={"status": status},
            ) from exc
        except InvalidURI as exc:
            raise TransportError(f"Invalid WebSocket URL: {self.url}") from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise TransportError(f"Timed out connecting to {self.url}") from exc
        except OSError as exc:
            raise TransportError(f"Cannot reach {self.url}: {exc}") from exc
        except WebSocketException as exc:
            raise TransportError(f"WebSocket connection failed: {exc}") from exc

        selected = getattr(ws, "subprotocol", None)
        if selected != WS_SUBPROTOCOL:
            await ws.close()
            raise ProtocolError(
                f"The server did not accept the '{WS_SUBPROTOCOL}' subprotocol"
                f"{f' (selected {selected!r})' if selected else ''}. Is this a Harness ACP endpoint?",
                details={"selected": selected or ""},
            )
        self._ws = ws
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
        outbox: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        pending = bytearray()
        closed = asyncio.Event()
        overflow_tasks: set[asyncio.Task[None]] = set()

        async def close_stalled() -> None:
            with contextlib.suppress(Exception):
                await ws.close(code=1008, reason="outbound buffer overflow")

        def enqueue(data: bytes) -> None:
            nonlocal pending
            pending.extend(data)
            while True:
                index = pending.find(b"\n")
                if index < 0:
                    break
                frame = bytes(pending[:index])
                del pending[: index + 1]
                if not frame.strip():
                    continue
                check_outbound_size(frame, context="outbound ACP message")
                if outbox.full():
                    # A persistently stalled consumer must lose the connection
                    # instead of growing an unbounded client-side buffer.
                    task = asyncio.create_task(
                        close_stalled(), name="harness-client.ws-overflow-close"
                    )
                    overflow_tasks.add(task)
                    task.add_done_callback(overflow_tasks.discard)
                    raise TransportError(
                        "Outbound buffering is full; the server is not consuming the stream. Disconnecting."
                    )
                outbox.put_nowait(frame)

        async def send_loop() -> None:
            try:
                while True:
                    frame = await outbox.get()
                    if frame is None:
                        return
                    if len(frame) > MAX_MESSAGE_BYTES:
                        await ws.close(code=1009, reason="message too large")
                        return
                    await ws.send(frame.decode("utf-8"))
            except asyncio.CancelledError:
                return
            except ConnectionClosed:
                return

        async def receive_loop() -> None:
            reason = "server closed the connection"
            try:
                while True:
                    try:
                        message = await ws.recv()
                    except ConnectionClosed as exc:
                        received = getattr(exc, "rcvd", None)
                        code = getattr(received, "code", None)
                        reason = f"WebSocket closed (code {code})" if code else "WebSocket closed"
                        if code == 1009:
                            reason = (
                                "The server closed with status 1009: a message exceeded the "
                                "16 MiB transport limit."
                            )
                        break
                    if isinstance(message, bytes):
                        raise ProtocolError(
                            "The harness WebSocket transport accepts text JSON frames only; got binary."
                        )
                    try:
                        # Exactly one complete JSON-RPC object per text frame:
                        # newline batching, malformed JSON and non-RPC payloads
                        # are transport errors, not several ACP messages.
                        decode_frame(message)
                    except (HarnessError, ValueError) as exc:
                        raise ProtocolError(
                            f"The server sent an invalid harness.acp.v1 frame: {exc}"
                        ) from exc
                    reader.feed_data(message.encode("utf-8") + b"\n")
            except ProtocolError as exc:
                reason = str(exc)
                # Close the framed stream instead of injecting an exception:
                # the ACP SDK turns EOF into a prompt failure for every pending
                # request, while a raw reader exception can leave them pending.
            except WebSocketException as exc:
                reason = f"WebSocket error: {exc}"
            finally:
                reader.feed_eof()
                closed.set()
                if on_loss is not None and not self._closing:
                    on_loss(reason, None)

        sender = asyncio.create_task(send_loop(), name="harness-client.ws-send")
        receiver = asyncio.create_task(receive_loop(), name="harness-client.ws-receive")
        writer_transport = _WsWriteTransport(enqueue)
        writer = asyncio.StreamWriter(writer_transport, _WriteProtocol(), None, loop)  # type: ignore[arg-type]

        async def close_transport() -> None:
            self._closing = True
            with contextlib.suppress(Exception):
                outbox.put_nowait(None)
            for task in (sender, receiver):
                task.cancel()
            for task in (sender, receiver):
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(Exception):
                await ws.close(code=1000)
            closed.set()

        conn = ClientSideConnection(client, writer, reader, use_unstable_protocol=True)
        bundle = ConnectionBundle(
            reader=reader,
            writer=writer,
            handle=ws,
            conn=conn,
            reader_handle=closed.wait(),
            closer=close_transport,
            name=self.name,
        )
        self._bundle = bundle
        return bundle

    async def close(self) -> None:
        if self._bundle is not None:
            await self._bundle.close()
            self._bundle = None


def _quote(value: str) -> str:
    if not value or any(char.isspace() for char in value) or '"' in value:
        return json.dumps(value)
    return value
