"""Presentation identifiers shared by the capability and its renderers."""

from __future__ import annotations

__all__ = ["RENDERER_IDS", "renderer_id"]

RENDERER_IDS = (
    "hrns.tool.read_file",
    "hrns.tool.apply_changes",
    "hrns.tool.list_files",
    "hrns.tool.grep",
    "hrns.tool.shell",
)
"""Presentation identifiers the bundled renderers implement, all at schema version 1."""


def renderer_id(tool: str) -> str:
    """The presentation identifier for one bundled tool."""
    return f"hrns.tool.{tool}"
