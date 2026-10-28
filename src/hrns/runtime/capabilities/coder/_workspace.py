"""Workspace guardrails and text helpers for the bundled coding tools.

The checks mirror the ones Pydantic AI's filesystem capability applies: a root the model may
not escape, read-only paths for credentials and version-control internals, binary detection,
and content hashes so a stale write cannot silently win. None of it is isolation — the
workspace backend is the boundary; this is a guardrail against accidents.
"""

from __future__ import annotations

import difflib
import hashlib
import os

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.workspaces import Workspace

from hrns.protocol.paths import read_only_path

__all__ = [
    "FileGuard",
    "content_hash",
    "is_binary",
    "unified_diff",
]

_BINARY_SAMPLE = 8_192


def _read_only(relative: str) -> bool:
    """Read-only policy, shared with the ACP client so neither can write what the other won't."""
    return read_only_path(relative)


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


def content_hash(data: bytes) -> str:
    """Short, stable hash returned to the model for optimistic concurrency."""
    return hashlib.sha256(data).hexdigest()[:16]


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SAMPLE]


def unified_diff(before: str, after: str, *, limit: int = 400) -> list[str]:
    """Unified diff lines with headers stripped, bounded for display."""
    old = before.splitlines()
    new = after.splitlines()
    rows = list(difflib.unified_diff(old, new, lineterm="", n=3))
    body = [row for row in rows[2:]]
    if len(body) > limit:
        body = body[:limit] + [f"… {len(body) - limit} more diff lines"]
    return body


class FileGuard:
    """Resolve tool paths against the run workspace and refuse unsafe ones."""

    def __init__(self, workspace: Workspace, *, unrestricted: bool = False) -> None:
        self.workspace = workspace
        self.unrestricted = unrestricted

    async def root(self) -> str:
        if self.unrestricted:
            return "/"
        return await self.workspace.working_dir()

    async def check(self, path: str, *, write: bool = False) -> str:
        """Return the spelling to hand the workspace, or raise a model retry."""
        resolved = await self.workspace.resolve(path)
        real = resolved
        try:
            real = await self.workspace.realpath(resolved)
        except Exception:
            real = resolved
        root = await self.root()
        if self.unrestricted:
            match = root
        else:
            roots = {root}
            try:
                roots.add(await self.workspace.realpath(root))
            except Exception:
                pass
            match = next((candidate for candidate in roots if _contains(candidate, real)), None)
            if match is None:
                raise ModelRetry(
                    f"Path {path!r} is outside the workspace root. Use a path inside {root!r}."
                )
        if write and not self.unrestricted:
            relative = os.path.relpath(real, match)
            if _read_only(relative):
                raise ModelRetry(
                    f"Path {path!r} is read-only for this agent "
                    "(version control, environment files, keys, and secrets are protected)."
                )
        return resolved
