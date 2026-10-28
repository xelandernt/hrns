"""Agent-to-client I/O: filesystem reads and writes, and terminal commands.

ACP lets an agent ask the *client* to do the work: ``fs/read_text_file``,
``fs/write_text_file``, and the ``terminal/*`` lifecycle. This client answers those requests
inside one session's working directory when the operator opts into ``client_io``. It is a
convenience boundary that keeps the agent's I/O next to the files, not a sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from acp import RequestError
from acp.schema import (
    CreateTerminalResponse,
    EnvVariable,
    ReadTextFileResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)

from hrns.protocol import FS_READ_TEXT_FILE, FS_WRITE_TEXT_FILE, TERMINAL
from hrns.protocol.paths import read_only_path

__all__ = [
    "ClientIo",
    "ClientIoMode",
    "ClientIoPolicy",
    "MAX_FILE_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_TERMINALS",
    "SERVED_CLIENT_CAPABILITIES",
    "parse_client_io",
]

ClientIoMode = Literal["off", "read", "all"]
"""What this client offers agents: nothing, filesystem reads, or everything it can serve."""

#: Every client capability path this terminal knows how to answer.
SERVED_CLIENT_CAPABILITIES: frozenset[str] = frozenset(
    {FS_READ_TEXT_FILE, FS_WRITE_TEXT_FILE, TERMINAL}
)
#: The read-only subset, which is all the ``read`` mode advertises.
_READ_ONLY_CAPABILITIES: frozenset[str] = frozenset({FS_READ_TEXT_FILE})

#: Largest file answered in full. Bigger reads are refused rather than silently truncated.
MAX_FILE_BYTES = 2 * 1024 * 1024
#: Most bytes of one terminal's output retained for later polling.
MAX_OUTPUT_BYTES = 1024 * 1024
#: Most terminals kept open at once, across the connection.
MAX_TERMINALS = 8

_READ_CHUNK = 64 * 1024

#: Errors swallowed while stopping a terminal that may already be gone.
_SUPPRESS = contextlib.suppress(ProcessLookupError, RuntimeError, OSError)


def parse_client_io(value: str) -> ClientIoMode:
    """The mode a profile or flag spelled, defaulting to offering nothing."""
    if value == "read":
        return "read"
    if value == "all":
        return "all"
    return "off"


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientIoPolicy:
    """What this terminal answers, and how much of it.

    ``off`` advertises nothing, ``read`` advertises the read-only subset, and ``all``
    advertises everything in :data:`SERVED_CLIENT_CAPABILITIES`. The mode is a convenience
    over a set of capability paths, so what an agent may use is decided by path membership
    and a new family of handlers joins the set instead of adding a branch here. Terminal
    support means an agent may run arbitrary commands as this user with no approval prompt:
    ACP has no permission request for it, so the choice is the operator's and belongs in the
    connection profile.
    """

    mode: ClientIoMode = "off"
    max_file_bytes: int = MAX_FILE_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_terminals: int = MAX_TERMINALS

    @property
    def offered(self) -> frozenset[str]:
        """The capability paths this policy advertises."""
        if self.mode == "all":
            return SERVED_CLIENT_CAPABILITIES
        if self.mode == "read":
            return _READ_ONLY_CAPABILITIES
        return frozenset()

    def offers(self, path: str) -> bool:
        """Whether this policy advertises the capability at ``path``."""
        return path in self.offered


@dataclass
class _Terminal:
    """One command an agent started, and the output captured so far."""

    session_id: str
    process: asyncio.subprocess.Process
    limit: int
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    status: TerminalExitStatus | None = None
    drain: asyncio.Task[None] | None = None


class ClientIo:
    """The filesystem and terminal endpoints this client exposes to a connected agent."""

    def __init__(self, policy: ClientIoPolicy | None = None) -> None:
        self.policy = policy or ClientIoPolicy()
        self._roots: dict[str, Path] = {}
        self._terminals: dict[str, _Terminal] = {}

    # --- session roots ------------------------------------------------

    def remember(self, session_id: str, cwd: str) -> None:
        """Record the directory this session's agent may reach."""
        if not self.policy.offered:
            return
        try:
            self._roots[session_id] = Path(cwd).expanduser().resolve()
        except OSError:
            return

    async def close(self) -> None:
        """Kill every terminal still running, then forget the session roots."""
        for terminal in list(self._terminals.values()):
            self._terminate(terminal)
        for terminal in list(self._terminals.values()):
            if terminal.drain is not None:
                with _SUPPRESS:
                    await asyncio.shield(terminal.drain)
        self._terminals.clear()
        self._roots.clear()

    # --- filesystem ---------------------------------------------------

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
    ) -> ReadTextFileResponse:
        if not self.policy.offers(FS_READ_TEXT_FILE):
            raise RequestError.method_not_found("fs/read_text_file")
        target = self._resolve(session_id, path)
        try:
            size = await asyncio.to_thread(_file_size, target)
        except OSError as exc:
            raise RequestError.invalid_params({"path": path, "reason": exc.strerror}) from exc
        if size > self.policy.max_file_bytes:
            raise RequestError.invalid_params(
                {
                    "path": path,
                    "reason": "file is larger than this client serves in one read",
                    "size": size,
                    "limit": self.policy.max_file_bytes,
                }
            )
        try:
            content = await asyncio.to_thread(_read_text, target)
        except OSError as exc:
            raise RequestError.invalid_params({"path": path, "reason": exc.strerror}) from exc
        if line is not None or limit is not None:
            content = _window(content, line=line, limit=limit)
        return ReadTextFileResponse(content=content)

    async def write_text_file(self, session_id: str, path: str, content: str) -> None:
        if not self.policy.offers(FS_WRITE_TEXT_FILE):
            raise RequestError.method_not_found("fs/write_text_file")
        target = self._resolve(session_id, path)
        self._check_writable(session_id, target, path)
        try:
            await asyncio.to_thread(_write_text, target, content)
        except OSError as exc:
            raise RequestError.invalid_params({"path": path, "reason": exc.strerror}) from exc

    # --- terminals ----------------------------------------------------

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
    ) -> CreateTerminalResponse:
        if not self.policy.offers(TERMINAL):
            raise RequestError.method_not_found("terminal/create")
        if len(self._terminals) >= self.policy.max_terminals:
            raise RequestError.invalid_params(
                {"reason": "too many terminals are open", "limit": self.policy.max_terminals}
            )
        root = self._root(session_id)
        working = self._resolve(session_id, cwd) if cwd else root
        environment = dict(os.environ)
        for variable in env or []:
            environment[variable.name] = variable.value
        limit = min(output_byte_limit or MAX_OUTPUT_BYTES, self.policy.max_output_bytes)
        argv = [command, *args] if args else [command]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(working),
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise RequestError.invalid_params({"command": command, "reason": exc.strerror}) from exc
        terminal = _Terminal(session_id=session_id, process=process, limit=limit)
        terminal.drain = asyncio.create_task(_drain(terminal))
        terminal_id = uuid.uuid4().hex
        self._terminals[terminal_id] = terminal
        return CreateTerminalResponse(terminal_id=terminal_id)

    async def terminal_output(self, session_id: str, terminal_id: str) -> TerminalOutputResponse:
        if not self.policy.offers(TERMINAL):
            raise RequestError.method_not_found("terminal/output")
        terminal = self._terminal(session_id, terminal_id)
        return TerminalOutputResponse(
            output=terminal.output.decode("utf-8", errors="replace"),
            truncated=terminal.truncated,
            exit_status=terminal.status,
        )

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str
    ) -> WaitForTerminalExitResponse:
        if not self.policy.offers(TERMINAL):
            raise RequestError.method_not_found("terminal/wait_for_exit")
        terminal = self._terminal(session_id, terminal_id)
        if terminal.drain is not None:
            await asyncio.shield(terminal.drain)
        status = terminal.status
        if status is None:
            raise RequestError.invalid_params({"terminalId": terminal_id, "reason": "no status"})
        return WaitForTerminalExitResponse(exit_code=status.exit_code, signal=status.signal)

    async def kill_terminal(self, session_id: str, terminal_id: str) -> None:
        if not self.policy.offers(TERMINAL):
            raise RequestError.method_not_found("terminal/kill")
        self._terminate(self._terminal(session_id, terminal_id))

    async def release_terminal(self, session_id: str, terminal_id: str) -> None:
        if not self.policy.offers(TERMINAL):
            raise RequestError.method_not_found("terminal/release")
        terminal = self._terminal(session_id, terminal_id)
        self._terminate(terminal)
        if terminal.drain is not None:
            with _SUPPRESS:
                await asyncio.shield(terminal.drain)
        self._terminals.pop(terminal_id, None)

    # --- internals ----------------------------------------------------

    def _root(self, session_id: str) -> Path:
        root = self._roots.get(session_id)
        if root is None:
            raise RequestError.invalid_params(
                {"sessionId": session_id, "reason": "no working directory is known"}
            )
        return root

    def _resolve(self, session_id: str, path: str | None) -> Path:
        root = self._root(session_id)
        if not path:
            return root
        candidate = Path(path).expanduser()
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise RequestError.invalid_params(
                {"path": str(path), "reason": "outside the session working directory"}
            )
        return resolved

    def _check_writable(self, session_id: str, target: Path, path: str) -> None:
        relative = target.relative_to(self._root(session_id)).as_posix()
        if read_only_path(relative):
            raise RequestError.invalid_params(
                {"path": path, "reason": "version-control, credential, and key paths are read-only"}
            )

    def _terminal(self, session_id: str, terminal_id: str) -> _Terminal:
        terminal = self._terminals.get(terminal_id)
        if terminal is None or terminal.session_id != session_id:
            raise RequestError.invalid_params(
                {"terminalId": terminal_id, "reason": "no such terminal"}
            )
        return terminal

    @staticmethod
    def _terminate(terminal: _Terminal) -> None:
        if terminal.process.returncode is not None:
            return
        with _SUPPRESS:
            _signal_tree(terminal.process)


def _file_size(target: Path) -> int:
    """Blocking file work, kept off the event loop so one slow read cannot stall the app."""
    return target.stat().st_size


def _read_text(target: Path) -> str:
    return target.read_text(encoding="utf-8", errors="replace")


def _write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _signal_tree(process: asyncio.subprocess.Process) -> None:
    """Kill the command and anything it started.

    Terminals are spawned as their own session (a process-group leader), so a bare
    ``process.kill()`` would orphan a build's own children and leave them running.
    """
    if hasattr(os, "killpg"):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    else:  # pragma: no cover - Windows has no process groups here
        process.kill()


async def _drain(terminal: _Terminal) -> None:
    """Read a terminal's output to EOF, then record how it exited."""
    stream = terminal.process.stdout
    if stream is not None:
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                break
            room = terminal.limit - len(terminal.output)
            if room <= 0:
                terminal.truncated = True
                continue
            terminal.output.extend(chunk[:room])
            if len(chunk) > room:
                terminal.truncated = True
    code = await terminal.process.wait()
    terminal.status = TerminalExitStatus(exit_code=code, signal=None)


def _window(content: str, *, line: int | None, limit: int | None) -> str:
    """The requested 1-based line window, matching ACP's ``line``/``limit`` arguments."""
    start = max((line or 1) - 1, 0)
    stop = None if limit is None else start + max(limit, 0)
    return "".join(content.splitlines(keepends=True)[start:stop])
