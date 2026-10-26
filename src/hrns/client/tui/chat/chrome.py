"""Chat chrome: settings, layout, status band, and activity reset."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from ...constants import CLIENT_VERSION
from ..connection_screen import _short_path
from ..panels import SidePanel
from ..state import (
    config_option_value,
    effort_option,
    format_context_detail,
    format_context_usage,
    format_cost,
    model_option,
    permission_mode_label,
)
from ..status import StatusBar
from ..transcript import TranscriptPane

if TYPE_CHECKING:
    from .screen import ChatScreen


class ChromeWorkflow:
    """Keeps the transcript options, side panel, and status band in sync."""

    def __init__(self, screen: ChatScreen) -> None:
        self.screen = screen

    def apply_settings(self) -> None:
        screen = self.screen
        pane = screen.query_one("#transcript", TranscriptPane)
        pane.auto_expand_tools = screen.settings.auto_expand_tools
        pane.show_thoughts = screen.settings.show_thoughts
        if screen.settings.theme in {
            "hrns",
            "textual-light",
            "textual-dark",
            "ansi-light",
            "ansi-dark",
        }:
            with contextlib.suppress(Exception):
                screen.app.theme = screen.settings.theme
        self.apply_layout()
        self.update_chrome()

    def apply_layout(self) -> None:
        screen = self.screen
        width = screen.app.size.width
        panel = screen.query_one("#side-panel", SidePanel)
        panel.display = screen._panel_open
        screen_class = "narrow" if width < 96 else "wide"
        if screen_class not in screen.classes:
            screen.add_class(screen_class)
        if width >= 96:
            screen.remove_class("narrow")
        else:
            screen.remove_class("wide")

    def update_chrome(self) -> None:
        screen = self.screen
        session_id = screen.session.session_id if screen.session else None
        screen.query_one("#side-panel", SidePanel).sessions.mark_current(session_id)
        self.refresh_status()
        activity = screen.query_one("#side-panel", SidePanel).activity
        location = screen.target.location()
        lines = [
            f"state: {'connected' if screen.connected else 'disconnected'}",
            f"location: {location.get('kind', 'unknown')}",
        ]
        if location.get("kind") == "local":
            lines.append(
                f"command: {screen.target.profile.command} {' '.join(screen.target.profile.args)}".rstrip()
            )
            lines.append(f"workspace: {screen.target.cwd}")
        else:
            lines.append(f"server: {screen.target.profile.url}")
            lines.append(f"agent: {screen.target.agent_id}")
            lines.append(f"remote workspace: {screen.target.cwd}")
        lines.append(f"client: hrns-client {CLIENT_VERSION}")
        if screen.client.extension.enabled:
            lines.append(f"extension: {screen.client.extension.describe()}")
            if screen.client.extension.local_binding_creation:
                lines.append("plugin creation: allowed by host")
        else:
            lines.append("extension: not advertised (standard ACP only)")
        activity.set_connection(lines)
        attached = screen.session is not None and screen.session.client is screen.client
        activity.set_session(
            [
                f"session: {session_id or '(none)'}",
                "attached: yes" if attached else "attached: no (reconnected without replay)",
                "selected/active revisions: available from the plugins panel when negotiated",
                f"user messages sent this run: {screen._turns_started}",
            ]
        )

    def refresh_status(self) -> None:
        screen = self.screen
        """Push the current session state into the one-line status band."""
        session = screen.session
        option = model_option(session.config_options) if session else None
        reasoning = effort_option(session.config_options) if session else None
        screen.query_one("#statusbar", StatusBar).show(
            connected=screen.connected,
            model=config_option_value(option) if option else "",
            effort=config_option_value(reasoning) if reasoning else "",
            mode=(session.current_mode_id or "") if session else "",
            usage=format_context_usage(screen._context_usage),
            cost=format_cost(screen._usage),
            usage_detail=format_context_detail(screen._context_usage, screen._usage),
            busy=screen.busy,
            activity=screen._activity,
            permission=permission_mode_label(screen.settings.permission_mode),
            hint=self.hint_text(),
            context=self.status_context(),
        )

    def status_context(self) -> str:
        screen = self.screen
        agent = f"{screen.client.agent_title} {screen.client.agent_version}".strip()
        location = screen.target.location()
        where = location.get("cwd") or location.get("server") or ""
        where = _short_path(where, 48) if where else ""
        session = screen._session_title or (screen.session.session_id if screen.session else "")
        return " · ".join(part for part in (agent, where, session) if part)

    def hint_text(self) -> str:
        screen = self.screen
        if screen._hint_note:
            return screen._hint_note
        if screen.busy:
            return "esc to stop"
        return ""

    def reset_activity(self) -> None:
        screen = self.screen
        """Drop activity that belongs to the previous session or connection."""
        screen._usage = None
        screen._context_usage = None
        screen._activity = ""
        screen._hint_note = ""
        screen._session_title = None
        screen._commands = ()
        screen._plugin_commands = ()
        screen._plugin_revision = 0
        screen._bindings_raw = []
        screen._turns_started = 0
        panel = screen.query_one("#side-panel", SidePanel)
        panel.todos.set_plan((), cleared=True)
        panel.activity.set_commands(())
        panel.activity.set_usage(None)
        screen._todo_auto_opened_for = None
        self.refresh_status()
