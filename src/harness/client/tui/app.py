"""The Harness Textual application: theme, workers, dialogs and cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.theme import Theme

from ..client import HarnessAcpClient, SessionProxy
from ..errors import HarnessClientError, InteractionRequired
from ..interactions import (
    ElicitationAnswer,
    ElicitationRequest,
    InteractionPolicy,
    PermissionDecision,
    PermissionRequest,
    auto_permission,
)
from ..launch import ConnectionTarget, transport_for
from ..local import local_model_catalog
from ..profiles import Profiles
from .dialogs import ElicitationDialog, PermissionDialog
from .screens import ChatScreen, ConnectScreen
from .state import Settings

__all__ = ["HarnessApp", "run_terminal"]

HARNESS_THEME = Theme(
    name="harness",
    primary="#c9a86a",
    secondary="#6f8f8a",
    accent="#c9a86a",
    foreground="#d9dee3",
    background="#0e1114",
    surface="#151a1e",
    panel="#1a2026",
    success="#7fa77f",
    warning="#cf9f5f",
    error="#c77b7b",
    dark=True,
    variables={
        "text-muted": "#8b93a3",
        "block-cursor-background": "#c9a86a",
    },
)


class UiInteractionPolicy(InteractionPolicy):
    """Approvals answered inline, elicitation answered by a dialog."""

    def __init__(self, app: HarnessApp) -> None:
        self.app = app

    async def choose_permission(self, request: PermissionRequest) -> PermissionDecision:
        return await self.app.ask_permission(request)

    async def answer_elicitation(self, request: ElicitationRequest) -> ElicitationAnswer:
        return await self.app.ask_elicitation(request)


class HarnessApp(App[int]):
    """Full-screen, keyboard-first ACP client."""

    TITLE = "harness"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit_app", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit_app", "Quit", show=False, priority=True),
    ]

    CSS = """
    Screen {
        background: $background;
        color: $foreground;
    }

    /* ------------------------------------------------------------ header */

    #topbar {
        dock: top;
        height: auto;
        max-height: 5;
        padding: 1 2 1 2;
        background: $surface;
        border-bottom: solid $accent 30%;
    }
    #topbar :focus { background: $surface; }

    /* ------------------------------------------------------- chat layout */

    #main { height: 1fr; }

    #transcript {
        width: 1fr;
        padding: 1 2;
        scrollbar-size-vertical: 1;
    }
    #transcript:focus { border: none; }
    #new-output {
        height: 1; min-height: 1; border: none; margin: 0 2; width: auto;
    }
    .navigation-target { background: $accent 10%; }
    #transcript-results { height: 1fr; min-height: 4; }

    .role-you,
    .role-agent {
        height: auto;
    }
    .message-role {
        color: $accent;
        margin-top: 1;
        text-style: bold;
    }
    .role-you .message-body {
        border-left: thick $accent;
        padding-left: 1;
    }
    .role-agent .message-body {
        padding-left: 1;
        margin-bottom: 1;
    }
    .reasoning-body {
        color: $text-muted;
        padding-left: 1;
    }
    .notice {
        margin: 0 0 1 0;
        padding: 0 1;
        border-left: thick $secondary;
        color: $text-muted;
    }
    .notice-warning { border-left: thick $warning; color: $foreground; }
    .notice-error { border-left: thick $error; color: $error; }
    .notice-success { border-left: thick $success; color: $foreground; }

    .tool-card {
        background: transparent;
        border-left: tall $secondary 40%;
        padding: 0 1;
        margin: 0 0 1 1;
        height: auto;
    }
    .tool-card:focus { background: transparent; }
    .tool-card .tool-header { height: auto; }
    .tool-card .tool-output,
    .tool-card .tool-output-expanded {
        padding: 0 0 0 3;
        color: $text-muted;
        height: auto;
        display: none;
    }
    .tool-card .tool-output.visible,
    .tool-card .tool-output-expanded.visible {
        display: block;
    }

    .tool-card .tool-view {
        padding: 0 0 0 3;
        height: auto;
    }

    /* --------------------------------------------------------- side panel */

    #side-panel {
        width: 46%;
        min-width: 36;
        max-width: 76;
        background: $panel;
        border-left: solid $accent 25%;
    }
    #side-panel Tabs {
        background: $panel;
    }
    #side-panel TabPane {
        padding: 0;
    }
    #side-panel TabbedContent {
        height: 1fr;
    }
    .section-title {
        color: $text-muted;
        padding: 1 1 0 1;
        text-style: italic;
    }
    .panel-status {
        padding: 0 1;
        color: $text-muted;
        height: auto;
        max-height: 4;
    }
    .panel-detail {
        padding: 1;
        color: $foreground;
        height: auto;
        max-height: 16;
    }
    .panel-hint {
        padding: 0 1 1 1;
        color: $text-muted;
        height: auto;
    }
    #binding-list, #choice-list, #command-list, #permission-options, #approval-options {
        background: transparent;
        height: auto;
        max-height: 14;
        scrollbar-size-vertical: 1;
    }
    #binding-list:focus, #choice-list:focus, #command-list:focus, #permission-options:focus,
    #approval-options:focus {
        border: none;
    }
    #session-table {
        height: 1fr;
        background: transparent;
    }
    #session-panel { height: 1fr; }
    #capability-panel { height: 1fr; }
    #activity-panel {
        padding: 0 1 1 1;
        height: 1fr;
    }
    .activity-section {
        color: $foreground;
        padding: 0 1;
        height: auto;
    }

    /* ----------------------------------------------------------- composer */

    #approval {
        height: auto;
        max-height: 16;
        margin: 0 1;
        padding: 0 1;
        background: $surface;
        border: solid $warning;
    }
    #approval .approval-header {
        height: auto;
        padding: 0 0 1 0;
    }
    #approval .approval-detail {
        height: auto;
        max-height: 9;
        padding: 0 0 0 1;
        color: $foreground;
    }
    #approval .approval-hint {
        height: auto;
        color: $text-muted;
        padding: 0 0 0 1;
    }
    #approval-options {
        background: transparent;
        height: auto;
        max-height: 5;
        scrollbar-size-vertical: 1;
    }
    #approval-options:focus { border: none; }

    #composer-row {
        dock: bottom;
        height: 6;
        min-height: 4;
        max-height: 10;
        padding: 1 2;
        background: $panel;
    }
    #composer-prompt {
        width: 2;
        height: 1;
        color: $accent 70%;
    }
    #composer {
        width: 1fr;
        height: 1fr;
        padding: 0;
        border: none;
        background: transparent;
    }
    #command-suggestions {
        dock: bottom;
        offset: 0 -6;
        height: auto;
        max-height: 10;
        margin: 0 1;
        padding: 0 1;
        background: $panel;
        border: solid $accent 35%;
        scrollbar-size-vertical: 1;
    }
    #command-suggestions:focus { border: solid $accent 35%; }
    #statusbar {
        dock: bottom;
        height: 1;
        padding: 0 2;
        background: $background;
        color: $text-muted;
    }
    .narrow #topbar { padding: 0 1; }
    .narrow #composer-row { padding: 1 1; }
    .narrow .message-role { margin-top: 0; }

    /* --------------------------------------------------------- picker */

    #connect-banner {
        padding: 1 2;
        background: $surface;
        border-bottom: solid $accent 35%;
        height: auto;
    }
    #connect-body { height: 1fr; }
    #connect-list-wrap {
        width: 46%;
        min-width: 32;
        max-width: 70;
        border-right: solid $accent 20%;
        padding: 0 0 1 1;
    }
    #target-list {
        height: 1fr;
        background: transparent;
    }
    #connect-filter {
        margin: 0 1;
        background: $surface;
        border: solid $accent 30%;
    }
    #connect-filter:focus { border: solid $accent; }
    #target-list:focus { border: none; }
    #target-list ListItem {
        padding: 0 1;
        background: transparent;
    }
    #target-list ListItem.-highlight {
        background: $boost;
        border-left: thick $accent;
    }
    #connect-detail {
        width: 1fr;
        padding: 1 2;
    }
    #connect-status {
        height: auto;
        max-height: 4;
        padding: 0 2;
        color: $text-muted;
        background: $surface;
    }
    #connect-hint {
        height: 1;
        padding: 0 2;
        background: $panel;
    }

    /* --------------------------------------------------------- dialogs */

    ModalScreen {
        align: center middle;
        overflow-y: auto;
    }
    .dialog {
        width: 86;
        max-width: 94%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: thick $accent 60%;
    }
    .dialog-tall { height: 70%; }
    .dialog-title {
        text-style: bold;
        padding-bottom: 0;
    }
    .dialog-subtitle {
        color: $text-muted;
        padding-bottom: 1;
    }
    .dialog-help {
        color: $text-muted;
        padding: 0 0 1 0;
        height: auto;
    }
    .dialog-hint {
        color: $text-muted;
        padding-top: 1;
        height: auto;
    }
    .dialog-error {
        color: $error;
        height: auto;
        padding: 0 0 1 0;
    }
    .form-scroll {
        height: auto;
        max-height: 18;
        scrollbar-size-vertical: 1;
    }
    .field-label {
        padding-top: 1;
        text-style: bold;
    }
    .field-help {
        color: $text-muted;
        height: auto;
    }
    .button-row {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }
    .button-row Button { margin-left: 1; }
    .permission-detail {
        height: auto;
        max-height: 8;
        color: $text-muted;
        padding-bottom: 1;
    }
    .url-box {
        background: $panel;
        padding: 1;
        height: auto;
        color: $foreground;
    }
    """

    def __init__(
        self,
        profiles: Profiles,
        *,
        initial_target: ConnectionTarget | None = None,
        initial_session: str | None = None,
        status_message: str | None = None,
    ) -> None:
        super().__init__()
        self.profiles = profiles
        self.initial_target = initial_target
        self.initial_session = initial_session
        self.status_message = status_message
        self.settings = Settings.from_mapping(profiles.tui)
        self.client: HarnessAcpClient | None = None
        self.session: SessionProxy | None = None
        self.target: ConnectionTarget | None = None
        self._shutting_down = False
        self._approval_lock = asyncio.Lock()

    # --------------------------------------------------------------- lifecycle

    def on_mount(self) -> None:
        self.register_theme(HARNESS_THEME)
        theme = self.settings.theme if self.settings.theme else "harness"
        try:
            self.theme = theme
        except Exception:
            self.theme = "harness"
        self._warm_local_catalog()
        self.push_screen(
            ConnectScreen(
                self.profiles,
                initial_target=self.initial_target,
                auto_connect=self.initial_target is not None,
                status_message=self.status_message,
            )
        )

    @work(thread=True, group="local-catalog", exclusive=True)
    def _warm_local_catalog(self) -> None:
        """Load the Pydantic AI provider/model catalog before the dialog opens."""
        local_model_catalog()

    def make_client(self) -> HarnessAcpClient:
        if self.target is None:
            raise HarnessClientError("No connection target is selected.")
        target = self.target
        return HarnessAcpClient(
            lambda: transport_for(target),
            policy=UiInteractionPolicy(self),
            auth_method=target.profile.auth_method,
        )

    async def connect_target(self, target: ConnectionTarget) -> ChatScreen:
        """Connect, open the initial session, and push the chat workspace."""
        client = HarnessAcpClient(
            lambda: transport_for(target),
            policy=UiInteractionPolicy(self),
            auth_method=target.profile.auth_method,
        )
        await client.connect()
        self.target = target
        self.client = client
        try:
            initial_load = False
            if self.initial_session:
                session = SessionProxy(client, self.initial_session, target.cwd)
                initial_load = True
            else:
                session = await client.new_session(target.cwd)
            self.session = session
            screen = ChatScreen(
                client,
                session,
                target,
                self.settings,
                initial_session_id=self.initial_session if initial_load else None,
                initial_load=initial_load,
            )
            await self.push_screen(screen)
        except BaseException:
            # A failed session/new must not leave a connected orphan client.
            self.client = None
            self.session = None
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        return screen

    @work(group="turn", exclusive=True)
    async def start_turn(
        self, screen: ChatScreen, text: str, blocks: list[Any] | None = None
    ) -> None:
        """Run one prompt in an app-owned worker so panel switches do not cancel it."""
        try:
            if blocks is not None:
                await screen.session.prompt_blocks(blocks, display_text=text)
            else:
                await screen.session.prompt(text)
        except InteractionRequired as exc:
            screen._queue_paused = True
            if screen in self.screen_stack:
                await screen._notice(exc.message, "error")
        except asyncio.CancelledError:
            screen._queue_paused = True
            raise
        except HarnessClientError as exc:
            screen._queue_paused = True
            if screen in self.screen_stack and screen.connected:
                await screen._notice(f"Turn failed: {exc.message}", "error")
        except Exception as exc:  # protocol/transport surprises must be recoverable
            screen._queue_paused = True
            logging.getLogger(__name__).exception("turn failed")
            if screen in self.screen_stack:
                await screen._notice(f"Turn failed: {exc}", "error")
        finally:
            if screen in self.screen_stack:
                await screen._events.join()
                screen._set_busy(False)
                screen._refresh_queue()
                self.call_later(screen.send_next_queued)

    async def choose_agent(self, screen: ChatScreen) -> None:
        if screen.busy:
            await screen._notice("Stop the running turn before switching agents.", "warning")
            return
        await self.disconnect_current()
        with contextlib.suppress(Exception):
            await self.pop_screen()

    async def disconnect_current(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        self.session = None

    async def ask_permission(self, request: PermissionRequest) -> PermissionDecision:
        """Answer a tool approval inline, or automatically when the mode allows it.

        The modal remains only as a fallback when another screen is on top, so a
        permission request can never be buried behind an unrelated dialog.
        """
        decision = auto_permission(self.settings.permission_mode, request)
        if decision is not None:
            return decision

        async with self._approval_lock:
            # The mode may have changed while this request waited for the lock.
            decision = auto_permission(self.settings.permission_mode, request)
            if decision is not None:
                return decision
            if not self.screen_stack:
                return PermissionDecision(option_id=None)
            screen = self.screen
            if not isinstance(screen, ChatScreen):
                return await self._ask_permission_dialog(request)
            decision = await screen.request_approval(request)
            if decision.option_id is None:
                screen.expect_redirect()
            return decision

    def _chat_screen(self) -> ChatScreen | None:
        for screen in reversed(self.screen_stack):
            if isinstance(screen, ChatScreen):
                return screen
        return None

    async def _ask_permission_dialog(self, request: PermissionRequest) -> PermissionDecision:
        future: asyncio.Future[PermissionDecision] = asyncio.get_running_loop().create_future()

        def done(result: str | None) -> None:
            if not future.done():
                future.set_result(PermissionDecision(option_id=result))

        try:
            self.push_screen(PermissionDialog(request), done)
        except Exception:
            return PermissionDecision(option_id=None)
        try:
            return await future
        except asyncio.CancelledError:
            return PermissionDecision(option_id=None)

    async def ask_elicitation(self, request: ElicitationRequest) -> ElicitationAnswer:
        future: asyncio.Future[ElicitationAnswer] = asyncio.get_running_loop().create_future()

        def done(result: ElicitationAnswer | None) -> None:
            if not future.done():
                future.set_result(result or ElicitationAnswer(action="decline"))

        try:
            self.push_screen(ElicitationDialog(request), done)
        except Exception:
            return ElicitationAnswer(action="decline")
        try:
            return await future
        except asyncio.CancelledError:
            return ElicitationAnswer(action="cancel")

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        if settings.theme:
            with contextlib.suppress(Exception):
                self.theme = settings.theme
        self.profiles.tui = {
            **(self.profiles.tui or {}),
            **settings.to_mapping(),
        }
        if self.profiles.sources:
            with contextlib.suppress(Exception):
                self.profiles.save(self.profiles.sources[0])

    # ----------------------------------------------------------------- quitting

    async def action_quit_app(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        screen = self.screen
        if isinstance(screen, ChatScreen) and screen.busy:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(screen.session.cancel(), timeout=3)
        await self.disconnect_current()
        self.exit(0)

    async def shutdown_client(self) -> None:
        await self.disconnect_current()


def run_terminal(
    profiles: Profiles,
    *,
    initial_target: ConnectionTarget | None = None,
    initial_session: str | None = None,
    status_message: str | None = None,
) -> int:
    """Run the terminal app to completion and always close the agent connection."""
    app = HarnessApp(
        profiles,
        initial_target=initial_target,
        initial_session=initial_session,
        status_message=status_message,
    )

    async def _main() -> int:
        try:
            result = await app.run_async()
            return int(result or 0)
        finally:
            await app.shutdown_client()

    return asyncio.run(_main())
