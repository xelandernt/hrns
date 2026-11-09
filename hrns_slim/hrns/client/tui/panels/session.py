"""Session list panel and its events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Input, Static


class SessionLoadRequested(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionResumeRequested(Message):
    """Resume a session without asking the agent to replay its history."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionDeleteRequested(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionCloseRequested(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionNewRequested(Message):
    pass


class SessionRefreshRequested(Message):
    pass


class SessionMoreRequested(Message):
    pass


class SessionPanel(Vertical):
    """Advertised session list with explicit load/close/delete actions."""

    can_focus = True
    BINDINGS = [
        Binding("slash", "filter_sessions", "Find session", show=False),
        Binding("d", "delete_session", "Delete", show=False),
        Binding("x", "close_session", "Close", show=False),
        Binding("u", "resume_session", "Resume", show=False),
        Binding("n", "new_session", "New", show=False),
        Binding("m", "more_sessions", "More", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ids: dict[str, str] = {}
        self._cwds: dict[str, str] = {}
        self._sessions: list[dict[str, Any]] = []
        self._current_session: str | None = None
        self._session_notes = ""
        self._filter = Input(placeholder="Filter title or workspace…", id="session-filter")
        self._status = Static(
            "Sessions load when you open this panel.", markup=False, classes="panel-status"
        )
        self._table = DataTable(id="session-table", cursor_type="row", zebra_stripes=False)
        self._hint = Static(
            "u resume · d delete · x close · n new · r refresh · Esc close",
            markup=False,
            classes="panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._filter
        yield self._table
        yield self._hint

    def on_mount(self) -> None:
        self._table.add_columns("title", "updated", "workspace")
        self._table.cursor_type = "row"
        self.set_interval(60, self._render_sessions)

    def show_message(self, text: str) -> None:
        self._status.update(Text(text))

    def set_sessions(
        self,
        sessions: list[dict[str, Any]],
        *,
        can_delete: bool,
        can_close: bool,
        can_resume: bool = False,
        can_load: bool = True,
        next_cursor: str | None = None,
    ) -> None:
        self._sessions = list(sessions)
        delete_note = "" if can_delete else " · delete not advertised"
        close_note = "" if can_close else " · close not advertised"
        resume_note = "" if can_resume else " · resume not advertised"
        more = " · m load more pages" if next_cursor else ""
        self._session_notes = f"{more}{delete_note}{close_note}{resume_note}"
        self._render_sessions()
        actions = [] if can_load else ["load unavailable"]
        if can_resume:
            actions.append("u resume")
        if can_delete:
            actions.append("d delete")
        if can_close:
            actions.append("x close")
        if next_cursor:
            actions.append("m more")
        actions.extend(("/ filter", "n new session", "r refresh"))
        self._hint.update(Text(" · ".join(actions), style="dim"))

    def mark_current(self, session_id: str | None) -> None:
        if session_id != self._current_session:
            self._current_session = session_id
            self._render_sessions()

    def _render_sessions(self) -> None:
        selected = self.selected_session_id()
        self._table.clear()
        self._ids.clear()
        self._cwds.clear()
        needle = self._filter.value.strip().casefold()
        for item in self._sessions:
            session_id = str(item.get("sessionId") or item.get("session_id") or "")
            if not session_id:
                continue
            title = str(item.get("title") or "(untitled)")
            cwd = str(item.get("cwd") or "")
            self._cwds[session_id] = cwd
            if needle and needle not in f"{title} {cwd} {session_id}".casefold():
                continue
            updated = str(item.get("updatedAt") or item.get("updated_at") or "")
            try:
                timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                seconds = max(0, int((datetime.now(timezone.utc) - timestamp).total_seconds()))
                updated = (
                    "just now"
                    if seconds < 60
                    else f"{seconds // 60}m ago"
                    if seconds < 3600
                    else f"{seconds // 3600}h ago"
                    if seconds < 86400
                    else f"{seconds // 86400}d ago"
                )
            except ValueError:
                pass
            current = session_id == self._current_session
            label = Text(f"● {title}" if current else title, style="bold" if current else "")
            key = self._table.add_row(label, updated, cwd, key=session_id)
            self._ids[str(key.value)] = session_id
        if selected in self._ids:
            self._table.move_cursor(row=list(self._ids).index(selected))
        self._status.update(
            Text(
                f"{len(self._ids)}/{len(self._sessions)} sessions{self._session_notes}", style="dim"
            )
        )

    def action_filter_sessions(self) -> None:
        self._filter.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self._filter:
            event.stop()
            self._render_sessions()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._filter:
            event.stop()
            self._table.focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._filter.has_focus:
            event.stop()
            self._filter.value = ""
            self._table.focus()

    def selected_session_id(self) -> str | None:
        if self._table.row_count == 0:
            return None
        try:
            row_key, _ = self._table.coordinate_to_cell_key(self._table.cursor_coordinate)
        except Exception:
            return None
        return self._ids.get(str(row_key.value))

    def cwd_for(self, session_id: str) -> str | None:
        return self._cwds.get(session_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        session_id = self._ids.get(str(event.row_key.value))
        if session_id:
            self.post_message(SessionLoadRequested(session_id))

    def _selected(self) -> str | None:
        session_id = self.selected_session_id()
        if session_id is None:
            self.show_message("Select a session row first.")
        return session_id

    def action_delete_session(self) -> None:
        session_id = self._selected()
        if session_id:
            self.post_message(SessionDeleteRequested(session_id))

    def action_close_session(self) -> None:
        session_id = self._selected()
        if session_id:
            self.post_message(SessionCloseRequested(session_id))

    def action_resume_session(self) -> None:
        session_id = self._selected()
        if session_id:
            self.post_message(SessionResumeRequested(session_id))

    def action_new_session(self) -> None:
        self.post_message(SessionNewRequested())

    def action_more_sessions(self) -> None:
        self.post_message(SessionMoreRequested())

    def action_refresh(self) -> None:
        self.post_message(SessionRefreshRequested())
