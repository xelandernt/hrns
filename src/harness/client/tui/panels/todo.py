"""Agent plan panel."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


class TodoPanel(VerticalScroll):
    """The current session's plan, as reported by the agent."""

    can_focus = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._summary = Static("No plan yet.", markup=False, classes="todo-summary")
        self._list = Static("", markup=False, classes="todo-list")
        self._entries: tuple[tuple[str, str, str], ...] = ()

    def compose(self) -> ComposeResult:
        yield self._summary
        yield self._list

    def set_plan(
        self,
        entries: tuple[tuple[str, str, str], ...],
        *,
        cleared: bool = False,
    ) -> None:
        """Replace the visible plan with a complete agent snapshot."""
        self._entries = () if cleared else entries
        if not self._entries:
            self._summary.update("No plan yet.")
            self._list.update("")
            return
        done = sum(1 for _, _, status in self._entries if status == "completed")
        total = len(self._entries)
        self._summary.update(f"{done} of {total} done")
        self._list.update(self._render_plan(self._entries))

    @staticmethod
    def _render_plan(entries: tuple[tuple[str, str, str], ...]) -> Text:
        body = Text()
        for content, priority, status in entries:
            if status == "completed":
                marker, style = "●", "dim"
            elif status == "in_progress":
                marker, style = "◐", "bold"
            elif status == "cancelled":
                marker, style = "✕", "dim strike"
            else:
                marker, style = "○", ""
            body.append(f"{marker} ", style="bold" if status == "in_progress" else "dim")
            body.append(content, style=style)
            if priority == "high":
                body.append("  high", style="bold yellow")
            body.append("\n")
        return body
