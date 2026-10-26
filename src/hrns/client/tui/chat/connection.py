"""Reconnect and authoritative session replay workflow."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from ...errors import HrnsClientError
from ..composer import Composer
from ..transcript import TranscriptPane

if TYPE_CHECKING:
    from .screen import ChatScreen


class ReconnectWorkflow:
    """Owns rebuilding a dropped ACP connection for one chat screen."""

    def __init__(self, screen: ChatScreen) -> None:
        self.screen = screen

    async def reconnect(self) -> None:
        screen = self.screen
        if screen not in screen.app.screen_stack:
            return
        if screen.busy:
            await screen._notice("Stop the running turn before reconnecting.", "warning")
            return
        if screen._loading_session or screen._replay_target is not None:
            await screen._notice("Wait for the current session load to finish.", "warning")
            return
        if not screen.connected:
            await screen._notice("Reconnecting…")
        session_id = screen.session.session_id
        cwd = screen.session.cwd
        old_client = screen.client
        with contextlib.suppress(Exception):
            await old_client.disconnect()
        try:
            new_client = screen.app.make_client()
            await new_client.connect()
        except HrnsClientError as exc:
            screen.connected = False
            if screen in screen.app.screen_stack:
                screen.chrome.update_chrome()
                await screen._notice(f"Reconnect failed: {exc.message}", "error")
            return
        if screen not in screen.app.screen_stack:
            with contextlib.suppress(Exception):
                await new_client.disconnect()
            return
        screen.client = new_client
        screen.app.client = new_client
        screen.connected = True
        screen.chrome.reset_activity()
        screen._subscribe()
        await screen._notice("Reconnected.", "success")
        if screen.client.supports_load:
            pane = screen.query_one("#transcript", TranscriptPane)
            screen._replay_target = session_id
            pane.begin_replay()
            try:
                loaded = await screen.client.load_session(session_id, cwd)
                await screen.client.drain_events()
            except HrnsClientError as exc:
                await pane.end_replay()
                screen._replay_target = None
                await screen._notice(
                    f"Reconnected, but the session could not be reloaded: {exc.message}. "
                    "Press Ctrl+N for a new session.",
                    "error",
                )
                screen.chrome.update_chrome()
                screen._refresh_panels()
                return
            await pane.end_replay()
            screen._replay_target = None
            if screen not in screen.app.screen_stack:
                return
            screen.session = loaded
            screen.app.session = loaded
            await screen._notice(
                f"Session {session_id} reloaded from authoritative history.", "success"
            )
        else:
            await screen._notice(
                "Reconnected. This agent does not advertise session/load, so the transcript cannot "
                "be replayed; press Ctrl+N for a new session.",
                "warning",
            )
        screen.chrome.update_chrome()
        screen.query_one("#composer", Composer).focus()
        screen._refresh_panels()
