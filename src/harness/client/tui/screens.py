"""Full-screen screens: connection picker and the conversation workspace."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Static

from ..client import HarnessAcpClient, SessionProxy
from ..constants import CLIENT_VERSION
from ..errors import HarnessClientError
from ..events import (
    AgentTextChunk,
    CommandsUpdated,
    ConfigOptionsUpdated,
    ConfigurationChanged,
    ConnectionEvent,
    ModesUpdated,
    PlanChanged,
    PluginCommandsListed,
    SessionInfoChanged,
    ToolEvent,
    ToolPresentationUpdated,
    TurnCompleted,
    TurnStarted,
    UsageUpdated,
    UserMessageReplayed,
)
from ..interactions import (
    PERMISSION_MODES,
    AskUserRequest,
    AskUserResponse,
    PermissionDecision,
    PermissionRequest,
)
from ..launch import ConnectionTarget
from .choice_keys import handle_choice_arrow
from .commands import LOCAL_COMMANDS, resolve_local_command
from .completion import FileIndex
from .connection_screen import _short_path
from .dialogs import (
    ChoiceDialog,
    CommandPaletteDialog,
    HelpDialog,
    SettingsDialog,
    TranscriptSearchDialog,
)

if TYPE_CHECKING:
    pass

from .approval import ApprovalBar, AskUserBar
from .chat.capabilities import CapabilityWorkflow
from .chat.commands import CommandWorkflow
from .chat.model import ModelWorkflow
from .chat.prompts import PromptWorkflow
from .chat.session import SessionWorkflow
from .composer import (
    AttachmentBar,
    CommandSuggestions,
    Composer,
    PromptQueue,
)
from .messages import (
    ApprovalDecided,
    AskUserAnswered,
    AttachmentRemoved,
    CommandSuggestionKey,
    OutputPending,
    QueueAction,
    Submitted,
)
from .panels import SidePanel, SidePanelCloseRequested
from .panels.capability import (
    BindingEditRequested,
    BindingToggleRequested,
    CapabilityAddRequested,
    CapabilityRefreshRequested,
    ConfigOptionRequested,
    ConfigOptionStepRequested,
)
from .panels.mcp import McpRefreshRequested
from .panels.session import (
    SessionCloseRequested,
    SessionDeleteRequested,
    SessionLoadRequested,
    SessionMoreRequested,
    SessionNewRequested,
    SessionRefreshRequested,
    SessionResumeRequested,
)
from .screen_base import _HarnessScreen
from .state import (
    Settings,
    activity_label,
    config_option_value,
    effort_option,
    format_context_detail,
    format_context_usage,
    format_cost,
    model_option,
    permission_mode_label,
)
from .status import StatusBar
from .transcript import TranscriptPane

__all__ = ["ChatScreen"]


class ChatScreen(_HarnessScreen):
    """The conversation workspace: transcript, composer, status and side panels."""

    BINDINGS = [
        Binding("f1", "help", "Help", show=False),
        Binding("f2", "show_capabilities", "Plugins", show=False),
        Binding("f3", "show_sessions", "Sessions", show=False),
        Binding("f4", "show_activity", "Activity", show=False),
        Binding("ctrl+t", "focus_transcript", "Transcript", show=False),
        Binding("ctrl+f", "find_transcript", "Find text", show=False, priority=True),
        Binding("alt+up", "previous_message", "Previous prompt", show=False),
        Binding("alt+down", "next_message", "Next prompt", show=False),
        Binding("ctrl+end", "latest_output", "Latest output", show=False, priority=True),
        Binding("ctrl+b", "toggle_panel", "Panel", show=False),
        Binding("ctrl+k", "command_palette", "Commands", show=False, priority=True),
        Binding("ctrl+n", "new_session", "New session", show=False),
        Binding("ctrl+r", "reconnect", "Reconnect", show=False),
        Binding("ctrl+enter", "submit_composer", "Send", show=False, priority=True),
        Binding("ctrl+s", "submit_question", "Submit answer", show=False, priority=True),
        Binding("escape", "interrupt_or_focus", "Stop", show=False),
        Binding("ctrl+q", "quit_app", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        client: HarnessAcpClient,
        session: SessionProxy,
        target: ConnectionTarget,
        settings: Settings,
        *,
        initial_session_id: str | None = None,
        initial_load: bool = False,
    ) -> None:
        super().__init__()
        self.client = client
        self.session = session
        self.target = target
        self.settings = settings
        self.initial_session_id = initial_session_id
        self.initial_load = initial_load
        self.connected = client.connected
        self.busy = False
        self._activity = ""
        self._hint_note = ""
        self._usage: dict[str, Any] | None = None
        self._context_usage: dict[str, Any] | None = None
        self._session_title: str | None = None
        self._todo_auto_opened_for: str | None = None
        self._commands: tuple[dict[str, Any], ...] = ()
        self._plugin_commands: tuple[dict[str, Any], ...] = ()
        self._plugin_revision = 0
        self._consumed_command_actions: set[tuple[str, str]] = set()
        self._bindings_raw: list[dict[str, Any]] = []
        self._loading_session = False
        self._replay_target: str | None = None
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._consumer: asyncio.Task[None] | None = None
        self._unsubscribe = None
        self._panel_open = False
        self._turns_started = 0
        self._attachments: list[str] = []
        self._drafts: dict[str, tuple[str, list[str], list[tuple[str, list[str]]]]] = {}
        self._prompt_history: dict[str, list[str]] = {}
        self._queued_prompts: list[tuple[str, list[str]]] = []
        self._queue_paused = False
        self._session_rows: list[dict[str, Any]] = []
        self._session_cursor: str | None = None
        self._approval_future: asyncio.Future[PermissionDecision] | None = None
        self._answers_future: asyncio.Future[AskUserResponse] | None = None
        self._redirect_after_turn = False
        self._suggestion_items: list[dict[str, Any]] = []
        self._suggestion_index = 0
        self._suggestion_signature: tuple[str, ...] = ()
        self._workspace_files: list[str] = []
        self._file_index = FileIndex(())
        self._workspace_root = Path.cwd()
        self._skills: list[dict[str, Any]] = []
        self._skills_loading = False
        if target.location().get("kind") == "local" and Path(target.cwd).is_dir():
            self._workspace_root = Path(target.cwd).resolve()
        self.commands = CommandWorkflow(self)
        self.prompts = PromptWorkflow(self)
        self.sessions = SessionWorkflow(self)
        self.models = ModelWorkflow(self)
        self.capabilities = CapabilityWorkflow(self)

    # --------------------------------------------------------------- structure

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield TranscriptPane(id="transcript")
            yield SidePanel()
        yield Button("New output ↓ · Ctrl+End", id="new-output")
        yield PromptQueue(id="prompt-queue")
        with Horizontal(id="ask-user-dock"):
            yield AskUserBar(id="ask-user")
        with Horizontal(id="approval-dock"):
            yield ApprovalBar(id="approval")
        with Horizontal(id="suggestions-dock"):
            yield CommandSuggestions(id="command-suggestions")
        yield AttachmentBar(id="attachments", markup=False)
        with Horizontal(id="composer-dock"):
            with Horizontal(id="composer-row"):
                yield Static("›", id="composer-prompt", markup=False)
                yield Composer(placeholder="Send a message…", id="composer")
        yield StatusBar(id="statusbar")

    def on_mount(self) -> None:
        self.query_one("#new-output").display = False
        self.prompts.restore_draft()
        self._index_workspace()
        self._apply_settings()
        self._apply_layout()
        self._subscribe()
        self._bind_view_context()
        self._update_chrome()
        self.query_one("#composer", Composer).focus()
        self._run_bootstrap()

    def on_unmount(self) -> None:
        self._resolve_approval(None, focus=False)
        self._resolve_questions(AskUserResponse(cancelled=True), focus=False)
        if self._consumer is not None:
            self._consumer.cancel()
            self._consumer = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _bind_view_context(self) -> None:
        self.query_one(
            "#transcript", TranscriptPane
        ).view_context = self.commands.make_view_context(self.session)

    def _subscribe(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = self.client.bus.subscribe(self._enqueue)
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume_events())

    def _enqueue(self, event: Any) -> None:
        self._events.put_nowait(event)

    async def _consume_events(self) -> None:
        while True:
            event = await self._events.get()
            try:
                await self._handle_event(event)
            except Exception as exc:  # a rendering failure must not kill the session
                with contextlib.suppress(Exception):
                    await self._notice(f"interface error: {exc}", "error")
            finally:
                self._events.task_done()

    def _apply_settings(self) -> None:
        pane = self.query_one("#transcript", TranscriptPane)
        pane.auto_expand_tools = self.settings.auto_expand_tools
        pane.show_thoughts = self.settings.show_thoughts
        if self.settings.theme in {
            "harness",
            "textual-light",
            "textual-dark",
            "ansi-light",
            "ansi-dark",
        }:
            with contextlib.suppress(Exception):
                self.app.theme = self.settings.theme
        self._apply_layout()
        self._update_chrome()

    def _apply_layout(self) -> None:
        width = self.app.size.width
        panel = self.query_one("#side-panel", SidePanel)
        panel.display = self._panel_open
        screen_class = "narrow" if width < 96 else "wide"
        if screen_class not in self.classes:
            self.add_class(screen_class)
        if width >= 96:
            self.remove_class("narrow")
        else:
            self.remove_class("wide")

    def on_resize(self, event: Any) -> None:
        self._apply_layout()

    # ------------------------------------------------------------------ chrome

    def _update_chrome(self) -> None:
        session_id = self.session.session_id if self.session else None
        self.query_one("#side-panel", SidePanel).sessions.mark_current(session_id)
        self._refresh_status()
        activity = self.query_one("#side-panel", SidePanel).activity
        location = self.target.location()
        lines = [
            f"state: {'connected' if self.connected else 'disconnected'}",
            f"location: {location.get('kind', 'unknown')}",
        ]
        if location.get("kind") == "local":
            lines.append(
                f"command: {self.target.profile.command} {' '.join(self.target.profile.args)}".rstrip()
            )
            lines.append(f"workspace: {self.target.cwd}")
        else:
            lines.append(f"server: {self.target.profile.url}")
            lines.append(f"agent: {self.target.agent_id}")
            lines.append(f"remote workspace: {self.target.cwd}")
        lines.append(f"client: harness-client {CLIENT_VERSION}")
        if self.client.extension.enabled:
            lines.append(f"extension: {self.client.extension.describe()}")
            if self.client.extension.local_binding_creation:
                lines.append("plugin creation: allowed by host")
        else:
            lines.append("extension: not advertised (standard ACP only)")
        activity.set_connection(lines)
        attached = self.session is not None and self.session.client is self.client
        activity.set_session(
            [
                f"session: {session_id or '(none)'}",
                "attached: yes" if attached else "attached: no (reconnected without replay)",
                "selected/active revisions: available from the plugins panel when negotiated",
                f"user messages sent this run: {self._turns_started}",
            ]
        )

    def _refresh_status(self) -> None:
        """Push the current session state into the one-line status band."""
        session = self.session
        option = model_option(session.config_options) if session else None
        reasoning = effort_option(session.config_options) if session else None
        self.query_one("#statusbar", StatusBar).show(
            connected=self.connected,
            model=config_option_value(option) if option else "",
            effort=config_option_value(reasoning) if reasoning else "",
            mode=(session.current_mode_id or "") if session else "",
            usage=format_context_usage(self._context_usage),
            cost=format_cost(self._usage),
            usage_detail=format_context_detail(self._context_usage, self._usage),
            busy=self.busy,
            activity=self._activity,
            permission=permission_mode_label(self.settings.permission_mode),
            hint=self._hint_text(),
            context=self._status_context(),
        )

    def _status_context(self) -> str:
        agent = f"{self.client.agent_title} {self.client.agent_version}".strip()
        location = self.target.location()
        where = location.get("cwd") or location.get("server") or ""
        where = _short_path(where, 48) if where else ""
        session = self._session_title or (self.session.session_id if self.session else "")
        return " · ".join(part for part in (agent, where, session) if part)

    def _hint_text(self) -> str:
        if self._hint_note:
            return self._hint_note
        if self.busy:
            return "esc to stop"
        return ""

    async def _notice(self, text: str, severity: str = "info") -> None:
        await self.query_one("#transcript", TranscriptPane).add_notice(text, severity=severity)

    def _reset_activity(self) -> None:
        """Drop activity that belongs to the previous session or connection."""
        self._usage = None
        self._context_usage = None
        self._activity = ""
        self._hint_note = ""
        self._session_title = None
        self._commands = ()
        self._plugin_commands = ()
        self._plugin_revision = 0
        self._bindings_raw = []
        self._turns_started = 0
        panel = self.query_one("#side-panel", SidePanel)
        panel.todos.set_plan((), cleared=True)
        panel.activity.set_commands(())
        panel.activity.set_usage(None)
        self._todo_auto_opened_for = None
        self._refresh_status()

    # ---------------------------------------------------------------- bootstrap

    @work(group="bootstrap", exclusive=True)
    async def _run_bootstrap(self) -> None:
        if self.initial_load and self.initial_session_id:
            await self.sessions.load(self.initial_session_id, cwd=self.session.cwd, initial=True)
        self._refresh_panels()

    @work(group="panels", exclusive=True)
    async def _refresh_panels(self) -> None:
        await self.capabilities.refresh()
        await self.sessions.refresh()

    @work(group="panels", exclusive=True)
    async def refresh_capabilities(self) -> None:
        await self.capabilities.refresh()

    @work(group="panels", exclusive=True)
    async def refresh_sessions(self) -> None:
        await self.sessions.refresh()

    # ------------------------------------------------------------------ events

    async def _handle_event(self, event: Any) -> None:
        pane = self.query_one("#transcript", TranscriptPane)
        session_id = getattr(event, "session_id", None)
        expected = self._replay_target or (self.session.session_id if self.session else None)
        if (
            session_id is not None
            and expected is not None
            and session_id != expected
            and not isinstance(event, ConnectionEvent)
        ):
            return
        if isinstance(event, UserMessageReplayed):
            await pane.finish_message()
            await pane.add_user(event.text)
            return
        if isinstance(event, AgentTextChunk):
            # The Thinking row is a status indicator, not reasoning content; it
            # appears even when the user keeps reasoning text collapsed.
            await pane.append_agent_text(event.text, thought=event.thought)
            return
        if isinstance(event, ToolEvent):
            if event.is_new:
                await pane.start_tool(event)
            else:
                await pane.update_tool(event)
            if event.status in {"pending", "in_progress"}:
                self._activity = activity_label(event.kind, event.title)
            elif self.busy:
                self._activity = "working"
            self._refresh_status()
            return
        if isinstance(event, ToolPresentationUpdated):
            for snapshot in event.snapshots:
                await pane.apply_presentation(snapshot)
            return
        if isinstance(event, TurnStarted):
            self._turns_started += 1
            self._set_busy(True)
            return
        if isinstance(event, TurnCompleted):
            if event.stop_reason not in {"end_turn", ""}:
                self._queue_paused = True
            self.prompts.refresh_queue()
            await pane.finish_message()
            if event.usage and (not self._usage or self._usage.get("scope") != "session"):
                self._usage = event.usage
                self.query_one("#side-panel", SidePanel).activity.set_usage(event.usage)
            if event.stop_reason not in {"end_turn", ""}:
                severity = (
                    "error"
                    if event.stop_reason in {"error", "connection_lost", "interaction_required"}
                    else "warning"
                )
                message = {
                    "cancelled": "Turn cancelled.",
                    "refusal": "The agent refused the request.",
                    "max_tokens": "The turn hit the model token limit.",
                    "max_turn_requests": "The turn hit the agent's request limit.",
                    "interaction_required": "An interaction was required and no handler was available.",
                    "connection_lost": "The connection was lost during the turn.",
                    "error": "The turn failed.",
                }.get(event.stop_reason, f"Turn ended: {event.stop_reason}")
                if event.detail:
                    message += f" {event.detail}"
                await self._notice(message, severity)
            self._update_chrome()
            return
        if isinstance(event, CommandsUpdated):
            self._commands = event.commands
            self.query_one("#side-panel", SidePanel).activity.set_commands(event.commands)
            self.prompts.sync_command_suggestions()
            return
        if isinstance(event, PluginCommandsListed):
            self.capabilities.set_plugin_commands(event.commands, revision=event.selected_revision)
            return
        if isinstance(event, PlanChanged):
            panel = self.query_one("#side-panel", SidePanel)
            panel.todos.set_plan(event.entries, cleared=event.cleared)
            session_id = self._replay_target or (self.session.session_id if self.session else None)
            if (
                not event.cleared
                and event.entries
                and session_id is not None
                and self._todo_auto_opened_for != session_id
            ):
                self._todo_auto_opened_for = session_id
                self._show_panel("todos", focus=False)
            return
        if isinstance(event, ConfigOptionsUpdated):
            self.session.remember(event.options)
            self._update_chrome()
            panel = self.query_one("#side-panel", SidePanel).capabilities
            if self.client.extension.enabled:
                if self._replay_target is None:
                    panel.show_message("Agent published updated standard options.")
            elif not panel.extension_mode:
                panel.set_standard_options(list(event.options))
            else:
                # The negotiated extension owns this panel; a standard option
                # update must not silently replace its richer state.
                panel.show_message("Agent published updated standard options.")
            return
        if isinstance(event, ModesUpdated):
            self.session.current_mode_id = event.current_mode_id
            self._update_chrome()
            return
        if isinstance(event, SessionInfoChanged):
            if event.title:
                self._session_title = event.title
                self._update_chrome()
                if not self._loading_session:
                    self.refresh_sessions()
            return
        if isinstance(event, UsageUpdated):
            if event.summary.get("scope") == "context":
                self._context_usage = event.summary
                self._refresh_status()
                return
            if (
                self._usage
                and self._usage.get("scope") == "session"
                and event.summary.get("scope") != "session"
            ):
                return
            self._usage = event.summary
            self.query_one("#side-panel", SidePanel).activity.set_usage(event.summary)
            self._update_chrome()
            return
        if isinstance(event, ConfigurationChanged):
            try:
                self.capabilities.apply_snapshot(event.snapshot)
            except Exception as exc:
                await self._notice(f"configuration update was malformed: {exc}", "error")
            return
        if isinstance(event, ConnectionEvent) and event.state == "lost":
            self._queue_paused = True
            self.prompts.refresh_queue()
            self.connected = False
            self._set_busy(False)
            await pane.finish_message()
            await self._notice(
                f"Connection lost: {event.detail or 'the agent stopped responding'}. "
                "The session is retained; press Ctrl+R to reconnect and reload it.",
                "error",
            )
            self._update_chrome()
            return
        if isinstance(event, ConnectionEvent) and event.state == "ready":
            self.connected = True
            self._update_chrome()
            return

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if not busy:
            self._activity = ""
        else:
            self._hint_note = ""
            if not self._activity:
                self._activity = "working"
        self._refresh_status()
        if not busy:
            if self._approval_future is not None:
                self._resolve_approval(None)
            if self._redirect_after_turn:
                self._redirect_after_turn = False
                self._hint_note = "Turn stopped · type a new instruction for the agent"
                self._refresh_status()
                self.query_one("#composer", Composer).focus()

    # --------------------------------------------------------------- approvals

    async def request_approval(self, request: PermissionRequest) -> PermissionDecision:
        if self._approval_future is not None:
            self._resolve_approval(None)
        future: asyncio.Future[PermissionDecision] = asyncio.get_running_loop().create_future()
        self._approval_future = future
        transcript = self.query_one("#transcript", TranscriptPane)
        self.query_one("#approval", ApprovalBar).show(
            request,
            data=transcript.tool_data(request.tool_call_id),
            renderer=transcript.renderers,
            view_context=transcript.view_context,
        )
        return await future

    def expect_redirect(self) -> None:
        """Focus the composer when the current turn stops, so the user can redirect."""
        self._redirect_after_turn = True

    def _resolve_approval(self, option_id: str | None, *, focus: bool = True) -> None:
        future, self._approval_future = self._approval_future, None
        if future is not None and not future.done():
            future.set_result(PermissionDecision(option_id=option_id))
        with contextlib.suppress(Exception):
            self.query_one("#approval", ApprovalBar).hide()
        if focus and self.is_mounted:
            with contextlib.suppress(Exception):
                self.query_one("#composer", Composer).focus()

    def on_approval_decided(self, event: ApprovalDecided) -> None:
        self._resolve_approval(event.option_id)
        if event.option_id is None:
            self._redirect_after_turn = True
            self._cancel_turn()

    # --------------------------------------------------------------- questions

    async def request_questions(self, request: AskUserRequest) -> AskUserResponse:
        if self._answers_future is not None:
            self._resolve_questions(AskUserResponse(cancelled=True))
        future: asyncio.Future[AskUserResponse] = asyncio.get_running_loop().create_future()
        self._answers_future = future
        self.query_one("#ask-user", AskUserBar).show(request)
        return await future

    def _resolve_questions(self, response: AskUserResponse, *, focus: bool = True) -> None:
        future, self._answers_future = self._answers_future, None
        if future is not None and not future.done():
            future.set_result(response)
        with contextlib.suppress(Exception):
            self.query_one("#ask-user", AskUserBar).hide()
        if focus and self.is_mounted:
            with contextlib.suppress(Exception):
                self.query_one("#composer", Composer).focus()

    def on_ask_user_answered(self, event: AskUserAnswered) -> None:
        self._resolve_questions(event.response)

    def on_key(self, event: Any) -> None:
        """Left/Right change a focused choice control; approval shortcuts need focus."""
        if handle_choice_arrow(self.focused, str(event.key)):
            event.stop()
            return
        if self._approval_future is None:
            return
        bar = self.query_one("#approval", ApprovalBar)
        if not bar.pending or not bar.has_focus_within:
            return
        key = str(event.key)
        if key.isdigit() and key != "0":
            option_id = bar.option_id_at(int(key) - 1)
            if option_id:
                event.stop()
                self._resolve_approval(option_id)
            return
        kinds = {
            "y": "allow_once",
            "a": "allow_always",
            "n": "reject_once",
            "N": "reject_always",
            "shift+n": "reject_always",
        }
        kind = kinds.get(key)
        if kind is None:
            return
        option_id = bar.option_id_for_kind(kind)
        if option_id:
            event.stop()
            self._resolve_approval(option_id)

    # --------------------------------------------------------------- submitting

    def on_text_area_changed(self, event: Any) -> None:
        self.prompts.text_changed(event)

    def on_text_area_selection_changed(self, event: Any) -> None:
        self.prompts.selection_changed(event)

    @work(group="file-index", exclusive=True)
    async def _index_workspace(self) -> None:
        await self.prompts.index_workspace()

    def on_command_suggestion_key(self, event: CommandSuggestionKey) -> None:
        self.prompts.handle_suggestion_key(event.key)

    @work(group="attach-file", exclusive=False)
    async def _complete_file(self, item: dict[str, Any]) -> None:
        await self.prompts.complete_file(item)

    async def on_submitted(self, event: Submitted) -> None:
        await self.prompts.submitted(event.text)

    def on_attachment_removed(self, event: AttachmentRemoved) -> None:
        self.prompts.attachment_removed(event.index)

    async def on_queue_action(self, event: QueueAction) -> None:
        await self.prompts.queue_action(event.action, event.index)

    async def send_next_queued(self) -> None:
        await self.prompts.send_next_queued()

    async def _handle_command(self, text: str) -> None:
        await self.commands.handle_command(text)

    async def _run_local_command(self, name: str, rest: str = "") -> None:
        await self.commands.run_local_command(name, rest)

    async def _run_plugin_command(self, binding_id: str, command_name: str, arguments: str) -> None:
        await self.commands.run_plugin_command(binding_id, command_name, arguments)

    async def _invoke_plugin_command(
        self,
        binding_id: str,
        command_name: str,
        *,
        text: str | None = None,
        arguments: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        return await self.commands.invoke_plugin_command(
            binding_id,
            command_name,
            text=text,
            arguments=arguments,
            request_id=request_id,
        )

    async def _send_agent_command(self, name: str, arguments: str) -> None:
        await self.commands.send_agent_command(name, arguments)

    def _find_plugin_command(self, display: str) -> dict[str, Any] | None:
        return self.commands.find_plugin_command(display)

    def _make_view_context(self, session: SessionProxy) -> Any:
        return self.commands.make_view_context(session)

    # --------------------------------------------------------------- capability

    def on_capability_refresh_requested(self, event: CapabilityRefreshRequested) -> None:
        self.refresh_capabilities()

    def on_mcp_refresh_requested(self, event: McpRefreshRequested) -> None:
        event.stop()
        self._refresh_mcp()

    def on_capability_add_requested(self, event: CapabilityAddRequested) -> None:
        asyncio.create_task(self.capabilities.choose_plugin_to_add())

    def on_binding_toggle_requested(self, event: BindingToggleRequested) -> None:
        self._toggle_binding(event.binding_id, event.enabled)

    def on_binding_edit_requested(self, event: BindingEditRequested) -> None:
        self.capabilities.edit_binding(event.binding_id)

    def on_config_option_requested(self, event: ConfigOptionRequested) -> None:
        self._change_standard_option(event.option_id)

    def on_config_option_step_requested(self, event: ConfigOptionStepRequested) -> None:
        self.models.step_standard_option(event.option_id, event.direction)

    @work(group="capability", exclusive=True)
    async def _toggle_binding(self, binding_id: str, enabled: bool) -> None:
        await self.capabilities.toggle_binding(binding_id, enabled)

    @work(group="mcp", exclusive=True)
    async def _refresh_mcp(self) -> None:
        await self.capabilities.refresh_mcp()

    @work(group="capability", exclusive=True)
    async def _add_capability(
        self,
        plugin_id: str,
        binding_id: str,
        enabled: bool,
        config: dict[str, Any],
        change_mode: str = "next_turn",
    ) -> None:
        await self.capabilities.add_capability(plugin_id, binding_id, enabled, config, change_mode)

    @work(group="capability", exclusive=True)
    async def _apply_binding_config(self, binding_id: str, values: dict[str, Any]) -> None:
        await self.capabilities.apply_binding_config(binding_id, values)

    @work(group="capability", exclusive=True)
    async def _change_standard_option(self, option_id: str) -> None:
        await self.models.change_standard_option(option_id)

    @work(group="capability", exclusive=True)
    async def _set_standard_value(self, option_id: str, value: str) -> None:
        await self.models.set_standard_value(option_id, value)

    # ----------------------------------------------------------------- sessions

    def on_session_load_requested(self, event: SessionLoadRequested) -> None:
        self._load_session_worker(event.session_id)

    @work(group="session", exclusive=True)
    async def _load_session_worker(self, session_id: str) -> None:
        await self.sessions.load(session_id, cwd=self.sessions.session_cwd(session_id))

    def on_session_resume_requested(self, event: SessionResumeRequested) -> None:
        self._resume_session_worker(event.session_id)

    @work(group="session", exclusive=True)
    async def _resume_session_worker(self, session_id: str) -> None:
        await self.sessions.resume(session_id)

    def on_session_new_requested(self, event: SessionNewRequested) -> None:
        self._new_session_worker()

    @work(group="session", exclusive=True)
    async def _new_session_worker(self) -> None:
        await self.sessions.new()

    def on_session_delete_requested(self, event: SessionDeleteRequested) -> None:
        self.sessions.confirm_delete(event.session_id)

    @work(group="session", exclusive=True)
    async def _delete_session(self, session_id: str) -> None:
        await self.sessions.delete(session_id)

    def on_session_close_requested(self, event: SessionCloseRequested) -> None:
        self._close_session(event.session_id)

    @work(group="session", exclusive=True)
    async def _close_session(self, session_id: str) -> None:
        await self.sessions.close(session_id)

    def on_session_refresh_requested(self, event: SessionRefreshRequested) -> None:
        self.refresh_sessions()

    def on_session_more_requested(self, event: SessionMoreRequested) -> None:
        self._load_more_sessions()

    @work(group="panels", exclusive=True)
    async def _load_more_sessions(self) -> None:
        await self.sessions.load_more()

    # ------------------------------------------------------------------ models

    @work(group="model", exclusive=True)
    async def _set_mode(self, mode_id: str) -> None:
        await self.models.set_mode(mode_id)

    # ----------------------------------------------------------------- actions

    def action_show_capabilities(self) -> None:
        self._show_panel("capabilities")

    def action_show_todos(self) -> None:
        self._show_panel("todos")

    def action_show_mcp(self) -> None:
        self._show_panel("mcp")
        self._refresh_mcp()

    def action_show_sessions(self) -> None:
        self._show_panel("sessions")
        self.refresh_sessions()

    def action_show_activity(self) -> None:
        self._show_panel("activity")

    def _show_panel(self, tab: str, *, focus: bool = True) -> None:
        panel = self.query_one("#side-panel", SidePanel)
        self._panel_open = True
        panel.display = True
        # TabbedContent resolves its private ContentTabs by immediate-child
        # lookup. Assigning ``active`` before compose has mounted it raises
        # NoMatches, so wait until the show/refresh cycle has run.
        if focus:
            self.call_after_refresh(panel.activate, tab)
        else:
            self.call_after_refresh(panel.select, tab)

    def _close_panel(self) -> None:
        panel = self.query_one("#side-panel", SidePanel)
        self._panel_open = False
        panel.display = False
        self.query_one("#composer", Composer).focus()

    def on_side_panel_close_requested(self, event: SidePanelCloseRequested) -> None:
        event.stop()
        self._close_panel()

    def action_toggle_panel(self) -> None:
        if self._panel_open:
            self._close_panel()
            return
        self._panel_open = True
        self.query_one("#side-panel", SidePanel).display = True

    def action_command_palette(self) -> None:
        agent_commands = list(self._commands)
        local_names = {
            name for command in LOCAL_COMMANDS for name in (command.name, *command.aliases)
        }
        agent_names = {str(command.get("name", "")) for command in agent_commands}
        collisions = local_names & agent_names

        def apply(result: str | None) -> None:
            if not result:
                return
            kind, _, name = result.partition(":")
            if kind == "local":
                command = resolve_local_command(name)
                composer = self.query_one("#composer", Composer)
                if command is not None and command.takes_argument:
                    composer.load_text(f"/{command.name} ")
                    composer.focus()
                elif command is not None:
                    asyncio.create_task(self.commands.run_local_command(command.name))
            elif kind == "plugin":
                descriptor = self.commands.find_plugin_command(name)
                if descriptor is not None:
                    composer = self.query_one("#composer", Composer)
                    takes_arguments = bool(
                        descriptor.get("takesArguments") or descriptor.get("takes_arguments")
                    )
                    if takes_arguments:
                        composer.load_text(f"/{name} ")
                        composer.focus()
                    else:
                        asyncio.create_task(
                            self.commands.run_plugin_command(
                                str(descriptor.get("bindingId") or descriptor.get("binding_id")),
                                str(
                                    descriptor.get("commandName") or descriptor.get("command_name")
                                ),
                                "",
                            )
                        )
            elif kind == "agent":
                composer = self.query_one("#composer", Composer)
                # Always route through /agent so collisions cannot hijack the
                # pick into a local command with the same name.
                composer.load_text(f"/agent {name} ")
                composer.focus()

        plugin_entries = [
            {
                "display": self.commands.plugin_command_display(command),
                "description": str(command.get("description", "")),
            }
            for command in self._plugin_commands
        ]
        self.app.push_screen(
            CommandPaletteDialog(
                LOCAL_COMMANDS,
                agent_commands,
                collision_names=collisions,
                plugin_commands=plugin_entries,
            ),
            apply,
        )

    def action_help(self) -> None:
        agent_lines = (
            "\n".join(
                f"- `/{command.get('name')}` — {command.get('description', '')}"
                for command in self._commands
            )
            or "No agent commands advertised."
        )
        local_lines = []
        for command in LOCAL_COMMANDS:
            if command.hidden:
                continue
            aliases = (
                " (aliases: " + ", ".join(f"/{alias}" for alias in command.aliases) + ")"
                if command.aliases
                else ""
            )
            local_lines.append(f"- `/{command.name}` — {command.description}{aliases}")
        plugin_lines = [
            f"- `/{self.commands.plugin_command_display(command)}` — {command.get('description', '')}"
            for command in self._plugin_commands
        ]
        plugin_section = (
            "\n## Plugin commands\n\n" + "\n".join(plugin_lines) if plugin_lines else ""
        )
        body = f"""# Harness terminal

## Keys

| key | action |
| --- | --- |
| `Ctrl+Enter` | send the prompt (works from any panel or the transcript) |
| `Ctrl+K` | command palette (local and agent sections) |
| `F1` | this help |
| `F2` | plugins panel |
| `F3` | sessions panel |
| `F4` | activity panel |
| `Tab` / `Shift+Tab` inside the side panel | switch between plugins, sessions and activity |
| `Esc` inside the side panel | close it and return to the composer |
| `Up` / `Down` or `Tab` in a form | move between fields |
| `Left` / `Right` in a form | change a choice or toggle a switch |
| `Enter` in a form | save and close |
| `Esc` in a form | cancel without saving |
| `Ctrl+T` | focus the transcript for scrolling; `Esc` returns to the composer |
| `Ctrl+F` | find text in retained messages, tools, and reasoning |
| `Alt+Up` / `Alt+Down` | jump to the previous / next user message |
| `Ctrl+End` | jump to latest output and resume following |
| `Up` / `Down` at editor boundaries | recall prompts and restore the unfinished draft |
| `Esc` (idle) | focus the composer |
| `Ctrl+B` | show or hide the side panel |
| `Ctrl+N` | new session |
| `Ctrl+R` | reconnect and reload the session |
| `Esc` | stop the running turn |
| `Ctrl+Q` | quit |

## Local commands

{chr(10).join(local_lines)}
{plugin_section}

Type `@` to find and attach a local workspace file; select a staged file to remove it.
Submitting during a running turn steers the agent: the text reaches it at its next model
request instead of waiting for the turn to end. Prompts with attachments, and peers without
the steering extension, queue locally instead; use the queue's Edit and Remove controls to
revise those. Cancellation or errors pause the queue.
Drafts, prompt history, attachments, and queues stay with their session while this app is open.
Use `/` in the sessions panel to filter titles and workspaces. `Esc` closes the panel.

## Agent commands

{agent_lines}

Agent commands are invoked as `/<name>` or `/agent <name>` on collisions. The client never
invents commands the agent did not advertise.
"""
        self.app.push_screen(HelpDialog(body))

    def _show_permissions(self) -> None:
        labels = {
            "ask": "Ask every time",
            "reads": "Allow reads, ask for the rest",
            "all": "Allow all tools",
        }
        descriptions = {
            "ask": "Show the approval bar for every tool request.",
            "reads": "Approve read, search and think calls automatically; ask before writes, commands and network access.",
            "all": "Approve every call whose agent offered an allow option. This is not a sandbox.",
        }
        choices = [
            {"value": mode, "name": labels[mode], "description": descriptions[mode]}
            for mode in PERMISSION_MODES
        ]

        def apply(mode: str | None) -> None:
            if mode not in PERMISSION_MODES:
                return
            self.settings = replace(self.settings, permission_mode=mode)
            self.app.apply_settings(self.settings)
            self._apply_settings()
            asyncio.create_task(self._notice(f"Approvals: {labels[mode]}.", "success"))

        self.app.push_screen(
            ChoiceDialog(
                "Tool approvals",
                "How should this client answer the agent's tool requests?",
                choices,
                current=self.settings.permission_mode,
            ),
            apply,
        )

    def _show_settings(self) -> None:
        def apply(settings: Settings | None) -> None:
            if settings is not None:
                self.settings = settings
                self.app.apply_settings(settings)
                self._apply_settings()

        self.app.push_screen(
            SettingsDialog(
                self.settings,
                can_persist=bool(self.app.profiles.sources),
            ),
            apply,
        )

    def action_interrupt_or_focus(self) -> None:
        if self._answers_future is not None:
            self.query_one("#ask-user", AskUserBar).action_decline()
            return
        if self._approval_future is not None:
            # Esc is the Codex-style "no, and tell it what to do differently":
            # stop the pending call, stop the turn, then hand back the composer.
            self._resolve_approval(None)
            self._redirect_after_turn = True
            self._cancel_turn()
            return
        if self.busy:
            self._cancel_turn()
        else:
            self.query_one("#composer", Composer).focus()

    def action_submit_composer(self) -> None:
        """Send whatever the composer holds, from any focused chat widget."""
        if self._answers_future is not None:
            self.query_one("#ask-user", AskUserBar).action_submit()
            return
        composer = self.query_one("#composer", Composer)
        if not composer.text.strip():
            composer.focus()
            return
        composer.action_submit()

    def action_submit_question(self) -> None:
        """Submit a pending agent question; a no-op otherwise.

        Ctrl+Enter and Ctrl+J need terminal support for modified keys, so
        Ctrl+S is the reliable fallback for the question bar.
        """
        if self._answers_future is not None:
            self.query_one("#ask-user", AskUserBar).action_submit()

    def action_focus_transcript(self) -> None:
        self.query_one("#transcript", TranscriptPane).focus()

    def action_find_transcript(self) -> None:
        self._find_transcript()

    def _find_transcript(self, query: str = "") -> None:
        pane = self.query_one("#transcript", TranscriptPane)
        entries = pane.search_entries()

        def reveal(index: int | None) -> None:
            if index is not None and entries[index][0].is_mounted:
                pane.reveal(entries[index][0])

        self.app.push_screen(
            TranscriptSearchDialog([(role, text) for _, role, text in entries], query),
            reveal,
        )

    def on_output_pending(self, event: OutputPending) -> None:
        self.query_one("#new-output").display = event.pending

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-output":
            self.action_latest_output()

    def action_latest_output(self) -> None:
        self.query_one("#transcript", TranscriptPane).jump_to_latest()

    def action_previous_message(self) -> None:
        self.query_one("#transcript", TranscriptPane).jump_user(-1)

    def action_next_message(self) -> None:
        self.query_one("#transcript", TranscriptPane).jump_user(1)

    def action_focus_composer(self) -> None:
        self.query_one("#composer", Composer).focus()

    @work(group="cancel", exclusive=False)
    async def _cancel_turn(self) -> None:
        self._queue_paused = True
        self.prompts.refresh_queue()
        try:
            await self.session.cancel()
        except HarnessClientError as exc:
            await self._notice(f"Could not cancel: {exc.message}", "error")
            return
        await self._notice("Cancellation requested; waiting for the agent to stop.", "warning")

    def action_new_session(self) -> None:
        self._new_session_worker()

    def action_reconnect(self) -> None:
        self._reconnect_worker()

    @work(group="reconnect", exclusive=True)
    async def _reconnect_worker(self) -> None:
        await self._reconnect()

    async def _reconnect(self) -> None:
        if self not in self.app.screen_stack:
            return
        if self.busy:
            await self._notice("Stop the running turn before reconnecting.", "warning")
            return
        if self._loading_session or self._replay_target is not None:
            await self._notice("Wait for the current session load to finish.", "warning")
            return
        if not self.connected:
            await self._notice("Reconnecting…")
        session_id = self.session.session_id
        cwd = self.session.cwd
        old_client = self.client
        with contextlib.suppress(Exception):
            await old_client.disconnect()
        try:
            new_client = self.app.make_client()
            await new_client.connect()
        except HarnessClientError as exc:
            self.connected = False
            if self in self.app.screen_stack:
                self._update_chrome()
                await self._notice(f"Reconnect failed: {exc.message}", "error")
            return
        if self not in self.app.screen_stack:
            with contextlib.suppress(Exception):
                await new_client.disconnect()
            return
        self.client = new_client
        self.app.client = new_client
        self.connected = True
        self._reset_activity()
        self._subscribe()
        await self._notice("Reconnected.", "success")
        if self.client.supports_load:
            pane = self.query_one("#transcript", TranscriptPane)
            self._replay_target = session_id
            pane.begin_replay()
            try:
                loaded = await self.client.load_session(session_id, cwd)
                await self.client.drain_events()
            except HarnessClientError as exc:
                await pane.end_replay()
                self._replay_target = None
                await self._notice(
                    f"Reconnected, but the session could not be reloaded: {exc.message}. "
                    "Press Ctrl+N for a new session.",
                    "error",
                )
                self._update_chrome()
                self._refresh_panels()
                return
            await pane.end_replay()
            self._replay_target = None
            if self not in self.app.screen_stack:
                return
            self.session = loaded
            self.app.session = loaded
            await self._notice(
                f"Session {session_id} reloaded from authoritative history.", "success"
            )
        else:
            await self._notice(
                "Reconnected. This agent does not advertise session/load, so the transcript cannot "
                "be replayed; press Ctrl+N for a new session.",
                "warning",
            )
        self._update_chrome()
        self.query_one("#composer", Composer).focus()
        self._refresh_panels()

    async def action_quit_app(self) -> None:
        await self.app.action_quit_app()
