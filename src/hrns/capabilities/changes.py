"""`apply_changes`: one approved batch of file writes and edits.

The filesystem capability applies one change per call, so a multi-file task costs one
approval and one round trip per file. This capability adds a single batch tool: the model
puts every change it already knows about in one call, the whole set is validated before
anything is written, and each file is reported individually.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT, RunContext, Tool
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.workspaces import Workspace, WorkspaceError

__all__ = ["ApplyChanges"]

MAX_CHANGE_FILES = 64
MAX_CHANGE_TOOL_RETRIES = 5
"""Consecutive retries the tool allows before the run fails.

A rejected path, a duplicate target, or a stale hash is correctable from the error message,
so the model needs a few attempts before a long autonomous run would otherwise end.
"""

_BINARY_SAMPLE = 8_192
_HASH_CHARS = 12
"""The hash length the filesystem tools report, so a hash from `read_file` still matches."""

# Kept in step with ``hrns.client.io.read_only_path``: the agent's tools and the ACP client
# both write for the agent, and the weaker guard must not become the way around the stronger.
_READ_ONLY_DIRS = frozenset({".git", ".hrns", ".pydantic-ai-harness"})


def _read_only(relative: str) -> bool:
    parts = relative.replace(os.sep, "/").split("/")
    if any(part in _READ_ONLY_DIRS for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name == ".env" or name.startswith(".env."):
        return True
    if name.lower().startswith("secrets"):
        return True
    return name.lower().endswith((".pem", ".key"))


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:_HASH_CHARS]


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SAMPLE]


def _contains(root: str, path: str) -> bool:
    root = os.path.normcase(os.path.normpath(root))
    path = os.path.normcase(os.path.normpath(path))
    if root == path:
        return True
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        # Different drives on Windows.
        return False


class TextReplacement(BaseModel):
    """One exact replacement, applied after the preceding replacements."""

    model_config = ConfigDict(extra="forbid")

    old_text: str = Field(min_length=1, description="Exact text occurring once at this step.")
    new_text: str = Field(description="Replacement text; may be empty to remove the old text.")


class _FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="File path relative to the workspace.")
    expected_hash: str | None = Field(
        default=None,
        description="Hash from read_file, checked against the original file before any edits.",
    )


class FileWrite(_FileChange):
    """Create a text file or replace its complete content."""

    kind: Literal["write"]
    content: str = Field(description="Complete new UTF-8 text content.")


class FileEdit(_FileChange):
    """Make ordered exact replacements within one existing text file."""

    kind: Literal["edit"]
    replacements: list[TextReplacement] = Field(
        min_length=1,
        description="Replacements in order; each sees the result of the previous one.",
    )


FileChange = Annotated[FileWrite | FileEdit, Field(discriminator="kind")]
FileChanges = Annotated[list[FileChange], Field(min_length=1, max_length=MAX_CHANGE_FILES)]


@dataclass(frozen=True)
class _PreparedChange:
    path: str
    canonical: str
    before: bytes | None
    after: bytes

    def result(self) -> dict[str, object]:
        before = self.before.decode("utf-8") if self.before is not None else ""
        after = self.after.decode("utf-8")
        added = removed = 0
        for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
            a=before.splitlines(), b=after.splitlines()
        ).get_opcodes():
            if tag in {"replace", "insert"}:
                added += new_end - new_start
            if tag in {"replace", "delete"}:
                removed += old_end - old_start
        return {
            "path": self.path,
            "status": "completed",
            "created": self.before is None,
            "hash": _content_hash(self.after),
            "added": added,
            "removed": removed,
        }


async def _snapshot(workspace: Workspace, path: str) -> bytes | None:
    try:
        return await workspace.read_bytes(path)
    except FileNotFoundError:
        return None


async def _canonical(workspace: Workspace, path: str) -> str:
    try:
        return await workspace.realpath(path)
    except (WorkspaceError, OSError, NotImplementedError):
        return path


async def _resolve(workspace: Workspace, path: str) -> str:
    """Return the spelling to hand the workspace, or refuse a path outside it."""
    resolved = await workspace.resolve(path)
    try:
        real = await workspace.realpath(resolved)
    except (WorkspaceError, OSError, NotImplementedError):
        real = resolved
    root = await workspace.working_dir()
    roots = {root}
    try:
        roots.add(await workspace.realpath(root))
    except (OSError, NotImplementedError):
        pass
    if not any(_contains(candidate, real) for candidate in roots):
        raise ModelRetry(
            f"Path {path!r} is outside the workspace root. Use a path inside {root!r}."
        )
    if _read_only(os.path.relpath(real, root)):
        raise ModelRetry(
            f"Path {path!r} is read-only for this agent "
            "(version control, environment files, keys, and secrets are protected)."
        )
    return resolved


def _edited_text(change: FileEdit, before: bytes | None, text: str) -> str:
    if before is None:
        raise ModelRetry(f"Path not found: {change.path}. No files were changed.")
    for index, replacement in enumerate(change.replacements, 1):
        count = text.count(replacement.old_text)
        if count != 1:
            raise ModelRetry(
                f"Replacement {index} in {change.path!r} matches {count} times; "
                "it must match exactly once. Read the file and include unique context. "
                "No files were changed."
            )
        text = text.replace(replacement.old_text, replacement.new_text, 1)
    return text


async def _prepare(workspace: Workspace, changes: list[FileChange]) -> list[_PreparedChange]:
    prepared: list[_PreparedChange] = []
    seen: set[str] = set()
    for change in changes:
        resolved = await _resolve(workspace, change.path)
        canonical = await _canonical(workspace, resolved)
        if canonical in seen:
            raise ModelRetry(
                f"Duplicate target {change.path!r}. Use one entry per file and put its "
                "replacements in order. No files were changed."
            )
        seen.add(canonical)
        try:
            before = await _snapshot(workspace, resolved)
        except (WorkspaceError, OSError) as exc:
            raise ModelRetry(
                f"Could not read {change.path!r}: {exc}. No files were changed."
            ) from exc
        if change.expected_hash is not None and (
            before is None or _content_hash(before) != change.expected_hash
        ):
            raise ModelRetry(
                f"{change.path!r} does not match expected_hash. Read it again. "
                "No files were changed."
            )
        if before is not None and _is_binary(before):
            raise ModelRetry(f"{change.path!r} is binary. No files were changed.")
        try:
            text = before.decode("utf-8") if before is not None else ""
        except UnicodeDecodeError as exc:
            raise ModelRetry(f"{change.path!r} is not UTF-8 text. No files were changed.") from exc
        if isinstance(change, FileWrite):
            text = change.content
        else:
            text = _edited_text(change, before, text)
        if "\x00" in text:
            raise ModelRetry(f"{change.path!r} would become binary. No files were changed.")
        prepared.append(_PreparedChange(change.path, canonical, before, text.encode("utf-8")))
    return prepared


def _result(files: list[dict[str, object]], *, success: bool) -> str:
    lines: list[str] = []
    for item in files:
        if item["status"] == "completed":
            verb = "Created" if item["created"] else "Updated"
            lines.append(
                f"{verb} {item['path']} (+{item['added']} -{item['removed']}, hash {item['hash']})"
            )
        else:
            lines.append(
                f"{item['path']}: {item['status']}"
                + (f" — {item['error']}" if "error" in item else "")
            )
    if not success:
        applied = sum(item["status"] == "completed" for item in files)
        lines.insert(0, f"Batch incomplete: {applied} of {len(files)} files updated.")
        lines.append("Read affected files before retrying; do not replay the whole batch blindly.")
    return "\n".join(lines)


async def apply_file_changes(workspace: Workspace, changes: list[FileChange]) -> str:
    """Preflight the whole batch, then commit each file once without claiming atomicity."""
    prepared = await _prepare(workspace, changes)
    files: list[dict[str, object]] = []
    for index, item in enumerate(prepared):
        writing = False
        try:
            resolved = await _resolve(workspace, item.path)
            if (
                await _canonical(workspace, resolved) != item.canonical
                or await _snapshot(workspace, resolved) != item.before
            ):
                raise ModelRetry("File changed during batch preparation; read it again.")
            writing = True
            await workspace.write_bytes(resolved, item.after)
        except (ModelRetry, WorkspaceError, OSError) as exc:
            detail = str(exc)
            if writing:
                detail += " The write outcome is unknown; the file may have been partially written."
            files.append({"path": item.path, "status": "failed", "error": detail})
            files.extend(
                {"path": pending.path, "status": "not attempted"}
                for pending in prepared[index + 1 :]
            )
            return _result(files, success=False)
        files.append(item.result())
    return _result(files, success=True)


@dataclass(kw_only=True)
class ApplyChanges(AbstractCapability[AgentDepsT]):
    """The `apply_changes` tool: one approved batch of writes and edits across many files."""

    def get_toolset(self) -> FunctionToolset[AgentDepsT]:
        async def apply_changes(ctx: RunContext[AgentDepsT], changes: FileChanges) -> str:
            """Apply one or many file changes in one approved batch.

            Batch all known changes to independent files together. Use one entry per file:
            `write` supplies complete content; `edit` supplies ordered exact replacements.
            Each replacement's old_text must match exactly once after earlier replacements;
            include surrounding context to make it unique. Omit line-number prefixes from
            read_file output. expected_hash optionally checks the original file content.
            The whole batch is validated before anything is written, so a preflight failure
            changes no files; a failure during writing reports each file individually.
            If a batch fails partway, use the per-file results to decide what to retry.

            Args:
                ctx: The current agent run context.
                changes: File writes and edits, with at most one entry per resolved file.
            """
            return await apply_file_changes(ctx.workspace, changes)

        return FunctionToolset(
            id="apply_changes",
            max_retries=MAX_CHANGE_TOOL_RETRIES,
            tools=[Tool(apply_changes, name="apply_changes", takes_ctx=True, sequential=True)],
        )
