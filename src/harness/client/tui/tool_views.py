"""First-party tool bodies and their approval variants.

Views are ordinary Textual widgets resolved through the same ``harness.renderers`` contract
third-party plugins use; they are registered programmatically because they ship inside the
client. Every renderer reads the validated presentation payload and falls back to plain text
when the payload is absent or shaped unexpectedly.

An approval variant renders the arguments the agent is asking permission for. The call has
not run yet, so there is no output payload: the body comes from ``raw_input`` and only uses
presentation data when a provider or the capability published some.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, TypeAlias

from rich.text import Text
from textual.widgets import Static

from ..renderers import readable_text
from .extensions import ToolViewState, ViewContext, ViewHandle

__all__ = ["register_builtin_renderers"]

_READ_ROWS = 60
_DIFF_ROWS = 60
_LIST_ROWS = 60
_GREP_ROWS = 40
_SHELL_ROWS = 40


def _wire(state: ToolViewState) -> dict[str, Any]:
    payload = state.output or state.input or {}
    return payload if isinstance(payload, dict) else {}


def _data(state: ToolViewState) -> dict[str, Any]:
    data = _wire(state).get("data")
    return data if isinstance(data, dict) else {}


def _args(state: ToolViewState) -> Mapping[str, Any]:
    """The arguments of the call an approval is asking about."""
    return state.raw_input if isinstance(state.raw_input, Mapping) else {}


def _pending(state: ToolViewState) -> Text:
    label = str(state.fallback or "working…")
    return Text(readable_text(label, limit=200), style="dim")


def _more(text: Text, hidden: int, noun: str = "lines") -> None:
    if hidden <= 0:
        return
    text.append("\n")
    text.append(f"… {hidden} more {noun}", style="dim")


def _diff_text(state: ToolViewState) -> Text:
    data = _data(state)
    return _diff_body(data) if data else _pending(state)


def _diff_body(data: Mapping[str, Any]) -> Text:
    text = Text()
    text.append(str(data.get("path") or ""), style="bold")
    verb = str(data.get("verb") or ("created" if data.get("created") else "edited"))
    text.append(f"  {verb}  ", style="dim")
    text.append(f"+{int(data.get('added') or 0)}", style="green")
    text.append(" ")
    text.append(f"-{int(data.get('removed') or 0)}", style="red")
    rows = [str(row) for row in (data.get("diff") or [])]
    for row in rows[:_DIFF_ROWS]:
        text.append("\n")
        if row.startswith("@@"):
            style = "cyan"
        elif row.startswith("+"):
            style = "green"
        elif row.startswith("-"):
            style = "red"
        else:
            style = "dim"
        text.append(readable_text(row, limit=400), style=style)
    _more(text, len(rows) - _DIFF_ROWS)
    return text


def _read_file(state: ToolViewState) -> Text:
    data = _data(state)
    if not data:
        return _pending(state)
    if data.get("binary"):
        return Text(f"{data.get('path')} · binary file", style="dim")
    text = Text()
    text.append(str(data.get("path") or ""), style="bold")
    total = int(data.get("totalLines") or 0)
    start = int(data.get("startLine") or 0)
    end = int(data.get("endLine") or 0)
    if total:
        text.append(f"  {start}–{end} of {total}", style="dim")
    rows = data.get("lines") or []
    shown = 0
    for row in rows[:_READ_ROWS]:
        try:
            number, content = row
        except (TypeError, ValueError):
            continue
        text.append("\n")
        text.append(f"{int(number):>5} ", style="dim")
        text.append(readable_text(content, limit=400))
        shown += 1
    _more(text, len(rows) - shown)
    return text


def _list_files(state: ToolViewState) -> Text:
    data = _data(state)
    if not data:
        return _pending(state)
    entries = [readable_text(name, limit=400) for name in (data.get("entries") or [])]
    text = Text()
    text.append(str(data.get("path") or "."), style="bold")
    text.append(f"  {int(data.get('count') or 0)} files", style="dim")
    if data.get("truncated"):
        text.append("  (truncated)", style="yellow")

    rendered: list[tuple[int, str, bool]] = []
    seen_dirs: set[str] = set()
    for name in entries:
        parts = name.split("/")
        for depth in range(len(parts) - 1):
            directory = "/".join(parts[: depth + 1])
            if directory in seen_dirs:
                continue
            seen_dirs.add(directory)
            rendered.append((depth, f"{parts[depth]}/", True))
        rendered.append((len(parts) - 1, parts[-1], False))
    shown = 0
    for depth, name, is_dir in rendered[:_LIST_ROWS]:
        text.append("\n")
        text.append("  " * depth + name, style="bold" if is_dir else "dim")
        shown += 1
    _more(text, len(rendered) - shown, "entries")
    return text


def _highlight(text: Text, line: str, pattern: str) -> None:
    try:
        match = re.search(pattern, line)
    except re.error:
        match = None
    if match is None or match.start() == match.end():
        text.append(line)
        return
    text.append(line[: match.start()])
    text.append(line[match.start() : match.end()], style="bold")
    text.append(line[match.end() :])


def _grep(state: ToolViewState) -> Text:
    data = _data(state)
    if not data:
        return _pending(state)
    text = Text()
    text.append(f"{int(data.get('count') or 0)} matches", style="bold")
    text.append(f"  {data.get('pattern')!r}", style="dim")
    if data.get("truncated"):
        text.append("  (truncated)", style="yellow")
    pattern = str(data.get("pattern") or "")
    rows = data.get("matches") or []
    shown = 0
    for row in rows[:_GREP_ROWS]:
        if not isinstance(row, dict):
            continue
        text.append("\n")
        separator = "-" if row.get("context") else ":"
        text.append(f"{row.get('path')}{separator}{row.get('line')}", style="dim")
        text.append(separator, style="dim")
        line = readable_text(row.get("text") or "", limit=400)
        if row.get("context"):
            text.append(line, style="dim")
        else:
            _highlight(text, line, pattern)
        shown += 1
    _more(text, len(rows) - shown, "matches")
    return text


def _shell(state: ToolViewState) -> Text:
    data = _data(state)
    return _shell_body(data) if data else _pending(state)


def _shell_body(data: Mapping[str, Any]) -> Text:
    text = Text()
    text.append("$ ", style="dim")
    text.append(readable_text(data.get("command") or "", limit=400), style="bold")
    if data.get("timedOut"):
        text.append("  timed out", style="yellow")
    elif "exitCode" in data:
        code = int(data.get("exitCode") or 0)
        text.append(f"  exit {code}", style="green" if code == 0 else "red")
    duration = int(data.get("durationMs") or 0)
    if duration:
        text.append(f"  {duration / 1000:.1f}s", style="dim")
    stdout = [line for line in str(data.get("stdout") or "").splitlines()]
    for line in stdout[:_SHELL_ROWS]:
        text.append("\n")
        text.append(readable_text(line, limit=400), style="dim")
    _more(text, len(stdout) - _SHELL_ROWS)
    stderr = [line for line in str(data.get("stderr") or "").splitlines()]
    for line in stderr[:_SHELL_ROWS]:
        text.append("\n")
        text.append(readable_text(line, limit=400), style="red")
    _more(text, len(stderr) - _SHELL_ROWS)
    return text


# ------------------------------------------------------------- approval bodies
#
# The call has not run yet, so these read ``raw_input`` instead of an output payload.
# Anything missing falls back to the generic approval detail, never to a blank body.


def _approval_shell(state: ToolViewState) -> Text:
    args = _args(state)
    command = args.get("command") or args.get("cmd")
    if not command:
        return _pending(state)
    return _shell_body({"command": command})


def _approval_write(state: ToolViewState) -> Text:
    args = _args(state)
    path = args.get("path") or args.get("file_path")
    content = str(args.get("content") or "")
    if not path and not content:
        return _pending(state)
    rows = [f"+ {line}" for line in content.splitlines()]
    return _diff_body(
        {
            "path": path or "",
            "verb": "write",
            "added": len(rows),
            "removed": 0,
            "diff": rows,
        }
    )


def _approval_edit(state: ToolViewState) -> Text:
    args = _args(state)
    old = str(args.get("old_text") or args.get("old_string") or "")
    new = str(args.get("new_text") or args.get("new_string") or "")
    if not old and not new:
        return _pending(state)
    old_rows = [f"- {line}" for line in old.splitlines()]
    new_rows = [f"+ {line}" for line in new.splitlines()]
    return _diff_body(
        {
            "path": args.get("path") or args.get("file_path") or "",
            "verb": "edit",
            "added": len(new_rows),
            "removed": len(old_rows),
            "diff": old_rows + new_rows,
        }
    )


def _approval_read(state: ToolViewState) -> Text:
    args = _args(state)
    path = args.get("path") or args.get("file_path")
    if not path:
        return _pending(state)
    text = Text()
    text.append(readable_text(path, limit=400), style="bold")
    offset = int(args.get("offset") or 0)
    limit = args.get("limit")
    if offset:
        text.append(f"  from line {offset + 1}", style="dim")
    elif limit:
        text.append(f"  first {int(limit)} lines", style="dim")
    return text


def _approval_list(state: ToolViewState) -> Text:
    args = _args(state)
    path = args.get("path")
    glob = args.get("glob")
    if not path and not glob:
        return _pending(state)
    text = Text()
    text.append(readable_text(path or ".", limit=400), style="bold")
    text.append(f"  {readable_text(glob, limit=200) or 'all files'}", style="dim")
    return text


def _approval_grep(state: ToolViewState) -> Text:
    args = _args(state)
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return _pending(state)
    text = Text()
    text.append(readable_text(pattern, limit=400), style="bold")
    scope = [str(args.get("path") or "")]
    if args.get("glob"):
        scope.append(str(args["glob"]))
    if args.get("ignore_case"):
        scope.append("ignore case")
    if args.get("literal"):
        scope.append("literal")
    joined = " · ".join(readable_text(part, limit=200) for part in scope if part)
    if joined:
        text.append(f"  {joined}", style="dim")
    return text


Render = Callable[[ToolViewState], Text]


class _Body:
    def __init__(self, render: Render) -> None:
        self._render = render
        self.widget = Static("", markup=False)

    def apply(self, state: ToolViewState) -> None:
        self.widget.update(self._render(state))


def _factory(render: Render) -> Callable[[ToolViewState, ViewContext], ViewHandle[Any]]:
    def build(state: ToolViewState, context: ViewContext) -> ViewHandle[Any]:
        del context
        body = _Body(render)
        body.apply(state)
        return ViewHandle(widget=body.widget, apply=body.apply)

    return build


class _Provider:
    def __init__(
        self,
        tool: Callable[[ToolViewState, ViewContext], ViewHandle[Any]],
        approval: Callable[[ToolViewState, ViewContext], ViewHandle[Any]],
    ) -> None:
        self.tool_view = tool
        self.approval_view = approval


ToolFactory: TypeAlias = Callable[[ToolViewState, ViewContext], ViewHandle[Any]]

_VIEWS: dict[str, ToolFactory] = {
    "harness.tool.read_file": _factory(_read_file),
    "harness.tool.write_file": _factory(_diff_text),
    "harness.tool.edit_file": _factory(_diff_text),
    "harness.tool.list_files": _factory(_list_files),
    "harness.tool.grep": _factory(_grep),
    "harness.tool.shell": _factory(_shell),
}

_APPROVAL_VIEWS: dict[str, ToolFactory] = {
    "harness.tool.read_file": _factory(_approval_read),
    "harness.tool.write_file": _factory(_approval_write),
    "harness.tool.edit_file": _factory(_approval_edit),
    "harness.tool.list_files": _factory(_approval_list),
    "harness.tool.grep": _factory(_approval_grep),
    "harness.tool.shell": _factory(_approval_shell),
}


def register_builtin_renderers(registry: Any) -> None:
    """Install the bundled tool and approval views on a client renderer registry."""
    for renderer_id, tool in _VIEWS.items():
        registry.register(renderer_id, 1, _Provider(tool, _APPROVAL_VIEWS[renderer_id]))
