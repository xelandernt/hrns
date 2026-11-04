"""The stdio client transport."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from acp.client.connection import ClientSideConnection
from acp.transports import spawn_stdio_transport

from .errors import TransportError
from .profiles import secret_value

__all__ = ["AgentTransport", "ConnectionBundle", "StdioAgentTransport"]

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
