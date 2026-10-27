"""Local workspaces: Pydantic AI's POSIX backend plus a native Windows one.

Pydantic AI's ``Workspace`` facade resolves paths with ``posixpath`` and its only in-process
local backend refuses non-POSIX platforms. The local developer agent therefore needs two
Windows pieces: a backend that runs commands through PowerShell 7 and uses the host filesystem
API, and a ``Workspace`` subclass that resolves paths with ``ntpath``. ``local_workspace()``
picks the right pair for the host platform.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import math
import os
import shutil
import stat as stat_module
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from anyio import CancelScope, to_thread
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    LocalWorkspaceBackend,
    SupportsCommands,
    SupportsFilesystem,
    SupportsRealpath,
    Workspace,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

__all__ = [
    "WindowsWorkspace",
    "WindowsWorkspaceBackend",
    "local_workspace",
    "require_pwsh",
]

_PWSH_FLAGS = ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command")
_PWSH_UTF8 = "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
"""Prefix for every shell command: redirected PowerShell output is then valid UTF-8."""

_INHERITED_ENV = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PSModulePath",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USERNAME",
    "USERDOMAIN",
)
"""Variables a shell needs on Windows; the rest of the host environment is not inherited.

The local runtime process carries provider credentials, so commands must not see the agent's
whole environment by accident. Callers can still add variables per command.
"""

_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _validate_timeout(timeout: float | None) -> None:
    if timeout is not None and (
        not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number or None")


@functools.lru_cache(maxsize=1)
def _locate_pwsh() -> str | None:
    return shutil.which("pwsh")


def require_pwsh() -> str:
    """The PowerShell 7 executable, or a ``ValueError`` a host can report at startup."""
    executable = _locate_pwsh()
    if executable is None:
        raise ValueError(
            "PowerShell 7 (pwsh) is required to run the local agent on Windows. "
            "Install it from https://aka.ms/powershell and make sure `pwsh` is on PATH."
        )
    return executable


def _same_or_ancestor(ancestor: str, path: str) -> bool:
    """Whether ``ancestor`` equals ``path`` or holds it, comparing case-insensitively."""
    ancestor = os.path.normcase(os.path.normpath(ancestor))
    path = os.path.normcase(os.path.normpath(path))
    if ancestor == path:
        return True
    try:
        return os.path.commonpath([ancestor, path]) == ancestor
    except ValueError:
        # Different drives: no shared prefix, so neither contains the other.
        return False


def _file_entry(target: Path) -> FileEntry:
    is_dir = target.is_dir()
    return FileEntry(
        name=target.name or str(target),
        path=str(target),
        is_dir=is_dir,
        size=None if is_dir else target.stat().st_size,
    )


def _list_dir(target: Path) -> list[FileEntry]:
    entries: list[FileEntry] = []
    with os.scandir(target) as scanned:
        for entry in scanned:
            try:
                is_dir = entry.is_dir()
                size = None if is_dir else entry.stat().st_size
            except OSError:
                # A broken symlink or a file removed mid-listing is still a file entry.
                is_dir = False
                size = None
            entries.append(
                FileEntry(
                    name=entry.name,
                    path=os.path.join(str(target), entry.name),
                    is_dir=is_dir,
                    size=size,
                )
            )
    return entries


def _is_reparse_point(target: Path) -> bool:
    """Whether ``target`` is a junction or another Windows reparse point.

    Reparse directories (junctions, mount points) must be removed with ``rmdir`` so the link
    itself goes, not the directory it points at.
    """
    try:
        attributes = getattr(target.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & flag)


class WindowsWorkspaceBackend(
    WorkspaceBackend, SupportsCommands, SupportsFilesystem, SupportsRealpath
):
    """A local workspace on Windows: native files and a PowerShell 7 shell.

    This isolates nothing: commands and absolute paths reach wherever this process can. The
    directory is the environment; it must already exist, and the backend never removes it.
    """

    #: Search builds POSIX shell scripts (`mktemp`, `head`, `/dev/null`), so it must fall back
    #: to the filesystem walk when the shell is PowerShell.
    posix_shell: bool = False

    def __init__(
        self,
        working_dir: str | os.PathLike[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if os.name != "nt":
            raise NotImplementedError(
                "WindowsWorkspaceBackend is only available on Windows; "
                "use LocalWorkspaceBackend on POSIX hosts."
            )
        expanded = Path(os.path.expanduser(os.fspath(working_dir)))
        self._working_dir = expanded if expanded.is_absolute() else Path.cwd() / expanded
        self._resolved: Path | None = None
        self._env = {
            name: os.environ[name] for name in _INHERITED_ENV if name in os.environ
        } | dict(env or {})
        self._ref = WorkspaceRef(provider="local", id=os.path.normpath(self._working_dir))

    @property
    def ref(self) -> WorkspaceRef:
        return self._ref

    def _ensure_alive(self) -> Path:
        root = self._resolved or self._working_dir.resolve()
        if not root.is_dir():
            raise WorkspaceUnavailableError(
                f"local workspace directory {self._working_dir!s} does not exist"
            )
        self._resolved = root
        return root

    async def _get_working_dir(self) -> Path:
        return await to_thread.run_sync(self._ensure_alive)

    @staticmethod
    def _path(path: str) -> Path:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError(f"path must be absolute, got {path!r}")
        return Path(os.path.normpath(target))

    # --- commands -----------------------------------------------------

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        _validate_timeout(timeout)
        argv = self._argv(command, shell)
        merged_env = {**self._env, **(env or {})}
        working_dir = await self._get_working_dir()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(working_dir),
                env=merged_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_CREATE_NEW_PROCESS_GROUP,
            )
        except FileNotFoundError:
            return CommandResult(
                exit_code=127,
                stdout="",
                stderr=f"{argv[0]}: command not found\n",
            )
        except PermissionError:
            return CommandResult(
                exit_code=126,
                stdout="",
                stderr=f"{argv[0]}: Permission denied\n",
            )
        try:
            if timeout is None:
                stdout, stderr = await process.communicate()
            else:
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
                except TimeoutError as exc:
                    raise WorkspaceTimeoutError(
                        f"command timed out after {timeout:g}s and was killed"
                    ) from exc
        except BaseException:
            # A cancelled run still has to stop the process tree; shield the cleanup so the
            # cancellation does not abort it part-way.
            with CancelScope(shield=True):
                await _kill_tree(process)
            raise
        assert process.returncode is not None
        return CommandResult(
            exit_code=process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _argv(command: WorkspaceCommand, shell: bool) -> list[str]:
        if isinstance(command, str):
            if not shell:
                raise TypeError(
                    "a string command requires shell=True; pass an argv sequence otherwise"
                )
            try:
                pwsh = require_pwsh()
            except ValueError as exc:
                raise WorkspaceUnavailableError(str(exc)) from exc
            return [pwsh, *_PWSH_FLAGS, _PWSH_UTF8 + command]
        if shell:
            raise TypeError(
                "an argv sequence cannot be combined with shell=True; pass a single command string"
            )
        return [os.fspath(part) for part in command]

    # --- filesystem ---------------------------------------------------

    async def read_bytes(self, path: str) -> bytes:
        await self._get_working_dir()
        target = self._path(path)
        if target.is_dir():
            raise IsADirectoryError(path)
        return await to_thread.run_sync(target.read_bytes)

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self._get_working_dir()
        await to_thread.run_sync(_write_bytes, self._path(path), data)

    async def stat(self, path: str) -> FileEntry:
        await self._get_working_dir()
        return await to_thread.run_sync(_file_entry, self._path(path))

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        await self._get_working_dir()
        return await to_thread.run_sync(_list_dir, self._path(path))

    async def make_dir(self, path: str) -> None:
        await self._get_working_dir()
        target = self._path(path)
        await to_thread.run_sync(functools.partial(target.mkdir, parents=True, exist_ok=True))

    async def remove(self, path: str) -> None:
        root = await self._get_working_dir()
        await to_thread.run_sync(_remove, self._path(path), root)

    async def exists(self, path: str) -> bool:
        await self._get_working_dir()
        return await to_thread.run_sync(self._path(path).exists)

    async def realpath(self, path: str) -> str:
        await self._get_working_dir()
        target = self._path(path)
        return await to_thread.run_sync(lambda: os.path.normpath(os.path.realpath(target)))


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Stop the command and any children it started, as ``taskkill /T`` does."""
    if process.returncode is None:
        with contextlib.suppress(OSError):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
    with contextlib.suppress(Exception):
        await process.wait()


def _write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _remove(target: Path, root: Path) -> None:
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(target)
    if _same_or_ancestor(real_target, real_root):
        raise ValueError(f"refusing to remove the workspace directory or its ancestor: {target}")
    if target.is_symlink():
        target.unlink()
    elif _is_reparse_point(target) and target.is_dir():
        target.rmdir()
    elif target.is_dir():
        shutil.rmtree(target)
    elif target.is_file():
        target.unlink()
    else:
        raise FileNotFoundError(str(target))


class WindowsWorkspace(Workspace):
    """A ``Workspace`` facade that resolves Windows paths with ``ntpath``.

    Pydantic AI's facade uses ``posixpath``, which treats ``C:\\DEV\\fixit`` as relative and
    joins it as text. Every file method routes through ``resolve``, so overriding it (and
    ``realpath``) gives the rest of the tool surface Windows semantics.
    """

    async def resolve(self, path: str, *, base: str | None = None) -> str:
        if base is not None and not os.path.isabs(base):
            raise ValueError(f"base must be an absolute path, got {base!r}")
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(base or await self.working_dir(), path))

    async def realpath(self, path: str) -> str:
        if not os.path.isabs(path):
            path = os.path.join(await self.working_dir(), path)
        backend = self.backend
        if not isinstance(backend, SupportsRealpath):
            raise WorkspaceUnavailableError(
                "this Windows workspace backend cannot resolve symlinks"
            )
        return await backend.realpath(path)


def local_workspace(value: str | os.PathLike[str]) -> Workspace:
    """The local workspace implementation for this host platform."""
    path = Path(value)
    if os.name == "nt":
        return WindowsWorkspace(WindowsWorkspaceBackend(path))
    return Workspace(LocalWorkspaceBackend(path))
