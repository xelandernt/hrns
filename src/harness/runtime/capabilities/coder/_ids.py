"""Presentation identifiers shared by the capability and its renderers."""

from __future__ import annotations

__all__ = ["RENDERER_IDS", "renderer_id"]

RENDERER_IDS = (
    "harness.tool.read_file",
    "harness.tool.write_file",
    "harness.tool.edit_file",
    "harness.tool.list_files",
    "harness.tool.grep",
    "harness.tool.shell",
)
"""Presentation identifiers the bundled renderers implement, all at schema version 1."""


def renderer_id(tool: str) -> str:
    """The presentation identifier for one bundled tool."""
    return f"harness.tool.{tool}"
