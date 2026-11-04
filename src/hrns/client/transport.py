"""The stdio and WebSocket client transports."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import sys
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

from acp.client.connection import ClientSideConnection
from acp.transports import spawn_stdio_transport
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, WebSocketException

from .constants import MAX_MESSAGE_BYTES, WS_PING_INTERVAL, WS_PING_TIMEOUT, WS_WRITE_LIMIT
from .errors import ProtocolError, TransportError
from .profiles import secret_value

__all__ = [
    "AgentTransport",
    "ConnectionBundle",
    "StdioAgentTransport",
    "WebSocketAgentTransport",
    "check_websocket_url",
]

StderrHandler = Callable[[str], None]
LossHandler = Callable[[str, BaseException | None], None]


def _default_stderr(line: str) -> None:
    try:
        print(f"agent: {line}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - broken stderr
        pass


def _quote(value: str) -> str:
    return f'"{value}"' if any(char.isspace() for char in value) else value


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
    """Structural protocol implemented by the stdio transport."""

    name: str

    def describe(self) -> str: ...

    async def open(
        self, client: Any, *, on_loss: LossHandler | None = None
    ) -> ConnectionBundle: ...

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
        argv = " ".join([_quote(self.command), *(_quote(a) for a in self.args)])
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
            raise TransportError(
                f"Cannot launch '{self.command}': permission denied.",
                details={"command": self.command},
            ) from exc
        except TimeoutError as exc:
            raise TransportError(f"Timed out launching agent process '{self.command}'.") from exc
        except OSError as exc:
            raise TransportError(f"Cannot launch '{self.command}': {exc}") from exc
        self._process = process
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._pump_stderr(stderr), name="hrns-client.agent-stderr"
            )

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

        watcher = asyncio.create_task(watch(), name="hrns-client.stdio-watch")
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


def check_websocket_url(url: str) -> str:
    """Return *url* when it is a usable ACP WebSocket endpoint.

    A bearer token must never cross the network in plaintext, so ``ws://`` is
    accepted only for loopback development; everything else needs ``wss://``.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise TransportError(f"WebSocket URL must start with ws:// or wss://, got '{url}'")
    if parsed.scheme == "ws" and not _is_loopback_host(parsed.hostname):
        raise TransportError(
            "Refusing plaintext ws:// to a remote host; use wss://. "
            "Plain ws:// is allowed only for loopback development."
        )
    return url


def _is_loopback_host(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback


class WebSocketAgentTransport:
    """ACP over a WebSocket endpoint, one JSON-RPC message per text frame.

    The ACP SDK expects newline-framed asyncio streams, so a socketpair bridges
    the WebSocket to a real ``StreamReader``/``StreamWriter`` pair.
    """

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
        self.url = check_websocket_url(url)
        self.token_ref = token_ref
        self._token = token
        self.headers = dict(headers or {})
        self.name = name
        self.connect_timeout = connect_timeout
        self._bundle: ConnectionBundle | None = None
        self._closing = False

    def describe(self) -> str:
        return f"remote · {self.url}"

    def _auth_headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        token = (
            self._token
            if self._token is not None
            else (secret_value(self.token_ref) if self.token_ref else None)
        )
        if token:
            headers.setdefault("Authorization", f"Bearer {token}")
        return headers

    async def open(self, client: Any, *, on_loss: LossHandler | None = None) -> ConnectionBundle:
        if self._bundle is not None:
            raise TransportError(
                "This WebSocket transport is already connected; create a new one to reconnect."
            )
        try:
            ws = await asyncio.wait_for(
                ws_connect(
                    self.url,
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
                "Check the bearer token and the endpoint URL.",
                details={"status": status},
            ) from exc
        except InvalidURI as exc:
            raise TransportError(f"Invalid WebSocket URL: {self.url}") from exc
        except TimeoutError as exc:
            raise TransportError(f"Timed out connecting to {self.url}") from exc
        except OSError as exc:
            raise TransportError(f"Cannot reach {self.url}: {exc}") from exc
        except WebSocketException as exc:
            raise TransportError(f"WebSocket connection failed: {exc}") from exc
        try:
            return await self._bridge(client, ws, on_loss=on_loss)
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise

    async def _bridge(
        self,
        client: Any,
        ws: ClientConnection,
        *,
        on_loss: LossHandler | None,
    ) -> ConnectionBundle:
        local, remote = socket.socketpair()
        local.setblocking(False)
        remote.setblocking(False)
        local_reader, local_writer = await asyncio.open_connection(
            sock=local, limit=MAX_MESSAGE_BYTES
        )
        remote_reader, remote_writer = await asyncio.open_connection(
            sock=remote, limit=MAX_MESSAGE_BYTES
        )
        closed = asyncio.Event()
        reason = "WebSocket closed"

        async def to_websocket() -> None:
            try:
                while True:
                    line = await remote_reader.readline()
                    if not line:
                        return
                    text = line.decode("utf-8", "replace").rstrip("\n")
                    if text.strip():
                        await ws.send(text)
            except asyncio.CancelledError:
                raise
            except ValueError:
                await ws.close(code=1009, reason="message too large")
            except (ConnectionClosed, OSError):
                return

        async def from_websocket() -> None:
            nonlocal reason
            try:
                while True:
                    try:
                        message = await ws.recv()
                    except ConnectionClosed as exc:
                        received = getattr(exc, "rcvd", None)
                        code = getattr(received, "code", None)
                        reason = f"WebSocket closed (code {code})" if code else "WebSocket closed"
                        break
                    if isinstance(message, bytes):
                        raise ProtocolError(
                            "The WebSocket ACP transport accepts text JSON frames only."
                        )
                    remote_writer.write(message.encode("utf-8") + b"\n")
                    await remote_writer.drain()
            except asyncio.CancelledError:
                raise
            except ProtocolError as exc:
                reason = exc.message
            except WebSocketException as exc:
                reason = f"WebSocket error: {exc}"
            except (ConnectionError, OSError) as exc:
                reason = f"WebSocket stream closed: {exc}"
            finally:
                with contextlib.suppress(Exception):
                    remote_writer.close()
                closed.set()
                if on_loss is not None and not self._closing:
                    on_loss(reason, None)

        sender = asyncio.create_task(to_websocket(), name="hrns-client.ws-send")
        receiver = asyncio.create_task(from_websocket(), name="hrns-client.ws-receive")

        async def close_transport() -> None:
            self._closing = True
            for task in (sender, receiver):
                task.cancel()
            for task in (sender, receiver):
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(Exception):
                await ws.close(code=1000)
            with contextlib.suppress(Exception):
                remote_writer.close()
            with contextlib.suppress(Exception):
                local_writer.close()
            closed.set()

        conn = ClientSideConnection(client, local_writer, local_reader, use_unstable_protocol=True)
        bundle = ConnectionBundle(
            reader=local_reader,
            writer=local_writer,
            handle=ws,
            conn=conn,
            reader_handle=closed.wait(),
            closer=close_transport,
            name=self.name,
        )
        self._bundle = bundle
        return bundle

    async def close(self) -> None:
        self._closing = True
        if self._bundle is not None:
            await self._bundle.close()
            self._bundle = None
