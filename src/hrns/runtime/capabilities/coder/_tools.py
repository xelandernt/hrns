"""hrns-owned file, batch-change, search, and shell tools.

Each tool returns a model-facing string plus a presentation payload for the terminal. Path
resolution, read-only protection, hashes, and search backends live next door; this module is
only the tool surface: names, arguments, and what the model and the user each see.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolReturn
from pydantic_ai.tools import AgentDepsT, RunContext, Tool
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.workspaces import WorkspaceError, WorkspaceTimeoutError

from ._changes import FileChanges, apply_file_changes
from ._presentation import input_metadata, output_return
from ._search import grep_workspace, list_workspace, supports_commands
from ._workspace import FileGuard, content_hash, is_binary
from .config import CoderConfig

__all__ = ["CoderTools", "MUTATING_TOOLS"]

MUTATING_TOOLS = frozenset({"apply_changes", "shell"})
"""Tools the local host asks the user to approve before they run."""

MAX_READ_LINES = 2_000
MAX_READ_CHARS = 50_000
MAX_MODEL_TEXT = 64_000
MAX_LIST_RESULTS = 2_000
MAX_MATCHES = 200
MAX_TOOL_RETRIES = 5
"""Consecutive retries each tool allows before the run fails.

A rejected path or a stale hash is correctable from the error message, so the model needs a
few attempts before a long autonomous run would otherwise end and discard its work.
"""


def _guard(ctx: RunContext[Any], config: CoderConfig) -> FileGuard:
    return FileGuard(ctx.workspace, unrestricted=config.unrestricted_filesystem)


def _tail(text: str, limit: int = MAX_MODEL_TEXT) -> str:
    if len(text) <= limit:
        return text
    return f"… ({len(text) - limit} characters omitted)\n" + text[-limit:]


def _read_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{index + 1}\t{line}" for index, line in enumerate(lines, start=start))


@dataclass(kw_only=True)
class CoderTools(AbstractCapability[AgentDepsT]):
    """The bundled file, search, and shell tools for one coding binding."""

    config: CoderConfig
    id: str | None = "hrns.coder.tools"

    def get_toolset(self) -> FunctionToolset[AgentDepsT]:
        config = self.config

        async def read_file(
            ctx: RunContext[Any],
            path: str,
            *,
            offset: int = 0,
            limit: int | None = None,
        ) -> Any:
            """Read a text file with line numbers.

            Args:
                ctx: The current agent run context.
                path: File path relative to the working directory.
                offset: Zero-based line offset to start reading from.
                limit: Maximum number of lines to return (default: 2000).
            """
            if offset < 0:
                raise ModelRetry("offset must be non-negative.")
            if limit is not None and limit < 1:
                raise ModelRetry("limit must be at least 1.")
            workspace = ctx.workspace
            resolved = await _guard(ctx, config).check(path)
            try:
                data = await workspace.read_bytes(resolved)
            except FileNotFoundError as exc:
                raise ModelRetry(f"Path not found: {path}") from exc
            except IsADirectoryError as exc:
                raise ModelRetry(f"{path!r} is a directory; use list_files.") from exc
            except (WorkspaceError, OSError) as exc:
                raise ModelRetry(f"Could not read {path!r}: {exc}") from exc
            if is_binary(data):
                text = f"{path} is a binary file; inspect it with a shell command instead."
                return output_return(text, "read_file", {"path": path, "binary": True})
            text = _read_text(data)
            all_lines = text.splitlines()
            start = min(offset, len(all_lines))
            requested = MAX_READ_LINES if limit is None else min(limit, MAX_READ_LINES)
            selected: list[str] = []
            consumed = 0
            truncated_line = False
            for line in all_lines[start : start + requested]:
                cost = len(line) + 1
                if not selected and cost > MAX_READ_CHARS:
                    # A single minified line must still make the cursor advance; returning an
                    # empty page with the same offset would loop forever.
                    selected.append(line[: MAX_READ_CHARS - 1] + "…")
                    truncated_line = True
                    break
                if consumed + cost > MAX_READ_CHARS:
                    break
                consumed += cost
                selected.append(line)
            end = start + len(selected)
            if selected:
                header = f"{path} (lines {start + 1}-{end} of {len(all_lines)})"
                if truncated_line:
                    header += " (line truncated at the read limit)"
            else:
                header = f"{path} (no lines at offset {start}; {len(all_lines)} lines total)"
            header += f" [hash {content_hash(data)}]"
            body = _numbered(selected, start)
            model_text = header if not selected else f"{header}\n{body}"
            if end < len(all_lines):
                model_text += f"\n… {len(all_lines) - end} more lines; continue with offset={end}"
            payload = {
                "path": path,
                "startLine": start + 1,
                "endLine": end,
                "totalLines": len(all_lines),
                "hash": content_hash(data),
                "lines": [[start + index + 1, line] for index, line in enumerate(selected)],
                "truncated": end < len(all_lines),
                "lineTruncated": truncated_line,
            }
            return output_return(_tail(model_text), "read_file", payload, fallback=header)

        async def apply_changes(ctx: RunContext[AgentDepsT], changes: FileChanges) -> ToolReturn:
            """Apply one or many file changes in one approved batch.

            Batch all known changes to independent files together. Use one entry per file:
            `write` supplies complete content; `edit` supplies ordered exact replacements.
            Each replacement's old_text must match exactly once after earlier replacements;
            include surrounding context to make it unique. Omit line-number prefixes from
            read_file output. expected_hash optionally checks the original file content.
            If a batch fails partway, use the per-file results to decide what to retry.

            Args:
                ctx: The current agent run context.
                changes: File writes and edits, with at most one entry per resolved file.
            """
            return await apply_file_changes(ctx.workspace, config, changes)

        async def list_files(
            ctx: RunContext[Any],
            path: str = ".",
            *,
            glob: str | None = None,
        ) -> Any:
            """List files under a directory, recursively, respecting ignore files.

            Args:
                ctx: The current agent run context.
                path: Directory to list, relative to the working directory.
                glob: Only list files matching this glob (e.g. '*.py' or 'src/**').
            """
            workspace = ctx.workspace
            await _guard(ctx, config).check(path)
            try:
                files, truncated = await list_workspace(
                    workspace, path, glob, limit=MAX_LIST_RESULTS
                )
            except (WorkspaceError, OSError) as exc:
                raise ModelRetry(f"Could not list {path!r}: {exc}") from exc
            header = f"{path}: {len(files)} file{'s' if len(files) != 1 else ''}"
            if truncated:
                header += f" (first {MAX_LIST_RESULTS}; narrow the glob or path)"
            model_text = header + ("\n" + "\n".join(files) if files else "")
            payload = {
                "path": path,
                "entries": files,
                "count": len(files),
                "truncated": truncated,
            }
            return output_return(_tail(model_text), "list_files", payload, fallback=header)

        async def grep(
            ctx: RunContext[Any],
            pattern: str,
            *,
            path: str = ".",
            glob: str | None = None,
            ignore_case: bool = False,
            literal: bool = False,
            context: int = 0,
        ) -> Any:
            """Search file contents, respecting ignore files and skipping hidden files.

            Args:
                ctx: The current agent run context.
                pattern: Regular expression, or exact text when `literal` is set.
                path: Directory or file to search, relative to the working directory.
                glob: Only search files matching this glob (e.g. '*.py').
                ignore_case: Match case-insensitively.
                literal: Treat `pattern` as exact text rather than a regular expression.
                context: Lines of context around each match (0 to 20).
            """
            if not pattern:
                raise ModelRetry("pattern must not be empty.")
            workspace = ctx.workspace
            await _guard(ctx, config).check(path)
            try:
                matches, truncated, backend = await grep_workspace(
                    workspace,
                    pattern,
                    path,
                    glob=glob,
                    ignore_case=ignore_case,
                    literal=literal,
                    context=max(0, min(context, 20)),
                    limit=MAX_MATCHES,
                )
            except ValueError as exc:
                raise ModelRetry(str(exc)) from exc
            except (WorkspaceError, OSError) as exc:
                raise ModelRetry(f"Could not search {path!r}: {exc}") from exc
            hits = sum(1 for match in matches if not match.context)
            header = f"{hits} match{'es' if hits != 1 else ''} for {pattern!r}"
            if truncated:
                header += f" (first {MAX_MATCHES}; narrow the search)"
            rows = [
                f"{match.path}{'-' if match.context else ':'}{match.line}:{match.text}"
                for match in matches
            ]
            payload = {
                "pattern": pattern,
                "path": path,
                "backend": backend,
                "count": hits,
                "truncated": truncated,
                "matches": [
                    {
                        "path": match.path,
                        "line": match.line,
                        "text": match.text,
                        "context": match.context,
                    }
                    for match in matches
                ],
            }
            model_text = header + ("\n" + "\n".join(rows) if rows else "")
            return output_return(_tail(model_text), "grep", payload, fallback=header)

        async def shell(
            ctx: RunContext[Any],
            command: str,
            *,
            timeout: float | None = None,
        ) -> Any:
            """Run a foreground shell command in the workspace and return its output.

            Args:
                ctx: The current agent run context.
                command: The command line to run with the workspace's shell.
                timeout: Seconds to wait; defaults to the binding's shell_timeout.
            """
            workspace = ctx.workspace
            if not supports_commands(workspace):
                raise ModelRetry("This workspace cannot run commands.")
            if not command.strip():
                raise ModelRetry("command must not be empty.")
            budget = timeout if timeout is not None else config.shell_timeout
            budget = max(1.0, min(float(budget), 3600.0))
            started = time.monotonic()
            try:
                result = await workspace.run(command, shell=True, timeout=budget)
            except WorkspaceTimeoutError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                message = f"Timed out after {budget:.0f}s: {command}"
                payload = {
                    "command": command,
                    "exitCode": None,
                    "stdout": "",
                    "stderr": str(exc) or "timed out",
                    "durationMs": elapsed,
                    "timedOut": True,
                }
                return output_return(message, "shell", payload)
            except (WorkspaceError, OSError) as exc:
                raise ModelRetry(f"Could not run the command: {exc}") from exc
            elapsed = int((time.monotonic() - started) * 1000)
            stdout = result.stdout
            stderr = result.stderr
            parts = [f"exit code: {result.exit_code}"]
            if stdout:
                parts.append(stdout.rstrip("\n"))
            if stderr:
                parts.append(f"stderr:\n{stderr.rstrip()}")
            model_text = _tail("\n".join(parts))
            payload = {
                "command": command,
                "exitCode": result.exit_code,
                "stdout": _tail(stdout, 200_000),
                "stderr": _tail(stderr, 200_000),
                "durationMs": elapsed,
                "timedOut": False,
            }
            # The collapsed card previews real output rather than the model-facing
            # "exit code:" sentence; the exit code is only prefixed when it failed.
            preview = "\n".join(part for part in (stdout.rstrip("\n"), stderr.rstrip("\n")) if part)
            if result.exit_code:
                preview = f"exit {result.exit_code}" + (f"\n{preview}" if preview else "")
            return output_return(model_text, "shell", payload, fallback=preview or "no output")

        return FunctionToolset(
            id="hrns.coder.files",
            max_retries=MAX_TOOL_RETRIES,
            tools=[
                Tool(
                    read_file,
                    name="read_file",
                    takes_ctx=True,
                    metadata=input_metadata("read_file", pending="Reading file…"),
                ),
                Tool(
                    apply_changes,
                    name="apply_changes",
                    takes_ctx=True,
                    sequential=True,
                    metadata=input_metadata("apply_changes", pending="Applying file changes…"),
                ),
                Tool(
                    list_files,
                    name="list_files",
                    takes_ctx=True,
                    metadata=input_metadata("list_files", pending="Listing files…"),
                ),
                Tool(
                    grep,
                    name="grep",
                    takes_ctx=True,
                    metadata=input_metadata("grep", pending="Searching…"),
                ),
                Tool(
                    shell,
                    name="shell",
                    takes_ctx=True,
                    metadata=input_metadata("shell", pending="Running command…"),
                ),
            ],
        )
