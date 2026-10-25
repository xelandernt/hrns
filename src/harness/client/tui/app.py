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
    AskUserRequest,
    AskUserResponse,
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
from .connection_screen import ConnectScreen
from .dialogs import ElicitationDialog, PermissionDialog
from .screens import ChatScreen
from .state import Settings

__all__ = ["HarnessApp", "run_terminal"]

HARNESS_THEME = Theme(
    name="harness",
    primary="#b79cff",
    secondary="#b79cff",
    accent="#b79cff",
    foreground="#e4e0ea",
    background="#0c0b0f",
    surface="#141218",
    panel="#1a171f",
    success="#7fb98c",
    warning="#d9a561",
    error="#d47c7c",
    dark=True,
    variables={
        "text-muted": "#8d8798",
        "border": "#2d2933",
        "block-cursor-background": "#b79cff",
        "block-cursor-foreground": "#0c0b0f",
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

    async def answer_questions(self, request: AskUserRequest) -> AskUserResponse:
        for screen in reversed(self.app.screen_stack):
            if isinstance(screen, ChatScreen):
                return await screen.request_questions(request)
        return AskUserResponse(cancelled=True)


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

    /* ------------------------------------------------------- chat layout */

    #main {
        height: 1fr;
        layers: base drawer;
    }

    #transcript {
        layer: base;
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
        margin-top: 1;
        text-style: bold;
    }
    .role-you .message-role { color: $accent; }
    .role-agent .message-role { color: $text-muted; }
    .role-you .message-body {
        border-left: tall $accent;
        padding-left: 1;
    }
    .role-agent .message-body {
        padding-left: 1;
        margin-bottom: 1;
    }
    /* Inline code and list markers stay neutral: the default Markdown theme
       tints them with the warning colour, which reads as red in the transcript. */
    MarkdownBlock:dark > .code_inline,
    MarkdownBlock:light > .code_inline {
        color: $foreground;
        background: $surface 70%;
    }
    Markdown MarkdownBullet {
        color: $text-muted;
    }
    .reasoning-body {
        color: $text-muted;
        padding-left: 1;
    }
    .notice {
        margin: 0 0 1 0;
        padding: 0 1;
        background: $surface 60%;
        border-left: thick $border;
        color: $text-muted;
    }
    .notice-warning { border-left: thick $warning; color: $foreground; }
    .notice-error { border-left: thick $error; color: $error; }
    .notice-success { border-left: thick $success; color: $foreground; }

    .tool-card {
        background: $surface 65%;
        border-left: tall $accent 35%;
        padding: 0 1;
        margin: 0 0 1 1;
        height: auto;
    }
    .tool-card:focus { background: $surface 85%; }
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
        layer: drawer;
        dock: right;
        height: 1fr;
        width: 46%;
        min-width: 36;
        max-width: 76;
        background: $panel;
        border: round $border;
        border-left: solid $accent 60%;
    }
    #side-panel Tabs {
        background: $panel;
    }
    #side-panel Tab.-active {
        color: $accent;
        text-style: bold;
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
        text-style: bold;
    }
    .panel-status {
        padding: 0 1;
        color: $text-muted;
        height: auto;
        max-height: 4;
    }
    .panel-hint {
        padding: 0 1 1 1;
        color: $text-muted;
        height: auto;
    }
    .panel-detail {
        padding: 1 1;
        color: $text-muted;
        height: auto;
        max-height: 12;
    }
    #binding-list, #mcp-list, #choice-list, #command-list, #permission-options, #approval-options {
        background: transparent;
        height: auto;
        max-height: 14;
        scrollbar-size-vertical: 1;
    }
    #binding-list, #mcp-list {
        height: 1fr;
        max-height: 1fr;
    }
    #binding-list:focus, #mcp-list:focus, #choice-list:focus, #command-list:focus, #permission-options:focus,
    #approval-options:focus {
        border: none;
    }
    #session-table {
        height: 1fr;
        background: transparent;
    }
    #session-panel { height: 1fr; }
    #capability-panel { height: 1fr; }
    #todo-panel {
        padding: 0 1 1 1;
        height: 1fr;
    }
    .todo-summary {
        color: $text-muted;
        padding: 0 1;
        height: auto;
        text-style: bold;
    }
    .todo-list {
        color: $foreground;
        padding: 0 1;
        height: auto;
    }
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

    #ask-user-dock,
    #approval-dock {
        height: auto;
        padding: 0 2;
        background: transparent;
    }
    #ask-user {
        height: auto;
        max-height: 18;
        margin: 0;
        padding: 0 1;
        background: $surface;
        border: round $accent 75%;
    }
    #ask-user .ask-tabs {
        height: auto;
        padding: 0 0 1 0;
    }
    #ask-user .ask-question {
        height: auto;
        padding-bottom: 1;
    }
    #ask-user #ask-options {
        background: transparent;
        height: auto;
        max-height: 8;
        scrollbar-size-vertical: 1;
    }
    #ask-user #ask-options:focus {
        border: none;
    }
    #ask-user #ask-custom-input {
        display: none;
        margin-top: 1;
    }
    #ask-user .ask-hint {
        height: auto;
        color: $text-muted;
    }
    #approval {
        height: auto;
        max-height: 16;
        margin: 0;
        padding: 0 1;
        background: $surface;
        border: round $warning 75%;
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
    #approval .approval-view {
        height: auto;
        max-height: 9;
        padding: 0 0 0 1;
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

    #composer-dock {
        dock: bottom;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 2;
        background: transparent;
    }
    #composer-row {
        width: 1fr;
        height: 6;
        min-height: 6;
        max-height: 10;
        padding: 0 1;
        background: $surface;
        border: round $border;
    }
    #composer-row:focus-within {
        border: round $accent 75%;
    }
    #composer-prompt {
        width: 2;
        height: 1;
        color: $accent;
        text-style: bold;
    }
    #composer {
        width: 1fr;
        height: 1fr;
        padding: 0;
        border: none;
        background: transparent;
    }
    #composer .text-area--placeholder {
        color: $text-muted;
    }
    #composer .text-area--skill-token {
        color: $success;
        text-style: bold;
    }
    #composer .text-area--file-token {
        color: $accent;
    }
    #suggestions-dock {
        dock: bottom;
        offset: 0 -8;
        height: auto;
        padding: 0 2;
        background: transparent;
    }
    #command-suggestions {
        width: 1fr;
        height: auto;
        max-height: 10;
        margin: 0;
        padding: 0 1;
        background: $panel;
        border: round $accent 45%;
        scrollbar-size-vertical: 1;
    }
    #command-suggestions:focus { border: round $accent 45%; }
    #statusbar {
        dock: bottom;
        height: 1;
        padding: 0 2;
        background: $background;
        color: $text-muted;
    }
    .narrow #composer-dock,
    .narrow #ask-user-dock,
    .narrow #approval-dock,
    .narrow #suggestions-dock { padding: 0 1; }
    .narrow .message-role { margin-top: 0; }

    /* --------------------------------------------------------- picker */

    #connect-banner {
        padding: 1 2;
        background: $surface;
        border-bottom: solid $accent 35%;
        height: auto;
    }
    #connect-body { height: 1fr; padding: 0 1; }
    #connect-list-wrap {
        width: 46%;
        min-width: 32;
        max-width: 70;
        margin: 0 1 0 0;
        padding: 0 1 1 1;
        background: $surface;
        border: round $border;
    }
    #target-list {
        height: 1fr;
        background: transparent;
    }
    #connect-filter {
        margin: 0 0 1 0;
        background: $surface;
        border: round $border;
    }
    #connect-filter:focus { border: round $accent 75%; }
    #target-list:focus { border: none; }
    #target-list ListItem {
        padding: 0 1;
        background: transparent;
    }
    #target-list ListItem.-highlight {
        background: $accent 12%;
        border-left: thick $accent;
    }
    #connect-detail {
        width: 1fr;
        padding: 1 2;
        background: $surface;
        border: round $border;
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
        border: round $accent 65%;
    }
    .dialog-tall { height: 70%; }
    .dialog-title {
        color: $accent;
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
    }
    .form-scroll {
        height: auto;
        max-height: 16;
        scrollbar-size-vertical: 1;
    }
    .field-row {
        height: 1;
    }
    .field-row:focus,
    .field-row:focus-within {
        background: $boost 35%;
    }
    .field-gutter {
        width: 2;
        color: $text-muted;
    }
    .field-row:focus .field-gutter,
    .field-row:focus-within .field-gutter {
        color: $accent;
        text-style: bold;
    }
    .field-name {
        width: 26;
        padding-right: 1;
        color: $text-muted;
    }
    .field-row:focus .field-name,
    .field-row:focus-within .field-name {
        color: $text;
    }
    .field-value {
        width: 1fr;
        color: $text;
    }
    .field-row Input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
    }
    .field-row Input:focus {
        border: none;
        background: transparent;
    }
    .field-focus-help {
        color: $text-muted;
        height: auto;
        max-height: 3;
        padding-top: 1;
    }
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
            return result or 0
        finally:
            await app.shutdown_client()

    return asyncio.run(_main())
