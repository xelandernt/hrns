"""First-party tool bodies for the bundled coding capability.

Views are ordinary Textual widgets resolved through the same ``harness.renderers`` contract
third-party plugins use; they are registered programmatically because they ship inside the
client. Every renderer reads the validated presentation payload and falls back to plain text
when the payload is absent or shaped unexpectedly.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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
    if not data:
        return _pending(state)
    text = Text()
    text.append(str(data.get("path") or ""), style="bold")
    verb = "created" if data.get("created") else "edited"
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
    if not data:
        return _pending(state)
    text = Text()
    text.append("$ ", style="dim")
    text.append(readable_text(data.get("command") or "", limit=400), style="bold")
    if data.get("timedOut"):
        text.append("  timed out", style="yellow")
    else:
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
    def __init__(self, factory: Callable[[ToolViewState, ViewContext], ViewHandle[Any]]) -> None:
        self.tool_view = factory


ToolFactory: TypeAlias = Callable[[ToolViewState, ViewContext], ViewHandle[Any]]

_VIEWS: dict[str, ToolFactory] = {
    "harness.tool.read_file": _factory(_read_file),
    "harness.tool.write_file": _factory(_diff_text),
    "harness.tool.edit_file": _factory(_diff_text),
    "harness.tool.list_files": _factory(_list_files),
    "harness.tool.grep": _factory(_grep),
    "harness.tool.shell": _factory(_shell),
}


def register_builtin_renderers(registry: Any) -> None:
    """Install the bundled tool views on a client renderer registry."""
    for renderer_id, factory in _VIEWS.items():
        registry.register(renderer_id, 1, _Provider(factory))
