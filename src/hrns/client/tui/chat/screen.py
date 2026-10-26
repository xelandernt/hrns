"""Full-screen screens: connection picker and the conversation workspace."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from hrns.config import LocalSettings

from ...client import HrnsAcpClient, SessionProxy
from ...errors import HrnsClientError
from ...events import (
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
from ...interactions import (
    AskUserRequest,
    AskUserResponse,
    PermissionDecision,
    PermissionRequest,
)
from ...launch import ConnectionTarget
from ..choice_keys import handle_choice_arrow
from ..completion import FileIndex
from ..local import LocalModelResult

if TYPE_CHECKING:
    pass

from ..approval import ApprovalBar, AskUserBar
from ..chat.commands import CommandWorkflow
from ..chat.model import ModelWorkflow
from ..chat.prompts import PromptWorkflow
from ..chat.session import SessionWorkflow
from ..composer import (
    AttachmentBar,
    CommandSuggestions,
    Composer,
    PromptQueue,
)
from ..messages import (
    ApprovalDecided,
    AskUserAnswered,
    AttachmentRemoved,
    CommandSuggestionKey,
    OutputPending,
    QueueAction,
    Submitted,
)
from ..panels import SidePanel, SidePanelCloseRequested
from ..panels.capability import (
    BindingEditRequested,
    BindingToggleRequested,
    CapabilityAddRequested,
    CapabilityRefreshRequested,
    ConfigOptionRequested,
    ConfigOptionStepRequested,
)
from ..panels.mcp import McpRefreshRequested
from ..panels.session import (
    SessionCloseRequested,
    SessionDeleteRequested,
    SessionLoadRequested,
    SessionMoreRequested,
    SessionNewRequested,
    SessionRefreshRequested,
    SessionResumeRequested,
)
from ..screen_base import _HrnsScreen
from ..state import (
    Settings,
    activity_label,
)
from ..status import StatusBar
from ..transcript import TranscriptPane
from .actions import ChatActions
from .capabilities import CapabilityWorkflow
from .chrome import ChromeWorkflow
from .connection import ReconnectWorkflow

__all__ = ["ChatScreen"]


class ChatScreen(_HrnsScreen):
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
        client: HrnsAcpClient,
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
        self.actions = ChatActions(self)
        self.chrome = ChromeWorkflow(self)
        self.connection = ReconnectWorkflow(self)
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
        self.chrome.apply_settings()
        self.chrome.apply_layout()
        self._subscribe()
        self._bind_view_context()
        self.chrome.update_chrome()
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
            # Tracked on the screen and cancelled in on_unmount; _consume_events
            # handles and reports every caught exception.
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

    def on_resize(self, event: Any) -> None:
        self.chrome.apply_layout()

    # ------------------------------------------------------------------ chrome

    async def _notice(self, text: str, severity: str = "info") -> None:
        await self.query_one("#transcript", TranscriptPane).add_notice(text, severity=severity)

    # ---------------------------------------------------------------- bootstrap

    @work(exit_on_error=False, group="bootstrap", exclusive=True)
    async def _run_bootstrap(self) -> None:
        if self.initial_load and self.initial_session_id:
            await self.sessions.load(self.initial_session_id, cwd=self.session.cwd, initial=True)
        self._refresh_panels()

    @work(exit_on_error=False, group="panels", exclusive=True)
    async def _refresh_panels(self) -> None:
        await self.capabilities.refresh()
        await self.sessions.refresh()

    @work(exit_on_error=False, group="panels", exclusive=True)
    async def refresh_capabilities(self) -> None:
        await self.capabilities.refresh()

    @work(exit_on_error=False, group="panels", exclusive=True)
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
            self.chrome.refresh_status()
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
            self.chrome.update_chrome()
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
            self.chrome.update_chrome()
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
            self.chrome.update_chrome()
            return
        if isinstance(event, SessionInfoChanged):
            if event.title:
                self._session_title = event.title
                self.chrome.update_chrome()
                if not self._loading_session:
                    self.refresh_sessions()
            return
        if isinstance(event, UsageUpdated):
            if event.summary.get("scope") == "context":
                self._context_usage = event.summary
                self.chrome.refresh_status()
                return
            if (
                self._usage
                and self._usage.get("scope") == "session"
                and event.summary.get("scope") != "session"
            ):
                return
            self._usage = event.summary
            self.query_one("#side-panel", SidePanel).activity.set_usage(event.summary)
            self.chrome.update_chrome()
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
            self.chrome.update_chrome()
            return
        if isinstance(event, ConnectionEvent) and event.state == "ready":
            self.connected = True
            self.chrome.update_chrome()
            return

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if not busy:
            self._activity = ""
        else:
            self._hint_note = ""
            if not self._activity:
                self._activity = "working"
        self.chrome.refresh_status()
        if not busy:
            if self._approval_future is not None:
                self._resolve_approval(None)
            if self._redirect_after_turn:
                self._redirect_after_turn = False
                self._hint_note = "Turn stopped · type a new instruction for the agent"
                self.chrome.refresh_status()
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

    @work(exit_on_error=False, group="file-index", exclusive=True)
    async def _index_workspace(self) -> None:
        await self.prompts.index_workspace()

    def on_command_suggestion_key(self, event: CommandSuggestionKey) -> None:
        self.prompts.handle_suggestion_key(event.key)

    @work(exit_on_error=False, group="attach-file", exclusive=False)
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

    @work(exit_on_error=False, group="capability", exclusive=True)
    async def _choose_plugin_to_add(self) -> None:
        await self.capabilities.choose_plugin_to_add()

    @work(exit_on_error=False, group="command", exclusive=True)
    async def _dispatch_local_command(self, name: str) -> None:
        await self.commands.run_local_command(name)

    @work(exit_on_error=False, group="command", exclusive=True)
    async def _dispatch_plugin_command(self, binding_id: str, command_name: str) -> None:
        await self.commands.run_plugin_command(binding_id, command_name, "")

    @work(exit_on_error=False, group="capability", exclusive=True)
    async def _plugin_add_form_worker(self, plugin_id: str) -> None:
        await self.capabilities.plugin_add_form(plugin_id)

    @work(exit_on_error=False, group="session", exclusive=True)
    async def _rename_session_worker(self, title: str) -> None:
        await self.sessions.rename(title)

    @work(exit_on_error=False, group="model", exclusive=True)
    async def _save_model_selection_worker(
        self,
        settings: LocalSettings,
        model: str,
        effort: str | None,
        *,
        touch_thinking: bool = True,
    ) -> None:
        await self.models.save_model_selection(
            settings, model, effort, touch_thinking=touch_thinking
        )

    @work(exit_on_error=False, group="model", exclusive=True)
    async def _set_local_effort_worker(self, settings: LocalSettings, effort: str) -> None:
        await self.models.set_local_effort(settings, effort)

    @work(exit_on_error=False, group="model", exclusive=True)
    async def _apply_model_settings_worker(self, result: LocalModelResult) -> None:
        await self.models.apply_model_settings(result)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Surface a failed background action instead of letting it disappear."""
        if event.state is not WorkerState.ERROR or event.worker.group == "notice":
            return
        error = event.worker.error
        if error is not None:
            self._notice_soon(f"Background task failed: {error}", "error")

    def _notice_soon(self, text: str, severity: str = "info") -> None:
        """Run a notice on a tracked worker so its failure cannot go unobserved."""
        self.run_worker(
            self._notice(text, severity),
            group="notice",
            exclusive=False,
            exit_on_error=False,
        )

    async def _handle_command(self, text: str) -> None:
        await self.commands.handle_command(text)

    def on_capability_refresh_requested(self, event: CapabilityRefreshRequested) -> None:
        self.refresh_capabilities()

    def on_mcp_refresh_requested(self, event: McpRefreshRequested) -> None:
        event.stop()
        self._refresh_mcp()

    def on_capability_add_requested(self, event: CapabilityAddRequested) -> None:
        self._choose_plugin_to_add()

    def on_binding_toggle_requested(self, event: BindingToggleRequested) -> None:
        self._toggle_binding(event.binding_id, event.enabled)

    def on_binding_edit_requested(self, event: BindingEditRequested) -> None:
        self.capabilities.edit_binding(event.binding_id)

    def on_config_option_requested(self, event: ConfigOptionRequested) -> None:
        self._change_standard_option(event.option_id)

    def on_config_option_step_requested(self, event: ConfigOptionStepRequested) -> None:
        self.models.step_standard_option(event.option_id, event.direction)

    @work(exit_on_error=False, group="capability", exclusive=True)
    async def _toggle_binding(self, binding_id: str, enabled: bool) -> None:
        await self.capabilities.toggle_binding(binding_id, enabled)

    @work(exit_on_error=False, group="mcp", exclusive=True)
    async def _refresh_mcp(self) -> None:
        await self.capabilities.refresh_mcp()

    @work(exit_on_error=False, group="capability", exclusive=True)
    async def _add_capability(
        self,
        plugin_id: str,
        binding_id: str,
        enabled: bool,
        config: dict[str, Any],
        change_mode: str = "next_turn",
    ) -> None:
        await self.capabilities.add_capability(plugin_id, binding_id, enabled, config, change_mode)

    @work(exit_on_error=False, group="capability", exclusive=True)
    async def _apply_binding_config(self, binding_id: str, values: dict[str, Any]) -> None:
        await self.capabilities.apply_binding_config(binding_id, values)

    @work(exit_on_error=False, group="capability", exclusive=True)
    async def _change_standard_option(self, option_id: str) -> None:
        await self.models.change_standard_option(option_id)

    @work(exit_on_error=False, group="capability", exclusive=True)
    async def _set_standard_value(self, option_id: str, value: str) -> None:
        await self.models.set_standard_value(option_id, value)

    # ----------------------------------------------------------------- sessions

    def on_session_load_requested(self, event: SessionLoadRequested) -> None:
        self._load_session_worker(event.session_id)

    @work(exit_on_error=False, group="session", exclusive=True)
    async def _load_session_worker(self, session_id: str) -> None:
        await self.sessions.load(session_id, cwd=self.sessions.session_cwd(session_id))

    def on_session_resume_requested(self, event: SessionResumeRequested) -> None:
        self._resume_session_worker(event.session_id)

    @work(exit_on_error=False, group="session", exclusive=True)
    async def _resume_session_worker(self, session_id: str) -> None:
        await self.sessions.resume(session_id)

    def on_session_new_requested(self, event: SessionNewRequested) -> None:
        self._new_session_worker()

    @work(exit_on_error=False, group="session", exclusive=True)
    async def _new_session_worker(self) -> None:
        await self.sessions.new()

    def on_session_delete_requested(self, event: SessionDeleteRequested) -> None:
        self.sessions.confirm_delete(event.session_id)

    @work(exit_on_error=False, group="session", exclusive=True)
    async def _delete_session(self, session_id: str) -> None:
        await self.sessions.delete(session_id)

    def on_session_close_requested(self, event: SessionCloseRequested) -> None:
        self._close_session(event.session_id)

    @work(exit_on_error=False, group="session", exclusive=True)
    async def _close_session(self, session_id: str) -> None:
        await self.sessions.close(session_id)

    def on_session_refresh_requested(self, event: SessionRefreshRequested) -> None:
        self.refresh_sessions()

    def on_session_more_requested(self, event: SessionMoreRequested) -> None:
        self._load_more_sessions()

    @work(exit_on_error=False, group="panels", exclusive=True)
    async def _load_more_sessions(self) -> None:
        await self.sessions.load_more()

    # ------------------------------------------------------------------ models

    @work(exit_on_error=False, group="model", exclusive=True)
    async def _set_mode(self, mode_id: str) -> None:
        await self.models.set_mode(mode_id)

    # ----------------------------------------------------------------- actions

    def on_side_panel_close_requested(self, event: SidePanelCloseRequested) -> None:
        event.stop()
        self._close_panel()

    def action_show_capabilities(self) -> None:
        self.actions.show_capabilities()

    def action_show_todos(self) -> None:
        self.actions.show_todos()

    def action_show_mcp(self) -> None:
        self.actions.show_mcp()

    def action_show_sessions(self) -> None:
        self.actions.show_sessions()

    def action_show_activity(self) -> None:
        self.actions.show_activity()

    def action_toggle_panel(self) -> None:
        self.actions.toggle_panel()

    def _show_panel(self, tab: str, *, focus: bool = True) -> None:
        self.actions.show_panel(tab, focus=focus)

    def _close_panel(self) -> None:
        self.actions.close_panel()

    def action_command_palette(self) -> None:
        self.actions.command_palette()

    def action_help(self) -> None:
        self.actions.help()

    def action_find_transcript(self) -> None:
        self.actions.find_transcript()

    def _find_transcript(self, query: str = "") -> None:
        self.actions.find_transcript(query)

    def _show_permissions(self) -> None:
        self.actions.show_permissions()

    def _show_settings(self) -> None:
        self.actions.show_settings()

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

    @work(exit_on_error=False, group="cancel", exclusive=False)
    async def _cancel_turn(self) -> None:
        self._queue_paused = True
        self.prompts.refresh_queue()
        try:
            await self.session.cancel()
        except HrnsClientError as exc:
            await self._notice(f"Could not cancel: {exc.message}", "error")
            return
        await self._notice("Cancellation requested; waiting for the agent to stop.", "warning")

    def action_new_session(self) -> None:
        self._new_session_worker()

    def action_reconnect(self) -> None:
        self._reconnect_worker()

    @work(exit_on_error=False, group="reconnect", exclusive=True)
    async def _reconnect_worker(self) -> None:
        await self._reconnect()

    async def _reconnect(self) -> None:
        await self.connection.reconnect()

    async def action_quit_app(self) -> None:
        await self.app.action_quit_app()
