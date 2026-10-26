"""Typed file-change batches: validate first, then report every write outcome."""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolReturn
from pydantic_ai.workspaces import Workspace, WorkspaceError

from ._presentation import output_return
from ._workspace import FileGuard, content_hash, is_binary, unified_diff
from .config import CoderConfig

MAX_CHANGE_FILES = 64


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
        diff = unified_diff(before, after)
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
            "hash": content_hash(self.after),
            "added": added,
            "removed": removed,
            "diff": diff,
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


async def _prepare(
    workspace: Workspace, guard: FileGuard, changes: list[FileChange]
) -> list[_PreparedChange]:
    prepared: list[_PreparedChange] = []
    seen: set[str] = set()
    for change in changes:
        resolved = await guard.check(change.path, write=True)
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
            before is None or content_hash(before) != change.expected_hash
        ):
            raise ModelRetry(
                f"{change.path!r} does not match expected_hash. Read it again. No files were changed."
            )
        if before is not None and is_binary(before):
            raise ModelRetry(f"{change.path!r} is binary. No files were changed.")
        try:
            text = before.decode("utf-8") if before is not None else ""
        except UnicodeDecodeError as exc:
            raise ModelRetry(f"{change.path!r} is not UTF-8 text. No files were changed.") from exc
        if isinstance(change, FileWrite):
            text = change.content
        else:
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
        try:
            after = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ModelRetry(
                f"{change.path!r} contains invalid Unicode. No files were changed."
            ) from exc
        prepared.append(_PreparedChange(change.path, canonical, before, after))
    return prepared


def _result(files: list[dict[str, object]], *, success: bool) -> ToolReturn:
    lines = []
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
    return output_return("\n".join(lines), "apply_changes", {"success": success, "files": files})


async def apply_file_changes(
    workspace: Workspace, config: CoderConfig, changes: list[FileChange]
) -> ToolReturn:
    """Preflight the whole batch, then commit each file once without claiming atomicity."""
    guard = FileGuard(workspace, unrestricted=config.unrestricted_filesystem)
    prepared = await _prepare(workspace, guard, changes)
    results: list[dict[str, object]] = []
    for index, item in enumerate(prepared):
        writing = False
        try:
            resolved = await guard.check(item.path, write=True)
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
            results.append({"path": item.path, "status": "failed", "error": detail})
            results.extend(
                {"path": pending.path, "status": "not attempted"}
                for pending in prepared[index + 1 :]
            )
            return _result(results, success=False)
        results.append(item.result())
    return _result(results, success=True)
