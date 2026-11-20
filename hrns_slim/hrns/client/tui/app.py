"""The hrns Textual application: theme, workers, dialogs and cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from importlib import import_module
from typing import TYPE_CHECKING, Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import Static

from hrns.shared.config import ClientIoMode, HrnsConfig, McpServerSettings, TuiSettings
from hrns.shared.config.mcp import server_target

from ...shared.errors import HrnsClientError, InteractionRequired, TransportError
from ..interactions import (
    ElicitationAnswer,
    ElicitationRequest,
    InteractionPolicy,
    PermissionDecision,
    PermissionRequest,
    auto_permission,
)
from ..launch import ConnectionTarget, transport_for
from ..runtime import agent_model_catalog
from ..skills import SkillMention
from .composer import Composer
from .messages import Submitted
from .screen_base import _HrnsScreen

if TYPE_CHECKING:
    from textual.widgets.text_area import Selection

    from ..client import HrnsAcpClient
    from ..mcp_provider import ClientMcpProvider, ConsentDecision
    from ..session import SessionProxy
    from .chat.screen import ChatScreen

InitialTarget = Callable[[], tuple[ConnectionTarget | None, str | None]]

__all__ = ["HrnsApp", "run_terminal"]

HRNS_THEME = Theme(
    name="hrns",
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


class _StartupScreen(_HrnsScreen):
    """An editable prompt while the first connection is opening."""

    def __init__(self) -> None:
        super().__init__()
        self.composer = Composer(placeholder="Draft a message…", id="composer")
        self.status = Static("Connecting…", id="statusbar", markup=False)

    def compose(self) -> ComposeResult:
        with Horizontal(id="composer-dock"):
            with Horizontal(id="composer-row"):
                yield Static("›", id="composer-prompt", markup=False)
                yield self.composer
        yield self.status

    def on_mount(self) -> None:
        self.composer.focus()
        self.call_after_refresh(self.app.start_initial_connection)

    def on_submitted(self, event: Submitted) -> None:
        event.stop()
        self.status.update("Connecting… Draft kept. Press Enter when the session is ready.")


class UiInteractionPolicy(InteractionPolicy):
    """Approvals answered inline, elicitation answered by a dialog."""

    def __init__(self, app: HrnsApp) -> None:
        self.app = app

    async def choose_permission(self, request: PermissionRequest) -> PermissionDecision:
        return await self.app.ask_permission(request)

    async def answer_elicitation(self, request: ElicitationRequest) -> ElicitationAnswer:
        chat = self.app.chat
        if request.kind == "form" and chat is not None and chat in self.app.screen_stack:
            return await chat.request_elicitation(request)
        return await self.app.ask_elicitation(request)


class HrnsApp(App[int]):
    """Full-screen, keyboard-first ACP client."""

    TITLE = "hrns"
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
    .message-header {
        margin-top: 1;
    }
    .message-role {
        text-style: bold;
    }
    .role-you .message-role { color: $accent; }
    .role-agent .message-role { color: $text-muted; }
    .role-you .message-body {
        height: auto;
    }
    .role-you .message-text { margin-left: 1; }
    .transcript-bar {
        dock: left;
        width: 1;
        height: 100%;
        background: $accent;
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
        padding: 0 1 0 0;
        height: auto;
        background: $surface 60%;
        color: $text-muted;
    }
    .notice .transcript-bar { background: $border; margin-right: 1; }
    .notice-warning { color: $foreground; }
    .notice-warning .transcript-bar { background: $warning; }
    .notice-error { color: $error; }
    .notice-error .transcript-bar { background: $error; }
    .notice-success { color: $foreground; }
    .notice-success .transcript-bar { background: $success; }

    .tool-card {
        background: $surface 65%;
        padding: 0 1 0 0;
        margin: 0 0 1 1;
        height: auto;
    }
    .tool-card .transcript-bar { background: $accent 35%; margin-right: 1; }
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

    #side-resize {
        display: none;
        layer: base;
        width: 1;
        height: 1fr;
        background: $border 70%;
    }
    #side-resize:hover,
    #side-resize:focus,
    #side-resize.dragging {
        background: $accent 70%;
    }
    #side-panel {
        height: 1fr;
        background: $panel;
        border: round $border;
        border-left: solid $accent 60%;
    }
    .sidebar-docked #side-panel {
        layer: base;
        min-width: 32;
        max-width: 76;
    }
    .sidebar-overlay #side-panel {
        layer: drawer;
        dock: right;
        width: 46%;
        min-width: 36;
        max-width: 76;
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

    #question-dock,
    #approval-dock {
        height: auto;
        padding: 0 2;
        background: transparent;
    }
    .question-bar {
        height: auto;
        max-height: 18;
        padding: 0 1;
        background: $surface;
        border: round $accent 75%;
    }
    .question-title {
        height: auto;
        padding-bottom: 1;
        color: $text;
        text-style: bold;
    }
    #approval {
        height: auto;
        margin: 0;
        padding: 0 1;
        background: $surface;
        border: round $warning 75%;
    }
    #approval .approval-header {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #approval .approval-content {
        height: auto;
        max-height: 20vh;
        scrollbar-size-vertical: 1;
    }
    #approval .approval-expand {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
        margin: 0;
        width: auto;
    }
    #approval .approval-detail {
        height: auto;
        padding: 0 0 0 1;
        color: $foreground;
    }
    #approval .approval-view {
        height: auto;
        padding: 0 0 0 1;
    }
    #approval .approval-hint {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
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
    .narrow #question-dock,
    .narrow #approval-dock,
    .narrow #suggestions-dock { padding: 0 1; }
    .narrow .message-header { margin-top: 0; }

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
    .model-picker .dialog {
        height: 90%;
        padding: 0 2;
    }
    .model-picker .dialog-subtitle { padding-bottom: 0; }
    .model-picker .dialog-hint { padding-top: 0; }
    #model-options, .model-picker #choice-list {
        height: 1fr;
        max-height: 100%;
        scrollbar-size-vertical: 1;
    }
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
        height: auto;
    }
    .question-tabs {
        height: 1;
    }
    .question-tab {
        width: auto;
        max-width: 24;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    .question-tab.active {
        color: $text;
        text-style: bold;
        background: $accent 25%;
    }
    .question-review {
        height: auto;
        padding: 0 1 1 1;
        color: $text;
    }
    .question-row {
        padding: 0 1 1 1;
    }
    .question-field-title {
        height: auto;
        color: $text;
        text-style: bold;
    }
    .question-field-text {
        height: auto;
        color: $text-muted;
        padding-bottom: 1;
    }
    .question-row .field-value {
        height: auto;
    }
    .question-custom-row {
        height: auto;
        min-height: 1;
        padding-left: 0;
    }
    .question-custom-row.question-cursor {
        background: $accent 20%;
    }
    .question-option-marker {
        width: 2;
        color: $text;
    }
    .question-option-label {
        width: auto;
        padding-right: 1;
        color: $text;
    }
    .field-line {
        height: auto;
        min-height: 1;
    }
    .field-row:focus,
    .field-row:focus-within {
        background: $boost 35%;
    }
    .field-badge {
        width: auto;
        padding-left: 1;
        color: $warning;
    }
    .field-suggestions {
        height: auto;
        max-height: 7;
        margin-left: 28;
        border: none;
        padding: 0;
        background: $panel;
        scrollbar-size-vertical: 1;
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
        config: HrnsConfig,
        *,
        resolve_initial_target: InitialTarget,
        initial_session: str | None = None,
        initial_session_cwd: str | None = None,
        client_io: ClientIoMode | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.resolve_initial_target = resolve_initial_target
        self.initial_session = initial_session
        self.initial_session_cwd = initial_session_cwd
        self.client_io = client_io
        self.client: HrnsAcpClient | None = None
        self.session: SessionProxy | None = None
        self.target: ConnectionTarget | None = None
        self.chat: ChatScreen | None = None
        self.startup_error: HrnsClientError | ValueError | OSError | KeyError | None = None
        self._startup_draft: tuple[str, Selection] | None = None
        self._shutting_down = False
        self._approval_lock = asyncio.Lock()

    # --------------------------------------------------------------- lifecycle

    def on_mount(self) -> None:
        self.register_theme(HRNS_THEME)
        theme = self.config.tui.theme if self.config.tui.theme else "hrns"
        try:
            self.theme = theme
        except Exception:
            self.theme = "hrns"
        self.push_screen(_StartupScreen())

    @work(group="connect", exclusive=True)
    async def start_initial_connection(self) -> None:
        try:
            target, error = await asyncio.to_thread(self.resolve_initial_target)
        except (HrnsClientError, ValueError, OSError, KeyError) as exc:
            self.startup_error = exc
            self.exit()
            return
        if self._shutting_down:
            return
        if target is not None:
            startup = self.screen
            if isinstance(startup, _StartupScreen):
                startup.status.update(f"Connecting to {target.location_text()}…")
            try:
                await self.connect_target(target)
            except Exception as exc:
                error = f"error · {exc}"
        if self._shutting_down:
            return
        if self.chat is None:
            await asyncio.to_thread(import_module, ".connection_screen", __package__)
            if self._shutting_down:
                return
            from .connection_screen import ConnectScreen

            startup = self.screen
            if isinstance(startup, _StartupScreen):
                self._startup_draft = (startup.composer.text, startup.composer.selection)
                await self.switch_screen(ConnectScreen(self.config, status_message=error))
        self._warm_local_catalog()

    @work(thread=True, group="local-catalog", exclusive=True)
    def _warm_local_catalog(self) -> None:
        """Load the Pydantic AI provider/model catalog before the dialog opens."""
        agent_model_catalog()

    def _mcp_provider(self) -> ClientMcpProvider:
        from ..mcp_provider import ClientMcpProvider

        return ClientMcpProvider(
            self.config.mcp,
            consent=self.ask_mcp_consent,
            persist=self.config.save,
        )

    def make_client(self) -> HrnsAcpClient:
        from ..client import HrnsAcpClient

        if self.target is None:
            raise HrnsClientError("No connection target is selected.")
        target = self.target
        return HrnsAcpClient(
            lambda: transport_for(target),
            policy=UiInteractionPolicy(self),
            auth_method=target.profile.auth_method,
            client_io=self.client_io_mode(target),
            mcp=None if target.local_setup else self._mcp_provider(),
        )

    def client_io_mode(self, target: ConnectionTarget) -> ClientIoMode:
        """What to offer this target: the flag, then its profile, then the setting.

        The advertisement is decided once, at ``initialize``, so a change here lands on the
        next connection rather than the live one.
        """
        if self.client_io is not None:
            return self.client_io
        if target.profile.client_io is not None:
            return target.profile.client_io
        return self.config.tui.client_io

    async def connect_target(self, target: ConnectionTarget) -> ChatScreen:
        """Connect, open the initial session, and show the chat workspace."""
        await asyncio.to_thread(import_module, ".chat.screen", __package__)
        from ..session import SessionProxy
        from .chat.screen import ChatScreen

        if self._shutting_down:
            raise asyncio.CancelledError
        self.target = target
        client = self.make_client()
        try:
            await client.connect()
            if self._shutting_down:
                raise asyncio.CancelledError
            self.client = client
            initial_load = False
            if self.initial_session and not target.local_setup:
                if target.kind == "remote":
                    cwd = self.initial_session_cwd
                    if cwd is None:
                        cwd = await self._remote_session_cwd(client, self.initial_session)
                    target = replace(target, cwd=cwd)
                    self.target = target
                session = SessionProxy(client, self.initial_session, target.cwd)
                initial_load = True
            else:
                try:
                    # The user answers consent prompts, so keep their thinking time
                    # out of the agent's session/new timeout.
                    await client.prepare_mcp()
                    session = await asyncio.wait_for(
                        client.new_session(target.cwd), timeout=client.connect_timeout
                    )
                except TimeoutError as exc:
                    raise TransportError(
                        f"{target.location_text()} did not answer session/new within "
                        f"{client.connect_timeout:g}s."
                    ) from exc
            if self._shutting_down:
                raise asyncio.CancelledError
            self.session = session
            if not target.local_setup:
                self.config.remember_connection(self.config.connection_identity(target.profile))
            startup = self.screen
            if isinstance(startup, _StartupScreen):
                self._startup_draft = (startup.composer.text, startup.composer.selection)
            screen = ChatScreen(
                client,
                session,
                target,
                self.config.tui,
                initial_session_id=self.initial_session if initial_load else None,
                initial_load=initial_load,
                initial_draft=self._startup_draft,
            )
            self.chat = screen
            if isinstance(startup, _StartupScreen):
                await self.switch_screen(screen)
            else:
                await self.push_screen(screen)
            self._startup_draft = None
        except BaseException:
            # Release the transport if startup fails or is cancelled.
            self.chat = None
            self.client = None
            self.session = None
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        return screen

    async def _remote_session_cwd(self, client: HrnsAcpClient, session_id: str) -> str:
        """Find the stored workspace without filtering by the connection's default cwd."""
        if not client.has_session_capability("list"):
            raise HrnsClientError(
                "This agent does not advertise session/list; supply --cwd to load the session."
            )
        cursor: str | None = None
        while True:
            listing = await client.list_sessions(cursor=cursor)
            for item in listing.sessions:
                if item.get("sessionId") == session_id:
                    cwd = item.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
                    raise HrnsClientError(
                        f"Session {session_id} has no listed working directory; supply --cwd."
                    )
            cursor = listing.next_cursor
            if not cursor:
                raise HrnsClientError(
                    f"Session {session_id} was not found in session/list; supply --cwd to load it."
                )

    @work(group="turn")
    async def start_turn(
        self,
        screen: ChatScreen,
        text: str,
        blocks: list[Any] | None = None,
        skills: Sequence[SkillMention] = (),
    ) -> None:
        """Keep each prompt alive while later prompts steer the same session."""
        try:
            if blocks is not None:
                await screen.session.prompt_blocks(blocks, display_text=text, skills=skills)
            else:
                await screen.session.prompt(text, skills=skills)
        except InteractionRequired as exc:
            screen._queue_paused = True
            if screen in self.screen_stack:
                await screen._notice(exc.message, "error")
        except asyncio.CancelledError:
            screen._queue_paused = True
            raise
        except HrnsClientError as exc:
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
                screen._set_busy(screen.client.turns.is_running(screen.session.session_id))
                screen.prompts.refresh_queue()
                self.call_later(screen.send_next_queued)

    async def choose_agent(self, screen: ChatScreen) -> None:
        if screen.busy:
            await screen._notice("Stop the running turn before switching agents.", "warning")
            return
        # Pop from an app-owned worker. Textual cancels a node's workers when it
        # unmounts, so popping from the chat screen's own command worker aborts
        # pop_screen halfway and leaves the app without a working focus chain.
        self.run_worker(self._leave_chat(), group="connect", exclusive=True)

    async def switch_agent(self, screen: ChatScreen, target: ConnectionTarget) -> None:
        """Leave the current conversation and connect to *target* directly."""
        if screen.busy:
            await screen._notice("Stop the running turn before switching agents.", "warning")
            return
        self.run_worker(self._switch_to(target), group="connect", exclusive=True)

    async def _switch_to(self, target: ConnectionTarget) -> None:
        await self.disconnect_current()
        with contextlib.suppress(Exception):
            await self._return_to_picker()
        try:
            await self.connect_target(target)
        except HrnsClientError as exc:
            self.notify(f"Connection failed: {exc.message}", severity="error")
        except Exception as exc:  # transport/protocol surprises must stay recoverable
            self.notify(f"Connection failed: {exc}", severity="error")

    async def _leave_chat(self) -> None:
        await self.disconnect_current()
        with contextlib.suppress(Exception):
            await self._return_to_picker()

    async def _return_to_picker(self) -> None:
        from .connection_screen import ConnectScreen

        if isinstance(self.screen_stack[-2], ConnectScreen):
            await self.pop_screen()
        else:
            await self.switch_screen(ConnectScreen(self.config))

    async def disconnect_current(self) -> None:
        self.chat = None
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
        decision = auto_permission(self.config.tui.permission_mode, request)
        if decision is not None:
            return decision

        async with self._approval_lock:
            # The mode may have changed while this request waited for the lock.
            decision = auto_permission(self.config.tui.permission_mode, request)
            if decision is not None:
                return decision
            if not self.screen_stack:
                return PermissionDecision(option_id=None)
            screen = self.chat
            if screen is None or screen is not self.screen:
                return await self._ask_permission_dialog(request)
            decision = await screen.request_approval(request)
            if decision.option_id is None:
                screen.expect_redirect()
            return decision

    async def _ask_permission_dialog(self, request: PermissionRequest) -> PermissionDecision:
        from .dialogs import PermissionDialog

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
        from .dialogs import ElicitationDialog

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

    async def ask_mcp_consent(self, name: str, server: McpServerSettings) -> ConsentDecision:
        """Ask before an exported MCP server is offered to the agent."""
        from ..mcp_provider import ConsentDecision
        from .dialogs import ChoiceDialog

        if not self.screen_stack:
            return ConsentDecision.DENY
        subtitle = f"{server_target(server)}\n\nThe agent will be able to call this server's tools."
        choices = [
            {
                "value": ConsentDecision.ALLOW_ONCE.value,
                "name": "Allow once",
                "description": "Offer this server for this session only.",
            },
            {
                "value": ConsentDecision.ALLOW_SESSION.value,
                "name": "Allow for this connection",
                "description": "Offer it to every session until you disconnect.",
            },
            {
                "value": ConsentDecision.ALWAYS_ALLOW.value,
                "name": "Always allow",
                "description": "Offer it in future sessions too and save the setting.",
            },
            {
                "value": ConsentDecision.DENY.value,
                "name": "Deny",
                "description": "Do not offer this server to the agent.",
            },
        ]
        future: asyncio.Future[ConsentDecision] = asyncio.get_running_loop().create_future()

        def done(result: str | None) -> None:
            if future.done():
                return
            try:
                future.set_result(ConsentDecision(result) if result else ConsentDecision.DENY)
            except ValueError:
                future.set_result(ConsentDecision.DENY)

        try:
            self.push_screen(ChoiceDialog(f"Allow MCP server '{name}'?", subtitle, choices), done)
        except Exception:
            return ConsentDecision.DENY
        try:
            return await future
        except asyncio.CancelledError:
            return ConsentDecision.DENY

    def apply_settings(self, settings: TuiSettings) -> None:
        """Adopt terminal preferences and persist them in the document."""
        self.config.tui = settings
        if settings.theme:
            with contextlib.suppress(Exception):
                self.theme = settings.theme
        with contextlib.suppress(Exception):
            self.config.save()

    # ----------------------------------------------------------------- quitting

    async def action_quit_app(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        screen = self.chat
        if screen is not None and screen is self.screen and screen.busy:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(screen.session.cancel(), timeout=3)
        await self.disconnect_current()
        self.exit(0)

    async def shutdown_client(self) -> None:
        await self.disconnect_current()


def run_terminal(
    config: HrnsConfig,
    *,
    resolve_initial_target: InitialTarget,
    initial_session: str | None = None,
    initial_session_cwd: str | None = None,
    client_io: ClientIoMode | None = None,
) -> int:
    """Run the terminal app to completion and always close the agent connection."""
    app = HrnsApp(
        config,
        resolve_initial_target=resolve_initial_target,
        initial_session=initial_session,
        initial_session_cwd=initial_session_cwd,
        client_io=client_io,
    )

    async def _main() -> int:
        try:
            result = await app.run_async()
            if app.startup_error is not None:
                raise app.startup_error
            return result or 0
        finally:
            await app.shutdown_client()

    return asyncio.run(_main())
