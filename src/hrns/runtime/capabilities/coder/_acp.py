"""Standard ACP presentation for the bundled coder tool names.

The richer ``hrns.presentation`` payload powers hrns clients, while this presenter keeps
coder calls meaningful in any ACP client and when the optional renderer is unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping

from acp import schema
from pydantic_ai.messages import ToolCallPart
from pydantic_ai_harness.experimental.acp import ToolCallPresentation

__all__ = ["coder_tool_presenter"]


def _path(value: object, *, default: str | None = None) -> str | None:
    return value if isinstance(value, str) and value else default


def _locations(paths: list[str]) -> tuple[schema.ToolCallLocation, ...]:
    return tuple(schema.ToolCallLocation(path=path) for path in dict.fromkeys(paths))


def _apply_changes(args: Mapping[str, object]) -> ToolCallPresentation | None:
    changes = args.get("changes")
    if not isinstance(changes, list):
        return None
    paths = [
        path
        for change in changes
        if isinstance(change, Mapping)
        if (path := _path(change.get("path"))) is not None
    ]
    if not paths:
        return None
    return ToolCallPresentation(
        title="Apply changes",
        kind="edit",
        locations=_locations(paths),
    )


def coder_tool_presenter(call: ToolCallPart) -> ToolCallPresentation | None:
    """Map bundled coder calls onto ACP's standard kind and location fields."""
    args = call.args_as_dict()
    name = call.tool_name
    if name == "apply_changes":
        return _apply_changes(args)
    if name == "read_file":
        path = _path(args.get("path"))
        if path is None:
            return None
        return ToolCallPresentation(
            title="Read file",
            kind="read",
            locations=_locations([path]),
        )
    if name == "list_files":
        path = _path(args.get("path"), default=".")
        return ToolCallPresentation(
            title="List files",
            kind="search",
            locations=_locations([path]) if path is not None else (),
        )
    if name == "grep":
        if _path(args.get("pattern")) is None:
            return None
        path = _path(args.get("path"), default=".")
        return ToolCallPresentation(
            title="Search files",
            kind="search",
            locations=_locations([path]) if path is not None else (),
        )
    if name == "shell":
        if _path(args.get("command")) is None:
            return None
        return ToolCallPresentation(title="Run command", kind="execute")
    return None
