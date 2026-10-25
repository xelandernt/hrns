"""File listing and content search with the same backend order as upstream Harness.

Ripgrep runs inside the workspace when the backend can run commands, because it honours
ignore files and is fast. A POSIX ``grep`` covers command-capable workspaces without
``rg``. A Python walk is the last resort for filesystem-only workspaces, so a sandbox with
no shell still gets working tools.

The fallbacks are intentionally narrower than ripgrep: ``grep`` uses the fixed exclusion list
below rather than repository ignore rules, and the Python walk skips hidden entries. Callers
that need exact ignore-file semantics should prefer a workspace with ``rg``.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import CommandResult, SupportsCommands, Workspace, WorkspaceError

__all__ = ["Match", "grep_workspace", "list_workspace", "supports_commands"]

_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
        ".idea",
    }
)
_MAX_PYTHON_FILE_BYTES = 1_000_000
_MAX_WALK_FILES = 20_000
_MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
_BOUNDED_CAPTURE_EXIT = 125
_BINARY_SAMPLE = 8_192

_POSIX_UNSUPPORTED = re.compile(r"\(\?|\\[dwsDSWbB]|\\[1-9]|\{\d*,\d*\}")
_RECORD = re.compile(r"^(?P<path>.+?)(?P<sep>[:-])(?P<line>\d+)(?P=sep)(?P<text>.*)$")
_NUMBERED = re.compile(r"^(?P<line>\d+)(?P<sep>[:-])(?P<text>.*)$")


@dataclass(frozen=True, slots=True)
class Match:
    """One search hit: a file, a 1-based line number, and the line's text."""

    path: str
    line: int
    text: str
    context: bool = False


class _BoundedCaptureUnavailable(WorkspaceError):
    """The workspace cannot cap command output; callers must use a filesystem path."""


def supports_commands(workspace: Workspace) -> bool:
    return isinstance(workspace.backend, SupportsCommands)


def _clean(path: str) -> str:
    cleaned = posixpath.normpath(path)
    return cleaned[2:] if cleaned.startswith("./") else cleaned


def _rg_missing(result: CommandResult) -> bool:
    if result.exit_code == 127:
        return True
    first = result.stderr.strip().splitlines()[:1]
    haystack = (first[0] if first else "").lower()
    return "not found" in haystack or "no such file" in haystack


def _bounded_command_script(argv: list[str], max_bytes: int) -> str:
    """A POSIX shell script that caps both streams without changing the command's exit status.

    Each stream is drained completely -- so the child never sees SIGPIPE -- while only the
    first ``max_bytes`` are retained and returned. The command's own status is written to a
    temporary file because a pipeline reports its last stage's status instead.
    """
    command = shlex.join(argv)
    return f"""\
if ! command -v mktemp >/dev/null 2>&1 ||
   ! command -v head >/dev/null 2>&1 ||
   ! command -v cat >/dev/null 2>&1 ||
   ! head -c 1 /dev/null >/dev/null 2>&1; then
    echo "bounded command capture is unavailable" >&2
    exit {_BOUNDED_CAPTURE_EXIT}
fi
status_file=$(mktemp) || exit {_BOUNDED_CAPTURE_EXIT}
trap 'rm -f "$status_file"' EXIT HUP INT TERM
{{
    {{ {command} 2>&1 1>&3; echo $? >&4; }} |
        {{ head -c {max_bytes} >&2; cat >/dev/null; }}
}} 3>&1 4>"$status_file" |
    {{ head -c {max_bytes}; cat >/dev/null; }}
status=$(cat "$status_file" 2>/dev/null)
exit "${{status:-1}}"
"""


async def _run(
    workspace: Workspace,
    argv: list[str],
    *,
    timeout: float = 120.0,
    max_bytes: int = _MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    """Run one command, capping both captured streams at the subprocess boundary.

    The workspace API returns complete output, so a POSIX shell pipeline drains stdout and
    stderr while retaining at most ``max_bytes`` from each. If the workspace cannot provide
    bounded command capture, callers fall back to the bounded filesystem walk instead of
    collecting unbounded output.
    """
    try:
        result = await workspace.run(
            _bounded_command_script(argv, max_bytes), shell=True, timeout=timeout
        )
    except (TypeError, ValueError, UserError, NotImplementedError) as exc:
        raise _BoundedCaptureUnavailable(
            "this workspace cannot run a shell with bounded output capture"
        ) from exc
    if result.exit_code == _BOUNDED_CAPTURE_EXIT:
        raise _BoundedCaptureUnavailable(
            result.stderr.strip() or "bounded command capture is unavailable"
        )
    return result


def _output_truncated(result: CommandResult, max_bytes: int = _MAX_COMMAND_OUTPUT_BYTES) -> bool:
    return len(result.stdout.encode("utf-8", "replace")) >= max_bytes


async def _walk(workspace: Workspace, root: str, *, limit: int) -> tuple[list[str], bool]:
    files: list[str] = []
    queue = [_clean(root)]
    while queue:
        current = queue.pop(0)
        try:
            entries = await workspace.list_dir(current)
        except (WorkspaceError, OSError):
            continue
        for entry in entries:
            name = entry.name
            if not name or name.startswith("."):
                continue
            relative = _clean(posixpath.join(current, name))
            if entry.is_dir:
                if name not in _IGNORED_DIRS:
                    queue.append(relative)
                continue
            files.append(relative)
            if len(files) >= limit:
                return files, True
    return files, False


async def _rg_list(
    workspace: Workspace, path: str, glob: str | None, limit: int
) -> tuple[list[str], bool] | None:
    argv = ["rg", "--files", "--null", "--sort", "path"]
    if glob:
        argv += ["--glob", glob]
    argv += ["--", path]
    result = await _run(workspace, argv)
    if _rg_missing(result) or result.exit_code not in (0, 1):
        return None
    if result.stderr.strip() and not result.stdout:
        return None
    parts = [part for part in result.stdout.split("\0") if part]
    listed = [_clean(part) for part in parts[:limit]]
    return listed, len(parts) > limit or _output_truncated(result)


async def list_workspace(
    workspace: Workspace,
    path: str,
    glob: str | None,
    *,
    limit: int,
) -> tuple[list[str], bool]:
    """Return workspace-relative file paths under ``path`` and whether they were cut."""
    if supports_commands(workspace):
        try:
            listed = await _rg_list(workspace, path, glob, limit)
        except (WorkspaceError, OSError):
            listed = None
        if listed is not None:
            return listed
    files, truncated = await _walk(workspace, path, limit=limit)
    if glob:
        files = [name for name in files if PurePosixPath(name).match(glob)]
    files.sort()
    if len(files) > limit:
        return files[:limit], True
    return files, truncated


def _validate_pattern(pattern: str) -> None:
    if _POSIX_UNSUPPORTED.search(pattern):
        raise ValueError("This pattern needs ripgrep: POSIX grep does not support the syntax used.")


def _parse_rg(output: str, limit: int) -> tuple[list[Match], bool]:
    matches: list[Match] = []
    for raw in output.splitlines():
        if not raw or raw == "--":
            continue
        path, separator, rest = raw.partition("\x00")
        if not separator:
            continue
        numbered = _NUMBERED.match(rest)
        if numbered is None:
            continue
        if len(matches) >= limit:
            return matches, True
        matches.append(
            Match(
                path=_clean(path),
                line=int(numbered.group("line")),
                text=numbered.group("text"),
                context=numbered.group("sep") == "-",
            )
        )
    return matches, False


async def _rg_grep(
    workspace: Workspace,
    pattern: str,
    path: str,
    *,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
) -> tuple[list[Match], bool] | None:
    argv = [
        "rg",
        "--line-number",
        "--no-heading",
        "--with-filename",
        "--color",
        "never",
        "--null",
        "--sort",
        "path",
    ]
    if ignore_case:
        argv.append("--ignore-case")
    if literal:
        argv.append("--fixed-strings")
    if context:
        argv += ["--context", str(max(0, min(context, 20)))]
    if glob:
        argv += ["--glob", glob]
    argv += ["--regexp", pattern, "--", path]
    result = await _run(workspace, argv)
    if _rg_missing(result) or result.exit_code not in (0, 1):
        return None
    if result.stderr.strip() and not result.stdout:
        return None
    matches, parsed_more = _parse_rg(result.stdout, limit)
    return matches, parsed_more or _output_truncated(result)


async def _posix_grep(
    workspace: Workspace,
    pattern: str,
    path: str,
    *,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
) -> tuple[list[Match], bool] | None:
    _validate_pattern(pattern)
    argv = ["grep", "-r", "-n", "-H", "-I"]
    for ignored in sorted(_IGNORED_DIRS):
        argv.append(f"--exclude-dir={ignored}")
    if ignore_case:
        argv.append("-i")
    if literal:
        argv.append("-F")
    if context:
        argv += ["-C", str(max(0, min(context, 20)))]
    if glob:
        argv += ["--include", glob]
    argv += ["-e", pattern, "--", path]
    try:
        result = await _run(workspace, argv)
    except ValueError:
        return None
    if result.exit_code not in (0, 1):
        return None
    if result.stderr.strip() and not result.stdout:
        return None
    matches: list[Match] = []
    for raw in result.stdout.splitlines():
        if not raw or raw == "--":
            continue
        record = _RECORD.match(raw)
        if record is None:
            continue
        if len(matches) >= limit:
            return matches, True
        matches.append(
            Match(
                path=_clean(record.group("path")),
                line=int(record.group("line")),
                text=record.group("text"),
                context=record.group("sep") == "-",
            )
        )
    return matches, _output_truncated(result)


async def _python_grep(
    workspace: Workspace,
    pattern: str,
    path: str,
    *,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
) -> list[Match]:
    flags = re.IGNORECASE if ignore_case else 0
    expression = re.escape(pattern) if literal else pattern
    try:
        compiled = re.compile(expression, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    root = _clean(path)
    entry = await workspace.stat(root)
    if entry.is_dir:
        files, _ = await _walk(workspace, root, limit=_MAX_WALK_FILES)
    else:
        # The tool contract allows a single file; walking it would silently return zero
        # matches because list_dir() fails on a regular file.
        files = [root]
    if glob:
        files = [name for name in files if PurePosixPath(name).match(glob)]
    matches: list[Match] = []
    for name in files:
        try:
            entry = await workspace.stat(name)
            if entry.is_dir or (entry.size is not None and entry.size > _MAX_PYTHON_FILE_BYTES):
                continue
            data = await workspace.read_bytes(name)
        except (WorkspaceError, OSError):
            continue
        if b"\x00" in data[:_BINARY_SAMPLE]:
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            if not compiled.search(line):
                continue
            if context:
                start = max(0, index - context)
                end = min(len(lines), index + context + 1)
                for offset in range(start, end):
                    matches.append(
                        Match(
                            path=name, line=offset + 1, text=lines[offset], context=offset != index
                        )
                    )
            else:
                matches.append(Match(path=name, line=index + 1, text=line))
            if len(matches) >= limit:
                return matches
    return matches


async def grep_workspace(
    workspace: Workspace,
    pattern: str,
    path: str,
    *,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
) -> tuple[list[Match], bool, str]:
    """Search file contents, returning matches, whether they were cut, and the backend used."""
    if supports_commands(workspace):
        runners = (("rg", _rg_grep), ("grep", _posix_grep))
        for backend, runner in runners:
            try:
                found = await runner(
                    workspace,
                    pattern,
                    path,
                    glob=glob,
                    ignore_case=ignore_case,
                    literal=literal,
                    context=context,
                    limit=limit,
                )
            except (WorkspaceError, OSError, ValueError):
                found = None
            if found is not None:
                matches, truncated = found
                # The collection is already bounded by the command cap and result limit,
                # so sorting it here cannot grow with the size of the repository.
                matches.sort(key=lambda match: (match.path, match.line))
                return matches, truncated, backend
    found = await _python_grep(
        workspace,
        pattern,
        path,
        glob=glob,
        ignore_case=ignore_case,
        literal=literal,
        context=context,
        limit=limit,
    )
    return found, len(found) >= limit, "python"
