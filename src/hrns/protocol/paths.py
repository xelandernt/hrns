"""Read-only path policy shared by everything that writes to the operator's disk.

The bundled host toolset and the ACP client both write files for an agent, so they must agree
on what is off limits; otherwise the weaker guard becomes the way around the stronger one. It
is a guardrail against accidents, not a sandbox: the workspace directory is the boundary.
"""

from __future__ import annotations

import os

__all__ = ["READ_ONLY_DIRS", "READ_ONLY_SUFFIXES", "read_only_path"]

#: Directory names that are read-only wherever they appear in a path.
READ_ONLY_DIRS = frozenset({".git", ".hrns", ".pydantic-ai-harness"})
#: File suffixes that hold keys.
READ_ONLY_SUFFIXES = (".pem", ".key")


def read_only_path(relative: str) -> bool:
    """Whether ``relative`` (a path inside its root) must not be written."""
    parts = relative.replace(os.sep, "/").split("/")
    if any(part in READ_ONLY_DIRS for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name == ".env" or name.startswith(".env."):
        return True
    if name.startswith("secrets"):
        return True
    return name.endswith(READ_ONLY_SUFFIXES)
