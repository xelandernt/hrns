"""Activity panel: connection, commands, usage, and session details."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from ..state import format_usage


class ActivityPanel(VerticalScroll):
    """Commands, usage and connection details in one scroll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connection = Static("Not connected.", markup=False, classes="activity-section")
        self._commands = Static(
            "No agent commands advertised.", markup=False, classes="activity-section"
        )
        self._usage = Static("No usage reported.", markup=False, classes="activity-section")
        self._session = Static("", markup=False, classes="activity-section")

    def compose(self) -> ComposeResult:
        yield Label("Connection", classes="section-title")
        yield self._connection
        yield Label("Agent commands", classes="section-title")
        yield self._commands
        yield Label("Usage", classes="section-title")
        yield self._usage
        yield Label("Session", classes="section-title")
        yield self._session

    def set_connection(self, lines: list[str]) -> None:
        self._connection.update("\n".join(lines))

    def set_commands(self, commands: tuple[dict[str, Any], ...]) -> None:
        if not commands:
            self._commands.update("No agent commands advertised.")
            return
        rendered = []
        for command in commands[:100]:
            name = command.get("name", "")
            description = command.get("description", "")
            rendered.append(f"/{name} — {description}" if description else f"/{name}")
        self._commands.update("\n".join(rendered))

    def set_usage(self, summary: dict[str, Any] | None) -> None:
        if not summary:
            self._usage.update("No usage reported.")
            return
        self._usage.update("Session total\n" + format_usage(summary).replace(" · ", "\n"))

    def set_session(self, lines: list[str]) -> None:
        self._session.update("\n".join(lines))
