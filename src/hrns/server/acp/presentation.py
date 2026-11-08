"""Map Pydantic AI tool calls onto ACP's rich tool-call presentation fields."""

from __future__ import annotations

import os.path
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from acp import schema
from pydantic_ai.messages import ToolCallPart

from .updates import ToolCallContent, tool_diff_content


@dataclass(frozen=True, kw_only=True)
class ToolCallPresentation:
    """How a tool call should be shown in an ACP client."""

    kind: schema.ToolKind | None = None
    title: str | None = None
    locations: tuple[schema.ToolCallLocation, ...] = ()
    content: tuple[ToolCallContent, ...] = ()


# A function deciding how to present a tool call, or ``None`` to leave it to generic rendering.
ToolCallPresenter = Callable[[ToolCallPart], ToolCallPresentation | None]


def _nonempty_str(args: Mapping[str, object], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) and value else None


def _any_str(args: Mapping[str, object], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) else None


def _location(path: str) -> schema.ToolCallLocation:
    return schema.ToolCallLocation(path=path)


def _present_read(args: Mapping[str, object]) -> ToolCallPresentation | None:
    path = _nonempty_str(args, "path")
    if path is None:
        return None
    return ToolCallPresentation(kind="read", locations=(_location(path),))


def _present_edit(args: Mapping[str, object]) -> ToolCallPresentation | None:
    path = _nonempty_str(args, "path")
    old_text = _nonempty_str(args, "old_text")
    new_text = _any_str(args, "new_text")
    if path is None or old_text is None or new_text is None:
        return None
    diff = tool_diff_content(path=path, new_text=new_text, old_text=old_text)
    return ToolCallPresentation(kind="edit", locations=(_location(path),), content=(diff,))


def _present_write(args: Mapping[str, object]) -> ToolCallPresentation | None:
    path = _nonempty_str(args, "path")
    content = _any_str(args, "content")
    if path is None or content is None:
        return None
    diff = tool_diff_content(path=path, new_text=content)
    return ToolCallPresentation(kind="edit", locations=(_location(path),), content=(diff,))


def _present_create_directory(args: Mapping[str, object]) -> ToolCallPresentation | None:
    path = _nonempty_str(args, "path")
    if path is None:
        return None
    return ToolCallPresentation(kind="other", locations=(_location(path),))


def _present_list(args: Mapping[str, object]) -> ToolCallPresentation | None:
    path = _nonempty_str(args, "path")
    locations = (_location(path),) if path is not None else ()
    return ToolCallPresentation(kind="search", locations=locations)


def _present_grep(args: Mapping[str, object]) -> ToolCallPresentation | None:
    if _nonempty_str(args, "pattern") is None:
        return None
    path = _nonempty_str(args, "path")
    locations = (_location(path),) if path is not None else ()
    return ToolCallPresentation(kind="search", locations=locations)


def _present_run(args: Mapping[str, object]) -> ToolCallPresentation | None:
    if _nonempty_str(args, "command") is None:
        return None
    return ToolCallPresentation(kind="execute")


def _present_command_ref(args: Mapping[str, object]) -> ToolCallPresentation | None:
    if _nonempty_str(args, "command_id") is None:
        return None
    return ToolCallPresentation(kind="execute")


_HANDLERS: dict[str, Callable[[Mapping[str, object]], ToolCallPresentation | None]] = {
    "read_file": _present_read,
    "file_info": _present_read,
    "edit_file": _present_edit,
    "write_file": _present_write,
    "create_directory": _present_create_directory,
    "list_directory": _present_list,
    "search_files": _present_grep,
    "find_files": _present_grep,
    "run_command": _present_run,
    "start_command": _present_run,
    "check_command": _present_command_ref,
    "stop_command": _present_command_ref,
}


def default_coding_presenter(call: ToolCallPart) -> ToolCallPresentation | None:
    """Present a recognized FileSystem/Shell tool call, else ``None``."""
    handler = _HANDLERS.get(call.tool_name)
    if handler is None:
        return None
    return handler(call.args_as_dict())


def chain_presenters(*presenters: ToolCallPresenter) -> ToolCallPresenter:
    """Combine presenters, using the first one that returns a presentation."""

    def presenter(call: ToolCallPart) -> ToolCallPresentation | None:
        for candidate in presenters:
            result = candidate(call)
            if result is not None:
                return result
        return None

    return presenter


def _resolve_within(path: str, cwd: str) -> str | None:
    """Resolve a relative path against ``cwd``, or ``None`` if it escapes it."""
    if os.path.isabs(path):
        return path
    resolved = os.path.normpath(os.path.join(cwd, path))
    relative = os.path.relpath(resolved, os.path.normpath(cwd))
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    return resolved


def _within_content(item: ToolCallContent, cwd: str) -> ToolCallContent | None:
    if isinstance(item, schema.FileEditToolCallContent):
        resolved = _resolve_within(item.path, cwd)
        return None if resolved is None else item.model_copy(update={"path": resolved})
    return item


def absolutize(presentation: ToolCallPresentation, cwd: str) -> ToolCallPresentation:
    """Resolve a presentation's workspace-relative paths against the session ``cwd``."""
    locations = tuple(
        loc.model_copy(update={"path": resolved})
        for loc in presentation.locations
        if (resolved := _resolve_within(loc.path, cwd)) is not None
    )
    content = tuple(
        item
        for item in (_within_content(entry, cwd) for entry in presentation.content)
        if item is not None
    )
    return replace(presentation, locations=locations, content=content)
