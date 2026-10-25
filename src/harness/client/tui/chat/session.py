"""Session load, resume, new, delete, close, and list workflow."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ...errors import ExtensionUnavailable, HarnessClientError
from ..composer import Composer
from ..dialogs import ConfirmDialog, FormDialog
from ..panels import SidePanel
from ..transcript import TranscriptPane

if TYPE_CHECKING:
    from ..screens import ChatScreen


class SessionWorkflow:
    """Owns the session lifecycle for one chat screen."""

    def __init__(self, screen: ChatScreen) -> None:
        self.screen = screen

    async def refresh(self, *, load_more: bool = False) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).sessions
        if not screen.client.has_session_capability("list"):
            panel.show_message("This agent does not advertise session/list.")
            return
        if not load_more:
            screen._session_rows = []
            screen._session_cursor = None
        if load_more and not screen._session_cursor:
            panel.show_message("No further session pages are advertised.")
            return
        try:
            listing = await screen.client.list_sessions(cursor=screen._session_cursor)
        except HarnessClientError as exc:
            panel.show_message(exc.message)
            return
        known = {
            str(item.get("sessionId") or item.get("session_id") or "")
            for item in screen._session_rows
        }
        for item in listing.sessions:
            session_id = str(item.get("sessionId") or item.get("session_id") or "")
            if session_id and session_id not in known:
                screen._session_rows.append(item)
                known.add(session_id)
        screen._session_cursor = listing.next_cursor
        panel.set_sessions(
            list(screen._session_rows),
            can_delete=screen.client.has_session_capability("delete"),
            can_close=screen.client.has_session_capability("close"),
            can_resume=screen.client.has_session_capability("resume"),
            can_load=screen.client.supports_load,
            next_cursor=screen._session_cursor,
        )

    def session_cwd(self, session_id: str) -> str:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).sessions
        cwd = panel.cwd_for(session_id)
        return cwd or screen.target.cwd

    async def load(self, session_id: str, *, cwd: str, initial: bool = False) -> None:
        screen = self.screen
        if screen.busy and not initial:
            await screen._notice("Stop the running turn before switching sessions.", "warning")
            return
        if screen._loading_session or screen._replay_target is not None:
            await screen._notice("Wait for the current load to finish.", "warning")
            return
        if not screen.client.supports_load:
            await screen._notice(
                "This agent does not advertise session/load; the conversation cannot be reopened.",
                "warning",
            )
            if initial:
                await self.new()
            return
        pane = screen.query_one("#transcript", TranscriptPane)
        screen._reset_activity()
        screen._loading_session = True
        screen._replay_target = session_id
        pane.begin_replay()
        try:
            loaded = await screen.client.load_session(session_id, cwd)
            await screen.client.drain_events()
        except HarnessClientError as exc:
            await pane.end_replay()
            screen._replay_target = None
            screen._loading_session = False
            await screen._notice(f"Could not load session {session_id}: {exc.message}", "error")
            if initial:
                await self.new()
            return
        await pane.end_replay()
        tool_ids = pane.retained_tool_ids()
        if tool_ids and screen.client.extension.supports("_harness/presentation/get"):
            try:
                snapshots = await loaded.presentations(tool_ids)
            except (ExtensionUnavailable, HarnessClientError):
                snapshots = ()
            for snapshot in snapshots:
                await pane.apply_presentation(snapshot)
        screen._replay_target = None
        screen._loading_session = False
        screen.prompts.save_draft()
        screen.session = loaded
        screen.prompts.restore_draft()
        screen._bind_view_context()
        screen.app.session = loaded
        screen._update_chrome()
        await screen._notice(f"Loaded session {session_id}.", "success")
        screen.query_one("#composer", Composer).focus()
        screen._refresh_panels()

    async def resume(self, session_id: str) -> None:
        screen = self.screen
        # This terminal does not retain a verified complete transcript cache.
        # CONCEPT §14.2 therefore requires authoritative session/load; resume is
        # never used as an unverified shortcut.
        if not screen.client.has_session_capability("resume"):
            await screen._notice(
                "This agent does not advertise session/resume and this terminal has no "
                "verified cache; authoritative history can be loaded when available.",
                "warning",
            )
            return
        if screen.client.supports_load:
            await self.load(session_id, cwd=self.session_cwd(session_id))
            return
        await screen._notice(
            "session/load is not advertised and this terminal has no verified cache; "
            "session/resume would discard authoritative history, so it was not used.",
            "warning",
        )

    async def new(self) -> None:
        screen = self.screen
        if screen.busy:
            await screen._notice("Stop the running turn before creating a session.", "warning")
            return
        if screen._loading_session or screen._replay_target is not None:
            await screen._notice("Wait for the current session load to finish.", "warning")
            return
        try:
            session = await screen.client.new_session(screen.target.cwd)
        except HarnessClientError as exc:
            await screen._notice(f"Could not create a session: {exc.message}", "error")
            return
        screen.prompts.save_draft()
        screen.session = session
        screen.prompts.restore_draft()
        screen._bind_view_context()
        screen.app.session = session
        screen._reset_activity()
        await screen.query_one("#transcript", TranscriptPane).clear_transcript()
        screen._update_chrome()
        await screen._notice(f"Started session {session.session_id}.", "success")
        screen.query_one("#composer", Composer).focus()
        screen._refresh_panels()

    def confirm_delete(self, session_id: str) -> None:
        screen = self.screen

        def done(result: bool | None) -> None:
            if result:
                screen._delete_session(session_id)

        screen.app.push_screen(
            ConfirmDialog(
                "Delete session",
                f"Delete session {session_id}? Its conversation and owned state are removed. "
                "This cannot be undone.",
                confirm_label="Delete session",
            ),
            done,
        )

    async def delete(self, session_id: str) -> None:
        screen = self.screen
        if not screen.client.has_session_capability("delete"):
            await screen._notice("This agent does not advertise session/delete.", "warning")
            return
        if screen.busy and session_id == screen.session.session_id:
            await screen._notice("Stop the running turn before deleting its session.", "warning")
            return
        if screen._loading_session or screen._replay_target is not None:
            await screen._notice("Wait for the current session load to finish.", "warning")
            return
        try:
            await screen.client.delete_session(session_id)
        except HarnessClientError as exc:
            await screen._notice(f"Delete failed: {exc.message}", "error")
            return
        await screen._notice(f"Deleted session {session_id}.", "success")
        if session_id == screen.session.session_id:
            await self.new()
        screen.refresh_sessions()

    async def close(self, session_id: str) -> None:
        screen = self.screen
        if not screen.client.has_session_capability("close"):
            await screen._notice("This agent does not advertise session/close.", "warning")
            return
        if screen._loading_session or screen._replay_target is not None:
            await screen._notice("Wait for the current session load to finish.", "warning")
            return
        try:
            await screen.client.close_session(session_id)
        except HarnessClientError as exc:
            await screen._notice(f"Close failed: {exc.message}", "error")
            return
        await screen._notice(
            f"Closed session {session_id}; history is retained by the agent.", "success"
        )
        if session_id == screen.session.session_id:
            await self.new()
        screen.refresh_sessions()

    async def load_more(self) -> None:
        await self.refresh(load_more=True)

    async def rename(self, title: str) -> None:
        screen = self.screen
        if not screen.client.extension.supports("_harness/session/rename"):
            await screen._notice("This agent does not support session renaming.", "warning")
            return
        if not title.strip():

            def apply(values: dict[str, Any] | None) -> None:
                if values:
                    asyncio.create_task(self.rename(str(values["title"])))

            screen.app.push_screen(
                FormDialog(
                    "Rename session",
                    "Choose a name for this conversation.",
                    [
                        {
                            "name": "title",
                            "title": "Session name",
                            "kind": "string",
                            "required": True,
                            "default": screen._session_title or "",
                        }
                    ],
                ),
                apply,
            )
            return
        try:
            renamed = await screen.session.rename(title.strip())
        except (HarnessClientError, ValueError) as exc:
            await screen._notice(str(exc), "error")
            return
        screen._session_title = renamed
        screen._update_chrome()
        screen.refresh_sessions()
        await screen._notice(f"Session renamed to {screen._session_title}.", "success")
