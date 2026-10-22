"""Full-screen screens: connection picker and the conversation workspace."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, ListItem, ListView, Static

from harness.config import (
    EFFORT_DEFAULT,
    LocalSettings,
    thinking_from_settings,
    with_thinking,
)

from ..attachments import attachment_summary, build_prompt_blocks, validate_attachment
from ..catalog import AgentDeployment
from ..client import HarnessAcpClient, SessionProxy
from ..constants import CLIENT_VERSION
from ..errors import ExtensionUnavailable, HarnessClientError
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
    TurnCompleted,
    TurnStarted,
    UsageUpdated,
    UserMessageReplayed,
)
from ..interactions import PERMISSION_MODES, PermissionDecision, PermissionRequest
from ..launch import ConnectionTarget, catalog_for, default_remote_cwd, local_cwd
from ..local import local_model_catalog, local_target, local_thinking_support
from ..profiles import Profile, Profiles, default_profiles_path
from .choice_keys import handle_choice_arrow, stepped_value
from .commands import LOCAL_COMMANDS, command_suggestion_text, resolve_local_command
from .completion import matching_files, workspace_files
from .dialogs import (
    ChoiceDialog,
    CommandPaletteDialog,
    ConfirmDialog,
    FormDialog,
    HelpDialog,
    InfoDialog,
    SettingsDialog,
    TranscriptSearchDialog,
)
from .local import LocalModelResult, effort_choices, effort_label
from .state import (
    Settings,
    activity_label,
    binding_summary,
    build_form_fields,
    config_option_value,
    effort_option,
    format_usage_compact,
    model_option,
    option_values,
    permission_mode_label,
)
from .widgets import (
    ApprovalBar,
    ApprovalDecided,
    AttachmentBar,
    AttachmentRemoved,
    BindingEditRequested,
    BindingToggleRequested,
    CapabilityAddRequested,
    CapabilityRefreshRequested,
    CommandSuggestionKey,
    CommandSuggestions,
    Composer,
    ConfigOptionRequested,
    ConfigOptionStepRequested,
    OutputPending,
    PromptQueue,
    QueueAction,
    SessionCloseRequested,
    SessionDeleteRequested,
    SessionLoadRequested,
    SessionMoreRequested,
    SessionNewRequested,
    SessionRefreshRequested,
    SessionResumeRequested,
    SidePanel,
    StatusBar,
    Submitted,
    TopBar,
    TranscriptPane,
)

__all__ = ["ChatScreen", "ConnectScreen", "PickEntry"]


@dataclass(slots=True)
class PickEntry:
    kind: str  # "local" | "server" | "agent" | "back"
    label: str
    detail: str
    profile: Profile | None = None
    deployment: AgentDeployment | None = None

    @property
    def is_selectable(self) -> bool:
        return self.kind != "back"


def _short_path(path: str, limit: int = 44) -> str:
    if len(path) <= limit:
        return path
    return "…" + path[-limit:]


class ConnectScreen(Screen[None]):
    """Searchable profile/agent picker with explicit local versus remote location."""

    BINDINGS = [
        Binding("q", "quit_app", "Quit", show=False),
        Binding("escape", "escape_picker", "Back", show=False),
        Binding("r", "refresh", "Refresh catalog", show=False),
        Binding("slash", "filter", "Filter", show=False),
        Binding("l", "configure_local", "Local model", show=False),
        Binding("n", "add_target", "New target", show=False),
    ]

    def __init__(
        self,
        profiles: Profiles,
        *,
        initial_target: ConnectionTarget | None = None,
        auto_connect: bool = False,
        status_message: str | None = None,
    ) -> None:
        super().__init__()
        self.profiles = profiles
        self.initial_target = initial_target
        self.auto_connect = auto_connect
        self.status_message = status_message
        self.entries: list[PickEntry] = []
        self._visible: list[PickEntry] = []
        self._browsing: Profile | None = None
        self._catalog_agents: list[AgentDeployment] = []
        self._connecting = False
        self._list = ListView(id="target-list")
        self._filter = Input(placeholder="Filter connections", id="connect-filter")
        self._detail = Static("", markup=False, id="connect-detail-body")
        self._status = Static("", markup=False, id="connect-status")
        self._banner = Static(id="connect-banner")

    def compose(self) -> ComposeResult:
        yield self._banner
        with Horizontal(id="connect-body"):
            with Vertical(id="connect-list-wrap"):
                yield Static("Connections", classes="section-title", markup=False)
                yield self._filter
                yield self._list
            with VerticalScroll(id="connect-detail"):
                yield self._detail
        yield self._status
        yield Static(
            "l configure local model · n add target · / filter · r refresh · q quit",
            markup=False,
            classes="panel-hint",
            id="connect-hint",
        )

    def on_mount(self) -> None:
        self._render_banner()
        self._render_entries()
        self._list.focus()
        if self.auto_connect and self.initial_target is not None:
            self._connect_worker(self.initial_target)
        elif self.status_message:
            self._status.update(self.status_message)
        else:
            self._status.update(
                "Local model is the default. Press l to configure it; selecting it connects. "
                "saved remote profiles are listed below."
            )
        self._apply_layout()
        if self.initial_target is not None:
            self._status.update(f"Ready to connect · {self.initial_target.location_text()}")

    def on_screen_resume(self) -> None:
        self._render_entries()

    def on_resize(self, event: Any) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        width = self.app.size.width
        detail = self.query("#connect-detail")
        if detail:
            detail.first().display = width >= 72
        wrap = self.query("#connect-list-wrap")
        if wrap:
            wrap.first().styles.width = "46%" if width >= 72 else "100%"
        self.query_one("#connect-hint").display = width >= 60

    def _render_banner(self) -> None:
        text = Text()
        text.append("harness", style="bold")
        text.append("  a terminal for ACP agents", style="dim")
        self._banner.update(text)

    # ------------------------------------------------------------------ entries

    def _render_entries(self) -> None:
        self.entries = []
        self._list.clear()
        if self._browsing is not None:
            profile = self._browsing
            self.entries.append(
                PickEntry(
                    kind="back",
                    label="← All connections",
                    detail="Return to the saved profile list.",
                    profile=profile,
                )
            )
            for agent in self._catalog_agents:
                self.entries.append(
                    PickEntry(
                        kind="agent",
                        label=f"{agent.label}  ·  {agent.version or agent.agent_id}",
                        detail=agent.description or f"deployment {agent.agent_id}",
                        profile=profile,
                        deployment=agent,
                    )
                )
            if not self._catalog_agents:
                self.entries.append(
                    PickEntry(
                        kind="back",
                        label="No agents returned",
                        detail="The catalog returned no accessible deployments for this profile.",
                        profile=profile,
                    )
                )
        else:
            settings = self._local_settings()
            local_ready = settings is not None and bool(settings.model)
            if settings is None or not settings.model:
                self.entries.append(
                    PickEntry(
                        kind="configure_local",
                        label="Configure local model…",
                        detail="Choose a cloud or local provider and model, then work in this project.",
                    )
                )
            else:
                effort = effort_label(thinking_from_settings(settings))
                self.entries.append(
                    PickEntry(
                        kind="local_model",
                        label=(
                            f"Local model · {settings.provider}:{settings.model} · effort {effort}"
                        ),
                        detail="Run the configured provider and model on this project.",
                    )
                )
            local = [
                profile for profile in self.profiles.profiles.values() if not profile.is_remote
            ]
            remote = [profile for profile in self.profiles.profiles.values() if profile.is_remote]
            for profile in sorted(local, key=lambda item: item.name):
                self.entries.append(
                    PickEntry(
                        kind="local",
                        label=f"local · {profile.name}",
                        detail=profile.describe_location(),
                        profile=profile,
                    )
                )
            for profile in sorted(remote, key=lambda item: item.name):
                self.entries.append(
                    PickEntry(
                        kind="server",
                        label=f"remote · {profile.name}",
                        detail=f"{profile.url} · browse deployments",
                        profile=profile,
                    )
                )
            if local_ready:
                self.entries.append(
                    PickEntry(
                        kind="configure_local",
                        label="Change local model…",
                        detail="Choose another cloud or local provider and model.",
                    )
                )
            self.entries.append(
                PickEntry(
                    kind="add_target",
                    label="Add remote or ACP target…",
                    detail="Save a server profile or a stdio ACP command, then connect to it.",
                )
            )
        self._render_visible(self._filter.value if self.is_mounted else "")

    def _local_settings(self) -> LocalSettings | None:
        try:
            return LocalSettings()
        except (ValueError, OSError):
            return None

    def _render_visible(self, needle: str) -> None:
        needle = (needle or "").strip().lower()
        self._list.clear()
        self._visible = [
            entry
            for entry in self.entries
            if not needle or needle in entry.label.lower() or needle in entry.detail.lower()
        ]
        for entry in self._visible:
            label = Text()
            if entry.kind == "back":
                label.append(entry.label, style="dim")
            elif entry.kind == "local":
                label.append("local  ", style="bold")
                label.append(entry.label.removeprefix("local · "))
            elif entry.kind == "local_model":
                label.append("local  ", style="bold")
                label.append(entry.label.removeprefix("Local model · "))
            elif entry.kind == "server":
                label.append("remote ", style="bold")
                label.append(entry.label.removeprefix("remote · "))
            elif entry.kind == "configure_local":
                label.append("setup  ", style="bold")
                label.append(entry.label)
            elif entry.kind == "add_target":
                label.append("new    ", style="bold")
                label.append(entry.label)
            else:
                label.append("agent  ", style="bold")
                label.append(entry.label)
            if entry.deployment is not None and entry.deployment.recommended:
                label.append("   recommended", style="dim")
            self._list.append(ListItem(Static(label, markup=False)))
        if self._visible:
            self._list.index = 0
            self._show_detail(self._visible[0])
        else:
            self._detail.update("No connections match this filter.")

    def _show_detail(self, entry: PickEntry) -> None:
        lines: list[str] = [entry.label]
        if entry.profile is not None:
            profile = entry.profile
            lines.append(f"{'remote' if profile.is_remote else 'local'} · profile {profile.name}")
            lines.append("")
            if profile.is_remote:
                lines.append(f"server: {profile.url}")
                lines.append(
                    f"agent: {entry.deployment.agent_id if entry.deployment else profile.agent or '(choose)'}"
                )
                if entry.deployment is not None:
                    deployment = entry.deployment
                    if deployment.description:
                        lines.append("")
                        lines.append(deployment.description)
                    if deployment.workspaces:
                        lines.append("")
                        lines.append("workspaces:")
                        for workspace in deployment.workspaces:
                            lines.append(f"  {workspace}")
                    if deployment.default_working_directory:
                        lines.append(f"default workspace: {deployment.default_working_directory}")
                else:
                    lines.append("Select it to list deployments from this server.")
            else:
                lines.append(f"command: {profile.command} {' '.join(profile.args)}".rstrip())
                if profile.cwd:
                    lines.append(f"working directory: {profile.cwd}")
                lines.append("")
                lines.append(
                    "The client launches this executable directly (no shell) and speaks ACP on stdio."
                )
        if entry.detail:
            lines.append("")
            lines.append(entry.detail)
        self._detail.update("\n".join(lines))

    def on_key(self, event: Any) -> None:
        """Left/Right move through the connection list like Up/Down."""
        if handle_choice_arrow(self.focused, str(event.key)):
            event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._render_visible(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if len(self._visible) == 1:
            self._activate(self._visible[0])
        else:
            self._list.focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None:
            index = self._list.index
            if index is not None and 0 <= index < len(self._visible):
                self._show_detail(self._visible[index])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = self._list.index
        if index is None or index >= len(self._visible):
            return
        self._activate(self._visible[index])

    def action_filter(self) -> None:
        self._filter.focus()

    def _activate(self, entry: PickEntry) -> None:
        if self._connecting:
            return
        if entry.kind == "configure_local":
            self.action_configure_local()
            return
        if entry.kind == "add_target":
            self.action_add_target()
            return
        if entry.kind == "local_model":
            try:
                self._connect_worker(local_target())
            except HarnessClientError as exc:
                self._status.update(f"error · {exc.message}")
            return
        if entry.kind == "back":
            self._browsing = None
            self._render_entries()
            return
        if entry.kind == "server":
            if entry.profile is not None:
                self._browse_profile(entry.profile)
            return
        try:
            target = self._target_for(entry)
        except HarnessClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        self._connect_worker(target)

    def action_configure_local(self) -> None:
        from .local import LocalModelDialog

        if self._connecting:
            return
        try:
            target = self.initial_target
            args = (
                target.profile.args if target and target.profile.command == "harness-local" else []
            )
            config = Path(args[1]) if len(args) == 2 and args[0] == "--config" else None
            settings = LocalSettings(config_file=config) if config else LocalSettings()
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        self.app.push_screen(LocalModelDialog(settings), self._local_configured)

    def action_add_target(self) -> None:
        if self._connecting:
            return
        fields = [
            {
                "name": "name",
                "title": "Profile name",
                "description": "A stable name for this connection.",
                "kind": "text",
                "required": True,
            },
            {
                "name": "transport",
                "title": "Transport",
                "description": "Remote server profiles use the catalog; stdio profiles launch an ACP command.",
                "kind": "select",
                "required": True,
                "choices": [("server", "Remote server"), ("stdio", "ACP command (stdio)")],
                "default": "server",
            },
            {
                "name": "url",
                "title": "Server URL",
                "description": "Required for remote servers, for example https://agents.example.com.",
                "kind": "text",
            },
            {
                "name": "agent",
                "title": "Default agent id",
                "description": "Optional deployment id; an empty value lets you choose from the catalog.",
                "kind": "text",
            },
            {
                "name": "token",
                "title": "Token reference",
                "description": "Use env:NAME or keyring:service/user. A literal value is stored in the profile file.",
                "kind": "text",
            },
            {
                "name": "remote_cwd",
                "title": "Remote working directory",
                "description": "Optional bridge rewrite target for this server.",
                "kind": "text",
            },
            {
                "name": "command",
                "title": "ACP command",
                "description": "Required for stdio: executable launched directly (no shell).",
                "kind": "text",
            },
            {
                "name": "args",
                "title": "Command arguments",
                "description": "Space-separated arguments; use quotes for values containing spaces.",
                "kind": "text",
            },
            {
                "name": "cwd",
                "title": "Working directory",
                "description": "Optional local working directory for a stdio command.",
                "kind": "text",
            },
            {
                "name": "auth_method",
                "title": "Auth method id",
                "description": "Optional agent-advertised authentication method to try automatically.",
                "kind": "text",
            },
            {
                "name": "notes",
                "title": "Notes",
                "description": "Optional reminder shown in the connection picker.",
                "kind": "text",
            },
        ]

        def validate_target(values: dict[str, Any]) -> str | None:
            if values.get("transport") == "server" and not str(values.get("url") or "").strip():
                return "A server URL is required for remote server profiles."
            if values.get("transport") == "stdio" and not str(values.get("command") or "").strip():
                return "An ACP command is required for stdio profiles."
            try:
                shlex.split(str(values.get("args") or ""))
            except ValueError as exc:
                return f"Invalid command arguments: {exc}"
            return None

        self.app.push_screen(
            FormDialog(
                "Add a connection target",
                "Saved to the first profiles file. Secrets should be references, not literals.",
                fields,
                accept_label="Save and connect",
                validate=validate_target,
            ),
            self._target_added,
        )

    def _target_added(self, values: dict[str, Any] | None) -> None:
        if not values:
            return
        try:
            name = str(values.get("name") or "").strip()
            transport = str(values.get("transport") or "server")
            command = str(values.get("command") or "").strip()
            url = str(values.get("url") or "").strip()
            if not name:
                raise ValueError("A profile name is required.")
            if transport == "server" and not url:
                raise ValueError("A server URL is required for remote server profiles.")
            if transport == "stdio" and not command:
                raise ValueError("An ACP command is required for stdio profiles.")
            profile = Profile(
                name=name,
                transport=transport,
                command=command,
                args=shlex.split(str(values.get("args") or "")),
                cwd=str(values.get("cwd") or "").strip(),
                url=url,
                agent=str(values.get("agent") or "").strip(),
                token=str(values.get("token") or "").strip(),
                remote_cwd=str(values.get("remote_cwd") or "").strip(),
                auth_method=str(values.get("auth_method") or "").strip(),
                notes=str(values.get("notes") or "").strip(),
            )
        except ValueError as exc:
            self._status.update(f"error · {exc}")
            return
        self.profiles.upsert(profile)
        target_path = self.profiles.sources[0] if self.profiles.sources else default_profiles_path()
        try:
            self.profiles.save(target_path)
        except OSError as exc:
            self._status.update(f"error · could not save profile: {exc}")
            return
        if target_path not in self.profiles.sources:
            self.profiles.sources.append(target_path)
        self._render_entries()
        self._status.update(f"Saved {name}. Connecting…")
        if profile.is_remote:
            self._browse_profile(profile)
            return
        try:
            target = ConnectionTarget(
                name=profile.name,
                kind="local",
                profile=profile,
                agent=None,
                cwd=local_cwd(profile),
            )
        except HarnessClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        self._connect_worker(target)

    def _local_configured(
        self, result: LocalModelResult | tuple[Path, str | None] | Path | None
    ) -> None:
        if result is None:
            return
        if isinstance(result, LocalModelResult):
            path = result.path
            api_key_ref = result.api_key_ref
        elif isinstance(result, tuple):
            path, api_key_ref = result
        else:
            path, api_key_ref = result, None
        try:
            target = local_target(path, api_key_ref=api_key_ref)
        except HarnessClientError as exc:
            self._status.update(f"Saved configuration · {exc.message}")
            return
        self._connect_worker(target)

    def _target_for(self, entry: PickEntry) -> ConnectionTarget:
        profile = entry.profile
        if profile is None:
            raise HarnessClientError("This entry has no connection profile.")
        if entry.kind == "local" or (profile.transport == "stdio" and entry.kind != "agent"):
            if not profile.command:
                raise HarnessClientError(f"Profile '{profile.name}' has no command.")
            return ConnectionTarget(
                name=profile.name,
                kind="local",
                profile=profile,
                agent=None,
                cwd=local_cwd(profile),
            )
        deployment = entry.deployment
        if deployment is None:
            raise HarnessClientError("Select a deployed agent first.")
        return ConnectionTarget(
            name=profile.name,
            kind="remote",
            profile=profile,
            agent=deployment,
            cwd=default_remote_cwd(deployment),
        )

    @work(group="catalog", exclusive=True)
    async def _browse_profile(self, profile: Profile) -> None:
        self._status.update(f"Loading deployments from {profile.url}…")
        try:
            async with catalog_for(profile) as catalog:
                agents: list[AgentDeployment] = []
                cursor: str | None = None
                for _ in range(4):
                    page = await catalog.list_agents(cursor=cursor, limit=50)
                    agents.extend(page.items)
                    cursor = page.next_cursor
                    if not cursor:
                        break
        except HarnessClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        self._catalog_agents = sorted(
            agents, key=lambda item: (not item.recommended, item.name.lower())
        )
        self._browsing = profile
        self._render_entries()
        self._list.focus()
        self._status.update(f"{len(agents)} deployments on {profile.url}. Esc returns to profiles.")

    @work(group="connect", exclusive=True)
    async def _connect_worker(self, target: ConnectionTarget) -> None:
        self._connecting = True
        self._status.update(f"Connecting to {target.location_text()}…")
        try:
            await self.app.connect_target(target)  # type: ignore[attr-defined]
        except HarnessClientError as exc:
            self._status.update(f"error · {exc.message}")
        except Exception as exc:  # unexpected startup failures stay on screen
            self._status.update(f"error · {exc}")
        finally:
            self._connecting = False
            if self.is_attached:
                self._list.focus()

    def action_escape_picker(self) -> None:
        if self._browsing is not None:
            self._browsing = None
            self._render_entries()
            self._status.update("Back to saved connections.")
        else:
            self.app.action_quit_app()  # type: ignore[attr-defined]

    def action_refresh(self) -> None:
        if self._browsing is not None:
            self._browse_profile(self._browsing)
        else:
            self._render_entries()
            self._status.update(
                "Profiles reloaded. Press n to add a target or edit the TOML file directly."
            )

    def action_quit_app(self) -> None:
        self.app.action_quit_app()  # type: ignore[attr-defined]


class ChatScreen(Screen[None]):
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
        self._session_title: str | None = None
        self._plan: tuple[tuple[str, str, str], ...] = ()
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
        self._panel_forced: bool | None = None
        self._turns_started = 0
        self._attachments: list[str] = []
        self._drafts: dict[str, tuple[str, list[str], list[tuple[str, list[str]]]]] = {}
        self._prompt_history: dict[str, list[str]] = {}
        self._queued_prompts: list[tuple[str, list[str]]] = []
        self._queue_paused = False
        self._session_rows: list[dict[str, Any]] = []
        self._session_cursor: str | None = None
        self._approval_future: asyncio.Future[PermissionDecision] | None = None
        self._redirect_after_turn = False
        self._suggestion_items: list[dict[str, Any]] = []
        self._suggestion_index = 0
        self._suggestion_signature: tuple[str, ...] = ()
        self._workspace_files: list[str] = []
        self._workspace_root = Path.cwd()
        if target.location().get("kind") == "local" and Path(target.cwd).is_dir():
            self._workspace_root = Path(target.cwd).resolve()

    # --------------------------------------------------------------- structure

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="main"):
            yield TranscriptPane(id="transcript")
            yield SidePanel()
        yield Button("New output ↓ · Ctrl+End", id="new-output")
        yield PromptQueue(id="prompt-queue")
        yield ApprovalBar(id="approval")
        yield CommandSuggestions(id="command-suggestions")
        yield AttachmentBar(id="attachments", markup=False)
        with Horizontal(id="composer-row"):
            yield Static("›", id="composer-prompt", markup=False)
            yield Composer(id="composer")
        yield StatusBar(id="statusbar")

    def on_mount(self) -> None:
        self.query_one("#new-output").display = False
        self._restore_draft()
        self._index_workspace()
        self._apply_settings()
        self._apply_layout()
        self._subscribe()
        self._update_chrome()
        self.query_one("#composer", Composer).focus()
        self._run_bootstrap()

    def on_unmount(self) -> None:
        self._resolve_approval(None, focus=False)
        if self._consumer is not None:
            self._consumer.cancel()
            self._consumer = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

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
        visible = self.settings.side_panel and width >= 96
        if self._panel_forced is not None:
            visible = self._panel_forced
        panel.display = visible
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
        extension = ""
        self.query_one("#topbar", TopBar).show(
            target_text=self.target.location_text(),
            agent_text=f"{self.client.agent_title} {self.client.agent_version}".strip(),
            extension_text=extension,
            session_id=self._session_title or session_id,
            connected=self.connected,
        )
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
            usage=format_usage_compact(self._usage),
            busy=self.busy,
            activity=self._activity,
            permission=permission_mode_label(self.settings.permission_mode),
            hint=self._hint_text(),
        )

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
        self._activity = ""
        self._hint_note = ""
        self._session_title = None
        self._plan = ()
        self._commands = ()
        self._plugin_commands = ()
        self._plugin_revision = 0
        self._bindings_raw = []
        self._turns_started = 0
        panel = self.query_one("#side-panel", SidePanel)
        panel.activity.set_plan((), cleared=True)
        panel.activity.clear_subagents()
        panel.activity.set_commands(())
        panel.activity.set_usage(None)
        self._refresh_status()

    # ---------------------------------------------------------------- bootstrap

    @work(group="bootstrap", exclusive=True)
    async def _run_bootstrap(self) -> None:
        if self.initial_load and self.initial_session_id:
            await self._load_session(self.initial_session_id, cwd=self.session.cwd, initial=True)
        self._refresh_panels()

    @work(group="panels", exclusive=True)
    async def _refresh_panels(self) -> None:
        await self._refresh_capabilities()
        await self._refresh_sessions()

    @work(group="panels", exclusive=True)
    async def refresh_capabilities(self) -> None:
        await self._refresh_capabilities()

    @work(group="panels", exclusive=True)
    async def refresh_sessions(self) -> None:
        await self._refresh_sessions()

    def _set_plugin_commands(self, commands: tuple[Any, ...], *, revision: int) -> None:
        self._plugin_commands = tuple(
            command.model_dump(mode="json", by_alias=True)
            if hasattr(command, "model_dump")
            else dict(command)
            for command in commands
        )
        self._plugin_revision = revision
        self._sync_command_suggestions()

    async def _refresh_plugin_commands(self) -> None:
        if not self.client.extension.supports("_harness/commands/list"):
            self._set_plugin_commands((), revision=0)
            return
        try:
            result = await self.session.list_commands()
        except (ExtensionUnavailable, HarnessClientError):
            self._set_plugin_commands((), revision=0)
            return
        self._set_plugin_commands(result.commands, revision=result.selected_revision)

    async def _refresh_capabilities(self) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        await self._refresh_plugin_commands()
        if self.session.client is not self.client:
            panel.show_message(
                "This session is not attached to the reconnected client. "
                "Start a new session to change plugins."
            )
            return
        if self.client.extension.supports(
            "_harness/capabilities/list"
        ) and self.client.extension.supports("_harness/configuration/get"):
            try:
                bindings = await self._all_capability_pages()
                snapshot = await self.session.configuration()
                self._bindings_raw = [
                    binding.model_dump(mode="json", by_alias=True) for binding in bindings
                ]
                panel.set_bindings(
                    self._bindings_raw,
                    snapshot.to_wire(),
                    active_revision=snapshot.active_revision,
                    selected_revision=snapshot.selected_revision,
                    can_write=self.client.extension.supports("_harness/configuration/update"),
                )
                return
            except ExtensionUnavailable:
                pass
            except HarnessClientError as exc:
                panel.show_message(exc.message)
                return
        panel.show_message("Loading standard configuration…")
        panel.set_standard_options(list(self.session.config_options))

    async def _refresh_sessions(self, *, load_more: bool = False) -> None:
        panel = self.query_one("#side-panel", SidePanel).sessions
        if not self.client.has_session_capability("list"):
            panel.show_message("This agent does not advertise session/list.")
            return
        if not load_more:
            self._session_rows = []
            self._session_cursor = None
        if load_more and not self._session_cursor:
            panel.show_message("No further session pages are advertised.")
            return
        try:
            listing = await self.client.list_sessions(cursor=self._session_cursor)
        except HarnessClientError as exc:
            panel.show_message(exc.message)
            return
        known = {
            str(item.get("sessionId") or item.get("session_id") or "")
            for item in self._session_rows
        }
        for item in listing.sessions:
            session_id = str(item.get("sessionId") or item.get("session_id") or "")
            if session_id and session_id not in known:
                self._session_rows.append(item)
                known.add(session_id)
        self._session_cursor = listing.next_cursor
        panel.set_sessions(
            list(self._session_rows),
            can_delete=self.client.has_session_capability("delete"),
            can_close=self.client.has_session_capability("close"),
            can_resume=self.client.has_session_capability("resume"),
            can_load=self.client.supports_load,
            next_cursor=self._session_cursor,
        )

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
            self.query_one("#side-panel", SidePanel).activity.note_tool(
                {
                    "toolCallId": event.tool_call_id,
                    "title": event.title,
                    "kind": event.kind,
                    "status": event.status,
                    "raw_input": event.raw_input,
                }
            )
            if event.status in {"pending", "in_progress"}:
                self._activity = activity_label(event.kind, event.title)
            elif self.busy:
                self._activity = "working"
            self._refresh_status()
            return
        if isinstance(event, TurnStarted):
            self._turns_started += 1
            self._set_busy(True)
            return
        if isinstance(event, TurnCompleted):
            if event.stop_reason not in {"end_turn", ""}:
                self._queue_paused = True
            self._refresh_queue()
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
            self._sync_command_suggestions()
            return
        if isinstance(event, PluginCommandsListed):
            self._set_plugin_commands(event.commands, revision=event.selected_revision)
            return
        if isinstance(event, PlanChanged):
            self._plan = event.entries
            self.query_one("#side-panel", SidePanel).activity.set_plan(
                event.entries, cleared=event.cleared
            )
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
                self._apply_snapshot(event.snapshot)
            except Exception as exc:
                await self._notice(f"configuration update was malformed: {exc}", "error")
            return
        if isinstance(event, ConnectionEvent) and event.state == "lost":
            self._queue_paused = True
            self._refresh_queue()
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

    def _apply_snapshot(self, snapshot: Any) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        if self._bindings_raw:
            panel.set_bindings(
                self._bindings_raw,
                snapshot.to_wire(),
                active_revision=snapshot.active_revision,
                selected_revision=snapshot.selected_revision,
                can_write=self.client.extension.supports("_harness/configuration/update"),
            )

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
        self.query_one("#approval", ApprovalBar).show(request)
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
        composer = self.query_one("#composer", Composer)
        if getattr(event, "text_area", None) is composer:
            self._sync_command_suggestions()

    def on_text_area_selection_changed(self, event: Any) -> None:
        composer = self.query_one("#composer", Composer)
        if getattr(event, "text_area", None) is composer and composer.has_focus:
            self._sync_command_suggestions()

    @work(group="file-index", exclusive=True)
    async def _index_workspace(self) -> None:
        self._workspace_files = await asyncio.to_thread(workspace_files, self._workspace_root)
        self._sync_command_suggestions()

    def _sync_file_suggestions(self) -> None:
        composer = self.query_one("#composer", Composer)
        row, column = composer.cursor_location
        line = composer.document.get_line(row)[:column]
        match = re.search(r"(?<!\S)@([^\s@]*)$", line)
        if match is None or not composer.has_focus:
            self._hide_command_suggestions()
            return
        items = [
            {
                "kind": "file",
                "name": path,
                "label": f"@{path}",
                "start": (row, match.start()),
                "end": (row, column),
            }
            for path in matching_files(self._workspace_files, match.group(1))
        ]
        if not items:
            self._hide_command_suggestions()
            return
        signature = tuple(item["name"] for item in items)
        if signature != self._suggestion_signature:
            self._suggestion_index = 0
            self._suggestion_signature = signature
        self._suggestion_items = items
        self._render_command_suggestions()
        composer.suggestions_open = True

    def _sync_command_suggestions(self) -> None:
        composer = self.query_one("#composer", Composer)
        raw = composer.text
        if not raw.startswith("/") or "\n" in raw:
            self._sync_file_suggestions()
            return
        body = raw[1:]
        if " " in body or "\t" in body:
            self._hide_command_suggestions()
            return
        needle = body.strip().lower()
        local = [
            command for command in LOCAL_COMMANDS if not command.hidden and command.matches(needle)
        ]
        if needle:
            local.sort(
                key=lambda command: (
                    0
                    if needle
                    in {command.name.lower(), *(alias.lower() for alias in command.aliases)}
                    else 1
                )
            )
        local_names = {
            name for command in LOCAL_COMMANDS for name in (command.name, *command.aliases)
        }
        agent = [
            command
            for command in self._commands
            if needle in str(command.get("name", "")).lower()
            or needle in str(command.get("description", "")).lower()
        ]
        if needle:
            agent.sort(
                key=lambda command: 0 if needle == str(command.get("name", "")).lower() else 1
            )
        plugins = []
        for command in self._plugin_commands:
            display = self._plugin_command_display(command)
            if (
                not needle
                or needle in display.lower()
                or needle in str(command.get("description", "")).lower()
            ):
                plugins.append(command)
        if needle:
            plugins.sort(
                key=lambda command: (
                    0
                    if needle == self._plugin_command_display(command).lower()
                    else 1
                )
            )

        items: list[dict[str, Any]] = []
        for command in local:
            items.append(
                {
                    "kind": "local",
                    "name": command.name,
                    "label": command_suggestion_text(command),
                    "command": command,
                }
            )
        for command in agent[:100]:
            name = str(command.get("name", ""))
            if not name:
                continue
            description = str(command.get("description", ""))
            collision = name in local_names
            label = f"/{name}   {description}".rstrip()
            if collision:
                label += "   (agent command; use /agent)"
            items.append(
                {
                    "kind": "agent",
                    "name": name,
                    "label": label,
                    "collision": collision,
                }
            )
        for command in plugins[:100]:
            display = self._plugin_command_display(command)
            description = str(command.get("description", ""))
            hint = str(command.get("argumentsHint") or command.get("arguments_hint") or "")
            label = f"/{display}   {description}".rstrip()
            if hint:
                label += f"   ({hint})"
            items.append(
                {
                    "kind": "plugin",
                    "name": display,
                    "bindingId": command.get("bindingId") or command.get("binding_id"),
                    "commandName": command.get("commandName") or command.get("command_name"),
                    "label": label,
                }
            )
        if not items:
            self._hide_command_suggestions()
            return

        signature = tuple(f"{item['kind']}:{item['name']}" for item in items)
        if signature != self._suggestion_signature:
            self._suggestion_index = 0
            self._suggestion_signature = signature
        else:
            self._suggestion_index = min(self._suggestion_index, len(items) - 1)
        self._suggestion_items = items
        self._render_command_suggestions()
        composer.suggestions_open = True

    def _render_command_suggestions(self) -> None:
        widget = self.query_one("#command-suggestions", CommandSuggestions)
        entries = [
            (str(item["label"]), f"{item['kind']}:{item['name']}")
            for item in self._suggestion_items
        ]
        widget.show_entries(entries, self._suggestion_index)
        if entries:
            widget.highlighted = self._suggestion_index

    def _hide_command_suggestions(self) -> None:
        self._suggestion_items = []
        self._suggestion_signature = ()
        self._suggestion_index = 0
        with contextlib.suppress(Exception):
            self.query_one("#command-suggestions", CommandSuggestions).hide_entries()
        with contextlib.suppress(Exception):
            self.query_one("#composer", Composer).suggestions_open = False

    def on_command_suggestion_key(self, event: CommandSuggestionKey) -> None:
        if not self._suggestion_items:
            return
        key = event.key
        if key == "escape":
            self._hide_command_suggestions()
            return
        if key in {"up", "down"}:
            step = -1 if key == "up" else 1
            self._suggestion_index = (self._suggestion_index + step) % len(self._suggestion_items)
            self._render_command_suggestions()
            return
        if key in {"enter", "tab"}:
            self._accept_command_suggestion(complete_only=key == "tab")

    def _accept_command_suggestion(self, *, complete_only: bool) -> None:
        if not (0 <= self._suggestion_index < len(self._suggestion_items)):
            return
        item = self._suggestion_items[self._suggestion_index]
        composer = self.query_one("#composer", Composer)
        self._hide_command_suggestions()
        if item["kind"] == "file":
            self._complete_file(item)
            return
        if item["kind"] == "local":
            command = item["command"]
            if complete_only or command.takes_argument:
                composer.load_text(f"/{command.name} ")
                composer.focus()
                return
            composer.clear_composer()
            composer.focus()
            asyncio.create_task(self._run_local_command(command.name))
            return
        if item["kind"] == "plugin":
            descriptor = next(
                (
                    command
                    for command in self._plugin_commands
                    if self._plugin_command_display(command) == item["name"]
                ),
                {},
            )
            takes_arguments = bool(
                descriptor.get("takesArguments", descriptor.get("takes_arguments"))
            )
            if complete_only or takes_arguments:
                composer.load_text(f"/{item['name']} ")
                composer.focus()
                return
            composer.clear_composer()
            composer.focus()
            asyncio.create_task(
                self._run_plugin_command(
                    str(item["bindingId"]), str(item["commandName"]), ""
                )
            )
            return
        prefix = "/agent " if item.get("collision") else "/"
        composer.load_text(f"{prefix}{item['name']} ")
        composer.focus()

    @work(group="attach-file", exclusive=False)
    async def _complete_file(self, item: dict[str, Any]) -> None:
        path = str(self._workspace_root / item["name"])
        if await self._stage_attachment(path):
            composer = self.query_one("#composer", Composer)
            composer.replace(f"@{item['name']} ", item["start"], item["end"])
            composer.focus()

    async def on_submitted(self, event: Submitted) -> None:
        self._hide_command_suggestions()
        text = event.text.strip()
        if not text:
            return
        if text.startswith("/"):
            await self._handle_command(text)
            return
        await self._start_prompt(text)

    async def _start_prompt(
        self, text: str, *, attachments: list[str] | None = None, from_queue: bool = False
    ) -> bool:
        if not self.connected:
            await self._notice("Not connected. Press Ctrl+R to reconnect.", "error")
            return False
        if self.session.client is not self.client:
            await self._notice(
                "This session is not attached to the current connection. "
                "Press Ctrl+N for a new session or reload it after reconnecting.",
                "warning",
            )
            self.query_one("#composer", Composer).focus()
            return False
        if self._loading_session or self._replay_target is not None:
            await self._notice(
                "Wait for the session to finish loading; the prompt was kept in the composer.",
                "warning",
            )
            self.query_one("#composer", Composer).focus()
            return False
        if self.busy:
            if len(self._queued_prompts) >= 20:
                await self._notice(
                    "The queue is full (20 follow-ups). Edit or remove an item.", "warning"
                )
                return False
            self._queued_prompts.append((text, list(self._attachments)))
            self._remember_prompt(text)
            self._attachments.clear()
            self.query_one("#composer", Composer).clear_composer()
            self._refresh_queue()
            return True
        attachments = list(self._attachments) if attachments is None else attachments
        try:
            blocks = build_prompt_blocks(text, attachments) if attachments else None
        except HarnessClientError as exc:
            await self._notice(exc.message, "error")
            if not from_queue:
                self.query_one("#composer", Composer).load_text(text)
            return False
        composer = self.query_one("#composer", Composer)
        if not from_queue:
            self._remember_prompt(text)
            composer.clear_composer()
        pane = self.query_one("#transcript", TranscriptPane)
        await pane.finish_message()
        display = text
        if attachments:
            names = ", ".join(item["name"] for item in attachment_summary(attachments))
            display = f"{text}\n\nattached: {names}"
        await pane.add_user(display)
        if not from_queue:
            self._attachments.clear()
            self._refresh_queue()
        self._set_busy(True)
        self.app.start_turn(self, text, blocks)  # type: ignore[attr-defined]
        return True

    def _remember_prompt(self, text: str) -> None:
        history = self._prompt_history.setdefault(self.session.session_id, [])
        if not history or history[-1] != text:
            history.append(text)
            del history[:-100]
        self.query_one("#composer", Composer).prompt_history = history

    def _save_draft(self) -> None:
        self._drafts[self.session.session_id] = (
            self.query_one("#composer", Composer).text,
            list(self._attachments),
            list(self._queued_prompts),
        )

    def _restore_draft(self) -> None:
        if self.target.location().get("kind") == "local":
            root = Path(self.session.cwd).resolve()
            if root.is_dir() and root != self._workspace_root:
                self._workspace_root = root
                self._workspace_files = []
                self._index_workspace()
        text, attachments, queue = self._drafts.get(self.session.session_id, ("", [], []))
        composer = self.query_one("#composer", Composer)
        composer.clear_composer()
        composer.load_text(text)
        composer.prompt_history = self._prompt_history.setdefault(self.session.session_id, [])
        self._attachments = list(attachments)
        self._queued_prompts = list(queue)
        self._queue_paused = bool(queue)
        self._refresh_queue()

    def _refresh_queue(self) -> None:
        self.query_one(PromptQueue).show_prompts(self._queued_prompts, paused=self._queue_paused)
        self.query_one(AttachmentBar).show_files(attachment_summary(self._attachments))

    def on_attachment_removed(self, event: AttachmentRemoved) -> None:
        if 0 <= event.index < len(self._attachments):
            self._attachments.pop(event.index)
            self._refresh_queue()
            self.query_one("#composer", Composer).focus()

    async def on_queue_action(self, event: QueueAction) -> None:
        if event.action == "resume":
            self._queue_paused = False
            self._refresh_queue()
            await self.send_next_queued()
        elif 0 <= event.index < len(self._queued_prompts):
            if event.action == "edit":
                composer = self.query_one("#composer", Composer)
                if composer.text.strip() or self._attachments:
                    await self._notice(
                        "Send or clear the current draft before editing a queued message.",
                        "warning",
                    )
                    return
                text, self._attachments = self._queued_prompts.pop(event.index)
                composer.load_text(text)
                composer.focus()
            elif event.action == "remove":
                self._queued_prompts.pop(event.index)
            self._refresh_queue()

    async def send_next_queued(self) -> None:
        if (
            self not in self.app.screen_stack
            or self.busy
            or self._queue_paused
            or not self._queued_prompts
            or not self.connected
            or self._loading_session
        ):
            return
        text, attachments = self._queued_prompts[0]
        if await self._start_prompt(text, attachments=attachments, from_queue=True):
            self._queued_prompts.pop(0)
        else:
            self._queue_paused = True
        self._refresh_queue()

    async def _handle_command(self, text: str) -> None:
        body = text[1:].strip()
        if not body:
            self.action_command_palette()
            return
        name, _, rest = body.partition(" ")
        name = name.strip()
        rest = rest.strip()
        local = resolve_local_command(name)
        agent_names = {str(command.get("name", "")) for command in self._commands}
        if ":" in name:
            descriptor = self._find_plugin_command(name)
            if descriptor is None:
                await self._notice(
                    f"Unknown plugin command '/{name}'. Press Ctrl+K to see available commands.",
                    "warning",
                )
                return
            await self._run_plugin_command(
                str(descriptor.get("bindingId") or descriptor.get("binding_id")),
                str(descriptor.get("commandName") or descriptor.get("command_name")),
                rest,
            )
            return
        if name == "agent":
            if not rest:
                await self._notice("Usage: /agent <agent-command> [arguments]", "warning")
                return
            agent_name, _, arguments = rest.partition(" ")
            await self._send_agent_command(agent_name, arguments)
            return
        if local is not None:
            await self._run_local_command(local.name, rest)
            if local.name in agent_names:
                await self._notice(
                    f"Local /{local.name} ran. Use /agent {local.name} for the agent's "
                    f"'{local.name}' command.",
                    "info",
                )
            return
        if name in agent_names:
            await self._send_agent_command(name, rest)
            return
        await self._notice(
            f"Unknown command '/{name}'. Press Ctrl+K to see local and agent commands.",
            "warning",
        )
        composer = self.query_one("#composer", Composer)
        composer.load_text(text)

    async def _run_local_command(self, name: str, rest: str = "") -> None:
        composer = self.query_one("#composer", Composer)
        composer.clear_composer()
        if name == "attach":
            await self._stage_attachment(rest)
        elif name == "connect":
            await self.app.choose_agent(self)  # type: ignore[attr-defined]
        elif name == "agents":
            await self._show_subagents()
        elif name == "rename":
            await self._rename_session(rest)
        elif name == "sessions":
            self.action_show_sessions()
        elif name == "find":
            self._find_transcript(rest)
        elif name == "copy":
            response = self.query_one("#transcript", TranscriptPane).latest_response()
            if response is None:
                self.app.notify("No completed assistant response to copy")
            else:
                self.app.copy_to_clipboard(response._text)
                self.app.notify("Response copied")
        elif name == "plugins":
            self.action_show_capabilities()
            self.refresh_capabilities()
        elif name == "models":
            await self._show_models()
        elif name == "effort":
            await self._show_effort()
        elif name == "permissions":
            self._show_permissions()
        elif name == "settings":
            self._show_settings()
        elif name == "help":
            self.action_help()

    async def _rename_session(self, title: str) -> None:
        if not self.client.extension.supports("_harness/session/rename"):
            await self._notice("This agent does not support session renaming.", "warning")
            return
        if not title.strip():

            def apply(values: dict[str, Any] | None) -> None:
                if values:
                    asyncio.create_task(self._rename_session(str(values["title"])))

            self.app.push_screen(
                FormDialog(
                    "Rename session",
                    "Choose a name for this conversation.",
                    [
                        {
                            "name": "title",
                            "title": "Session name",
                            "kind": "string",
                            "required": True,
                            "default": self._session_title or "",
                        }
                    ],
                ),
                apply,
            )
            return
        try:
            renamed = await self.session.rename(title.strip())
        except (HarnessClientError, ValueError) as exc:
            await self._notice(str(exc), "error")
            return
        self._session_title = renamed
        self._update_chrome()
        self.refresh_sessions()
        await self._notice(f"Session renamed to {self._session_title}.", "success")

    async def _stage_attachment(self, argument: str) -> bool:
        """Stage or clear local files for the next prompt (explicit user action only)."""
        argument = argument.strip()
        if argument.lower() in {"", "list"}:
            if not self._attachments:
                await self._notice("No files staged. Use /attach <path>.", "info")
            else:
                await self._notice(
                    "Staged: " + ", ".join(Path(item).name for item in self._attachments),
                    "info",
                )
            return False
        if argument.lower() in {"clear", "none"}:
            count = len(self._attachments)
            self._attachments.clear()
            self._refresh_queue()
            await self._notice(f"Cleared {count} staged attachment(s).", "info")
            return False
        capabilities = getattr(
            getattr(self.client.initialized, "agent_capabilities", None),
            "prompt_capabilities",
            None,
        )
        if not bool(getattr(capabilities, "embedded_context", False)):
            await self._notice(
                "This agent did not advertise embedded-context prompt support; "
                "attachments cannot be sent. Paste the relevant text instead.",
                "warning",
            )
            return False
        try:
            candidate = Path(argument).expanduser()
            path = validate_attachment(
                candidate if candidate.is_absolute() else self._workspace_root / candidate
            )
        except HarnessClientError as exc:
            await self._notice(exc.message, "error")
            return False
        if str(path) in self._attachments:
            await self._notice(f"{path.name} is already staged.", "info")
        else:
            self._attachments.append(str(path))
            await self._notice(
                f"Staged {path.name}; {len(self._attachments)} file(s) will be attached "
                "to the next message. Use /attach clear to remove them.",
                "success",
            )
        composer = self.query_one("#composer", Composer)
        self._refresh_queue()
        composer.focus()
        return True

    @staticmethod
    def _plugin_command_display(command: dict[str, Any]) -> str:
        binding = str(command.get("bindingId") or command.get("binding_id") or "")
        name = str(command.get("commandName") or command.get("command_name") or "")
        return f"{binding}:{name}" if binding else name

    def _find_plugin_command(self, display: str) -> dict[str, Any] | None:
        # A binding id may itself contain colons; the displayed name ends at the final one.
        for command in self._plugin_commands:
            if self._plugin_command_display(command) == display:
                return command
        return None

    async def _run_plugin_command(
        self, binding_id: str, command_name: str, arguments: str
    ) -> None:
        """Dispatch a user-only command without creating a model turn or tool call."""
        if not self.client.extension.supports("_harness/commands/invoke"):
            await self._notice(
                "This agent does not negotiate Harness plugin commands.", "warning"
            )
            return
        if self.busy:
            await self._notice("Stop the running turn before running a plugin command.", "warning")
            return
        session = self.session
        composer = self.query_one("#composer", Composer)
        composer.clear_composer()
        request_id = f"ui-{uuid.uuid4().hex}"
        try:
            response = await session.invoke_command(
                binding_id,
                command_name,
                text=arguments,
                expected_revision=self._plugin_revision,
                request_id=request_id,
            )
        except (ExtensionUnavailable, HarnessClientError) as exc:
            await self._notice(f"Command failed: {exc}", "error")
            return
        if self.session is not session:
            # The user switched sessions while the handler ran; stale output must not be
            # presented in, or start a turn for, a different conversation.
            return
        result = response.result
        fallback = (
            result.presentation.fallback
            if result.presentation is not None
            else "Command completed."
        )
        await self.query_one("#transcript", TranscriptPane).add_command(
            {
                "bindingId": response.binding_id,
                "commandName": response.command_name,
                "fallback": fallback,
                "presentation": (
                    result.presentation.to_wire()
                    if result.presentation is not None
                    else None
                ),
            }
        )
        action = result.conversation
        if action is None:
            return
        key = (session.session_id, request_id)
        if key in self._consumed_command_actions:
            return
        self._consumed_command_actions.add(key)
        if len(self._consumed_command_actions) > 200:
            self._consumed_command_actions.clear()
            self._consumed_command_actions.add(key)
        if getattr(action, "type", "") != "start_turn":
            await self._notice(
                f"Command '/{binding_id}:{command_name}' requested an unsupported "
                "conversation action.",
                "warning",
            )
            return
        await self._start_prompt(
            action.prompt,
            attachments=[],
            from_queue=True,
        )

    async def _send_agent_command(self, name: str, arguments: str) -> None:
        if not name:
            return
        prompt = f"/{name}" + (f" {arguments}" if arguments else "")
        composer = self.query_one("#composer", Composer)
        composer.clear_composer()
        await self._start_prompt(prompt)

    # --------------------------------------------------------------- capability

    def on_capability_refresh_requested(self, event: CapabilityRefreshRequested) -> None:
        self.refresh_capabilities()

    def on_capability_add_requested(self, event: CapabilityAddRequested) -> None:
        asyncio.create_task(self._choose_plugin_to_add())

    def on_binding_toggle_requested(self, event: BindingToggleRequested) -> None:
        self._toggle_binding(event.binding_id, event.enabled)

    def on_binding_edit_requested(self, event: BindingEditRequested) -> None:
        self._edit_binding(event.binding_id)

    def on_config_option_requested(self, event: ConfigOptionRequested) -> None:
        self._change_standard_option(event.option_id)

    def on_config_option_step_requested(self, event: ConfigOptionStepRequested) -> None:
        self._step_standard_option(event.option_id, event.direction)

    @work(group="capability", exclusive=True)
    async def _toggle_binding(self, binding_id: str, enabled: bool) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        summary = panel.binding(binding_id)
        if summary is None:
            return
        if not summary["canToggle"]:
            panel.show_message(f"{summary['name']} cannot be toggled from this client.")
            return
        try:
            snapshot = await self.session.update_configuration(
                [{"type": "enable", "bindingId": binding_id, "enabled": enabled}]
            )
        except HarnessClientError as exc:
            panel.show_message(exc.message)
            if "REVISION_CONFLICT" in exc.message or "revision" in exc.message.lower():
                await self._refresh_capabilities()
            return
        self._apply_snapshot(snapshot)
        await self._refresh_capabilities()
        state = "enabled" if enabled else "disabled"
        panel.show_message(
            f"{summary['name']} {state} for the next turn."
            if summary["changeMode"] == "next_turn"
            else f"{summary['name']} {state}; change takes effect with a new session."
        )

    def _edit_binding(self, binding_id: str) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        summary = panel.binding(binding_id)
        if summary is None:
            return
        schema = summary.get("configSchema") or {}
        fields = build_form_fields(schema, summary.get("config") or {})
        editable = set(summary.get("editableFields") or ())
        if editable:
            fields = [field for field in fields if field["name"] in editable]
        if not fields:
            panel.show_message(f"{summary['name']} has no editable fields.")
            return

        async def apply(fields_values: dict[str, Any] | None) -> None:
            if not fields_values:
                return
            self._apply_binding_config(binding_id, fields_values)

        subtitle = (
            "Field replacements apply at the next turn boundary when the binding mode allows it."
        )
        if summary["requires"]:
            subtitle += " Requires: " + ", ".join(summary["requires"]) + "."
        if summary["conflicts"]:
            subtitle += " Conflicts: " + ", ".join(summary["conflicts"]) + "."
        self.app.push_screen(
            FormDialog(
                f"Configure {summary['name']}",
                subtitle,
                fields,
                accept_label="Save configuration",
                current=summary.get("config") or {},
            ),
            lambda result: apply(result),
        )

    async def _all_capability_pages(self) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            listing = await self.session.list_capabilities(cursor=cursor, limit=100)
            items.extend(listing.bindings)
            cursor = listing.next_cursor
            if not cursor or cursor in seen:
                return items
            seen.add(cursor)

    async def _all_plugin_pages(self) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            listing = await self.session.list_plugins(cursor=cursor, limit=100)
            items.extend(listing.plugins)
            cursor = listing.next_cursor
            if not cursor or cursor in seen:
                return items
            seen.add(cursor)

    async def _choose_plugin_to_add(self) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        if not (
            self.client.extension.local_binding_creation
            and self.client.extension.supports("_harness/plugins/list")
        ):
            panel.show_message(
                "This host does not advertise local binding creation; only offered bindings "
                "can be enabled here."
            )
            return
        try:
            inventory = await self._all_plugin_pages()
        except HarnessClientError as exc:
            panel.show_message(exc.message)
            return
        available = [
            plugin
            for plugin in inventory
            if plugin.runtime_compatible and plugin.change_mode == "next_turn"
        ]
        if not available:
            panel.show_message(
                "No runtime-compatible next-turn plugins are available to add. "
                "New-session plugins can only be chosen when the session is created."
            )
            return
        choices = [
            {
                "value": plugin.plugin_id,
                "name": f"{plugin.plugin_id}  {plugin.version}".strip(),
                "description": " · ".join(
                    item
                    for item in (
                        plugin.description or "No description.",
                        f"activation: {plugin.change_mode}",
                        f"requirements: {', '.join(plugin.requirements)}"
                        if plugin.requirements
                        else "",
                        plugin.constraint or "",
                    )
                    if item
                ),
            }
            for plugin in available
        ]

        def start_plugin(plugin_id: str | None) -> None:
            if isinstance(plugin_id, str) and plugin_id:
                asyncio.create_task(self._plugin_add_form(plugin_id))

        self.app.push_screen(
            ChoiceDialog(
                "Add a local plugin",
                "Only plugins the host advertises for local binding creation are listed.",
                choices,
            ),
            start_plugin,
        )

    async def _plugin_add_form(self, plugin_id: str) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        try:
            inventory = await self._all_plugin_pages()
        except HarnessClientError as exc:
            panel.show_message(exc.message)
            return
        plugin = next((item for item in inventory if item.plugin_id == plugin_id), None)
        if plugin is None:
            panel.show_message(f"Plugin {plugin_id} is no longer available.")
            return
        fields = [
            {
                "name": "__binding_id__",
                "title": "Binding id",
                "kind": "text",
                "required": True,
                "description": "A stable name for this configured instance.",
            },
            {
                "name": "__enabled__",
                "title": "Enable for the next turn",
                "kind": "boolean",
                "required": False,
                "default": True,
            },
            *build_form_fields(plugin.config_schema, {}),
        ]

        async def apply(values: dict[str, Any] | None) -> None:
            if not values:
                return
            binding_id = str(values.pop("__binding_id__", "")).strip()
            enabled = bool(values.pop("__enabled__", True))
            if not binding_id:
                panel.show_message("A binding id is required.")
                return
            # @work returns a Worker, not an awaitable; let it run in its group.
            self._add_capability(
                plugin.plugin_id,
                binding_id,
                enabled,
                values,
                plugin.change_mode,
            )

        subtitle = f"Create a new binding from {plugin.plugin_id}."
        if plugin.requirements:
            subtitle += " Requires: " + ", ".join(plugin.requirements) + "."
        if plugin.constraint:
            subtitle += " " + plugin.constraint
        self.app.push_screen(
            FormDialog(
                "Add a local plugin",
                subtitle,
                fields,
                accept_label="Add plugin",
            ),
            apply,
        )

    @work(group="capability", exclusive=True)
    async def _add_capability(
        self,
        plugin_id: str,
        binding_id: str,
        enabled: bool,
        config: dict[str, Any],
        change_mode: str = "next_turn",
    ) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        try:
            snapshot = await self.session.configuration()
            updated = await self.session.update_configuration(
                [
                    {
                        "type": "add",
                        "bindingId": binding_id,
                        "pluginId": plugin_id,
                        "config": config,
                        "enabled": enabled,
                    }
                ],
                expected_revision=snapshot.selected_revision,
                request_id=f"ui-add-{uuid.uuid4().hex}",
            )
        except HarnessClientError as exc:
            panel.show_message(exc.message)
            return
        self._apply_snapshot(updated)
        await self._refresh_capabilities()
        timing = "with a new session" if change_mode == "new_session" else "for the next turn"
        state = "enabled" if enabled else "disabled"
        panel.show_message(f"Added {binding_id} from {plugin_id} ({state}) {timing}.")

    @work(group="capability", exclusive=True)
    async def _apply_binding_config(self, binding_id: str, values: dict[str, Any]) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        try:
            snapshot = await self.session.update_configuration(
                [{"type": "configure", "bindingId": binding_id, "fields": values}]
            )
        except HarnessClientError as exc:
            panel.show_message(exc.message)
            return
        self._apply_snapshot(snapshot)
        await self._refresh_capabilities()
        panel.show_message(f"Configuration saved for {binding_id}.")

    def _step_standard_option(self, option_id: str, direction: int) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        option = panel.option(option_id) or {}
        if option.get("type") == "boolean":
            self._change_standard_option(option_id)
            return
        choices = option_values(option)
        if not choices:
            panel.show_message(f"{option.get('name', option_id)} has no selectable values.")
            return
        values = [choice["value"] for choice in choices]
        current = option.get("currentValue")
        value = stepped_value(values, None if current is None else str(current), direction)
        if value is not None:
            self._set_standard_value(option_id, value)

    @work(group="capability", exclusive=True)
    async def _change_standard_option(self, option_id: str) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        option = panel.option(option_id) or {}
        if option.get("type") == "boolean":
            try:
                options = await self.session.set_config_option(
                    option_id, not bool(option.get("currentValue"))
                )
            except HarnessClientError as exc:
                panel.show_message(exc.message)
                return
            panel.set_standard_options(list(options))
            self._update_chrome()
            return
        choices = option_values(option)
        if not choices:
            panel.show_message(f"{option.get('name', option_id)} has no selectable values.")
            return
        current = option.get("currentValue")

        def apply(value: str | None) -> None:
            if value is not None:
                self._set_standard_value(option_id, value)

        self.app.push_screen(
            ChoiceDialog(
                str(option.get("name") or option_id),
                str(option.get("description") or ""),
                choices,
                current=str(current) if current is not None else None,
            ),
            apply,
        )

    @work(group="capability", exclusive=True)
    async def _set_standard_value(self, option_id: str, value: str) -> None:
        panel = self.query_one("#side-panel", SidePanel).capabilities
        try:
            options = await self.session.set_config_option(option_id, value)
        except HarnessClientError as exc:
            panel.show_message(exc.message)
            return
        panel.set_standard_options(list(options))
        self._update_chrome()

    # ----------------------------------------------------------------- sessions

    def on_session_load_requested(self, event: SessionLoadRequested) -> None:
        self._load_session_worker(event.session_id)

    @work(group="session", exclusive=True)
    async def _load_session_worker(self, session_id: str) -> None:
        await self._load_session(session_id, cwd=self._session_cwd(session_id))

    def _session_cwd(self, session_id: str) -> str:
        panel = self.query_one("#side-panel", SidePanel).sessions
        cwd = panel.cwd_for(session_id)
        return cwd or self.target.cwd

    async def _load_session(self, session_id: str, *, cwd: str, initial: bool = False) -> None:
        if self.busy and not initial:
            await self._notice("Stop the running turn before switching sessions.", "warning")
            return
        if self._loading_session or self._replay_target is not None:
            await self._notice("Wait for the current load to finish.", "warning")
            return
        if not self.client.supports_load:
            await self._notice(
                "This agent does not advertise session/load; the conversation cannot be reopened.",
                "warning",
            )
            if initial:
                await self._new_session(replace_session=True)
            return
        pane = self.query_one("#transcript", TranscriptPane)
        self._reset_activity()
        self._loading_session = True
        self._replay_target = session_id
        pane.begin_replay()
        try:
            loaded = await self.client.load_session(session_id, cwd)
            await self.client.drain_events()
        except HarnessClientError as exc:
            await pane.end_replay()
            self._replay_target = None
            self._loading_session = False
            await self._notice(f"Could not load session {session_id}: {exc.message}", "error")
            if initial:
                await self._new_session(replace_session=True)
            return
        await pane.end_replay()
        self._replay_target = None
        self._loading_session = False
        self._save_draft()
        self.session = loaded
        self._restore_draft()
        self.app.session = loaded  # type: ignore[attr-defined]
        self._update_chrome()
        await self._notice(f"Loaded session {session_id}.", "success")
        self.query_one("#composer", Composer).focus()
        self._refresh_panels()

    def on_session_resume_requested(self, event: SessionResumeRequested) -> None:
        self._resume_session_worker(event.session_id)

    @work(group="session", exclusive=True)
    async def _resume_session_worker(self, session_id: str) -> None:
        await self._resume_session(session_id)

    async def _resume_session(self, session_id: str) -> None:
        # This terminal does not retain a verified complete transcript cache.
        # CONCEPT §14.2 therefore requires authoritative session/load; resume is
        # never used as an unverified shortcut.
        if not self.client.has_session_capability("resume"):
            await self._notice(
                "This agent does not advertise session/resume and this terminal has no "
                "verified cache; authoritative history can be loaded when available.",
                "warning",
            )
            return
        if self.client.supports_load:
            await self._load_session(session_id, cwd=self._session_cwd(session_id))
            return
        await self._notice(
            "session/load is not advertised and this terminal has no verified cache; "
            "session/resume would discard authoritative history, so it was not used.",
            "warning",
        )

    def on_session_new_requested(self, event: SessionNewRequested) -> None:
        self._new_session_worker()

    @work(group="session", exclusive=True)
    async def _new_session_worker(self) -> None:
        await self._new_session()

    async def _new_session(self, *, replace_session: bool = False) -> None:
        if self.busy:
            await self._notice("Stop the running turn before creating a session.", "warning")
            return
        if self._loading_session or self._replay_target is not None:
            await self._notice("Wait for the current session load to finish.", "warning")
            return
        try:
            session = await self.client.new_session(self.target.cwd)
        except HarnessClientError as exc:
            await self._notice(f"Could not create a session: {exc.message}", "error")
            return
        self._save_draft()
        self.session = session
        self._restore_draft()
        self.app.session = session  # type: ignore[attr-defined]
        self._reset_activity()
        await self.query_one("#transcript", TranscriptPane).clear_transcript()
        self._update_chrome()
        await self._notice(f"Started session {session.session_id}.", "success")
        self.query_one("#composer", Composer).focus()
        self._refresh_panels()

    def on_session_delete_requested(self, event: SessionDeleteRequested) -> None:
        self._confirm_delete(event.session_id)

    def _confirm_delete(self, session_id: str) -> None:
        def done(result: bool | None) -> None:
            if result:
                self._delete_session(session_id)

        self.app.push_screen(
            ConfirmDialog(
                "Delete session",
                f"Delete session {session_id}? Its conversation and owned state are removed. "
                "This cannot be undone.",
                confirm_label="Delete session",
            ),
            done,
        )

    @work(group="session", exclusive=True)
    async def _delete_session(self, session_id: str) -> None:
        if not self.client.has_session_capability("delete"):
            await self._notice("This agent does not advertise session/delete.", "warning")
            return
        if self.busy and session_id == self.session.session_id:
            await self._notice("Stop the running turn before deleting its session.", "warning")
            return
        if self._loading_session or self._replay_target is not None:
            await self._notice("Wait for the current session load to finish.", "warning")
            return
        try:
            await self.client.delete_session(session_id)
        except HarnessClientError as exc:
            await self._notice(f"Delete failed: {exc.message}", "error")
            return
        await self._notice(f"Deleted session {session_id}.", "success")
        if session_id == self.session.session_id:
            await self._new_session(replace_session=True)
        self.refresh_sessions()

    def on_session_close_requested(self, event: SessionCloseRequested) -> None:
        self._close_session(event.session_id)

    @work(group="session", exclusive=True)
    async def _close_session(self, session_id: str) -> None:
        if not self.client.has_session_capability("close"):
            await self._notice("This agent does not advertise session/close.", "warning")
            return
        if self._loading_session or self._replay_target is not None:
            await self._notice("Wait for the current session load to finish.", "warning")
            return
        try:
            await self.client.close_session(session_id)
        except HarnessClientError as exc:
            await self._notice(f"Close failed: {exc.message}", "error")
            return
        await self._notice(
            f"Closed session {session_id}; history is retained by the agent.", "success"
        )
        if session_id == self.session.session_id:
            await self._new_session(replace_session=True)
        self.refresh_sessions()

    def on_session_refresh_requested(self, event: SessionRefreshRequested) -> None:
        self.refresh_sessions()

    def on_session_more_requested(self, event: SessionMoreRequested) -> None:
        self._load_more_sessions()

    @work(group="panels", exclusive=True)
    async def _load_more_sessions(self) -> None:
        await self._refresh_sessions(load_more=True)

    # ------------------------------------------------------------------ models

    def _local_settings(self) -> LocalSettings:
        target = self.target
        args = (
            target.profile.args
            if target is not None and target.profile.command == "harness-local"
            else []
        )
        config = Path(args[1]) if len(args) == 2 and args[0] == "--config" else None
        return LocalSettings(config_file=config) if config else LocalSettings()

    async def _show_models(self) -> None:
        if self.session.client is not self.client:
            await self._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        if self.target.kind != "local" or self.target.profile.command != "harness-local":
            await self._show_model_dialog()
            return
        try:
            settings = self._local_settings()
        except (ValueError, OSError) as exc:
            await self._notice(f"Local settings are unavailable: {exc}", "error")
            return
        models = list(local_model_catalog().get(settings.provider, ()))
        if not models:
            self._open_local_model_form(settings)
            return
        if settings.model and settings.model not in models:
            models.insert(0, settings.model)
        effort = effort_label(thinking_from_settings(settings))
        current_note = (
            f"current · effort {effort}"
            if settings.model and local_thinking_support(settings.provider, settings.model)
            else "current"
        )
        choices = [
            {
                "value": model,
                "name": model,
                "description": current_note if model == settings.model else "",
            }
            for model in models
        ]
        choices.append(
            {
                "value": "__configure__",
                "name": "Configure provider or model…",
                "description": "Add a provider, paste a key, set a base URL or enter a custom model id.",
            }
        )

        def apply(value: str | None) -> None:
            if not value:
                return
            if value == "__configure__":
                self._open_local_model_form(settings)
                return
            self._select_local_model(settings, value)

        current_effort = (
            f" · effort {effort}"
            if settings.model and local_thinking_support(settings.provider, settings.model)
            else ""
        )
        self.app.push_screen(
            ChoiceDialog(
                "Models",
                f"Provider {settings.provider}{current_effort}. "
                "The selection applies to the next message.",
                choices,
                current=settings.model,
            ),
            apply,
        )

    def _open_local_model_form(self, settings: LocalSettings) -> None:
        from .local import LocalModelDialog

        self.app.push_screen(
            LocalModelDialog(settings, accept_label="Save and apply"),
            self._models_configured,
        )

    def _select_local_model(self, settings: LocalSettings, model: str) -> None:
        """Model choice first; prompt for effort when that model can reason.

        Canceling the effort step cancels the whole change, matching Codex.
        """
        support = local_thinking_support(settings.provider, model)
        if support is None:
            # A model outside the catalog has unknown support: keep a manually
            # configured thinking level instead of clearing it.
            known = model in local_model_catalog().get(settings.provider, ())
            asyncio.create_task(
                self._save_model_selection(settings, model, None, touch_thinking=known)
            )
            return
        current = thinking_from_settings(settings) if model == settings.model else None
        self._choose_effort(
            settings,
            model,
            current=current,
            on_select=lambda effort: asyncio.create_task(
                self._save_model_selection(settings, model, effort)
            ),
        )

    def _choose_effort(
        self,
        settings: LocalSettings,
        model: str,
        *,
        current: str | None,
        on_select: Any,
    ) -> None:
        self.app.push_screen(
            ChoiceDialog(
                "Reasoning effort",
                f"{settings.provider}:{model}. Applies to the next message.",
                effort_choices(settings.provider, model),
                current=current or EFFORT_DEFAULT,
            ),
            lambda value: on_select(value) if value else None,
        )

    async def _save_model_selection(
        self,
        settings: LocalSettings,
        model: str,
        effort: str | None,
        *,
        touch_thinking: bool = True,
    ) -> None:
        try:
            configured = settings.model_copy(update={"model": model})
            if touch_thinking:
                configured = with_thinking(configured, effort)
            path = configured.save(settings.config_file)
        except (ValueError, OSError) as exc:
            await self._notice(f"Could not save the model selection: {exc}", "error")
            return
        await self._apply_model_settings(
            LocalModelResult(
                path=path,
                provider=settings.provider,
                model=model,
                api_key_ref=None,
                api_key_changed=False,
                effort=effort,
            )
        )

    async def _show_effort(self) -> None:
        if self.session.client is not self.client:
            await self._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        if self.target.kind != "local" or self.target.profile.command != "harness-local":
            await self._notice(
                "Reasoning effort is only available for local models.",
                "warning",
            )
            return
        try:
            settings = self._local_settings()
        except (ValueError, OSError) as exc:
            await self._notice(f"Local settings are unavailable: {exc}", "error")
            return
        if not settings.model:
            await self._notice("Configure a local model before setting reasoning effort.", "error")
            return
        support = local_thinking_support(settings.provider, settings.model)
        if support is None:
            await self._notice(
                f"{settings.provider}:{settings.model} does not advertise reasoning effort.",
                "warning",
            )
            return
        option = effort_option(self.session.config_options)
        current = (
            str(option.get("currentValue"))
            if option is not None
            else (thinking_from_settings(settings) or EFFORT_DEFAULT)
        )
        self._choose_effort(
            settings,
            settings.model,
            current=current,
            on_select=lambda effort: asyncio.create_task(self._set_local_effort(settings, effort)),
        )

    async def _set_local_effort(self, settings: LocalSettings, effort: str) -> None:
        try:
            configured = with_thinking(settings, effort)
            path = configured.save(settings.config_file)
        except (ValueError, OSError) as exc:
            await self._notice(f"Could not save the reasoning effort: {exc}", "error")
            return
        await self._apply_model_settings(
            LocalModelResult(
                path=path,
                provider=settings.provider,
                model=settings.model,
                api_key_ref=None,
                api_key_changed=False,
                effort=effort,
            )
        )

    def _models_configured(self, result: Any) -> None:
        if result is not None:
            asyncio.create_task(self._apply_model_settings(result))

    async def _apply_model_settings(self, result: Any) -> None:
        from .local import LocalModelResult

        if not isinstance(result, LocalModelResult):
            return
        try:
            old = self._local_settings()
        except (ValueError, OSError):
            old = None
        wanted = f"{result.provider}:{result.model}"
        option = model_option(self.session.config_options)
        values = {str(item["value"]) for item in option_values(option)} if option else set()
        restart = (
            old is None
            or result.provider != old.provider
            or result.api_key_changed
            or option is None
            or wanted not in values
        )
        if not restart:
            option_id = str(option.get("id") or "model")
            try:
                options = await self.session.set_config_option(option_id, wanted)
            except HarnessClientError as exc:
                await self._notice(f"Model change failed: {exc.message}", "error")
                return
            self.session.remember(options)
            if result.effort is not None:
                if effort_option(options) is not None:
                    try:
                        options = await self.session.set_config_option("effort", result.effort)
                    except HarnessClientError as exc:
                        await self._notice(
                            f"Reasoning effort change failed: {exc.message}", "error"
                        )
                        return
                    self.session.remember(options)
                elif result.effort != EFFORT_DEFAULT:
                    # The new model may not have advertised effort until the switch
                    # landed; restart so the saved setting is read at startup.
                    restart = True
            if not restart:
                self._update_chrome()
                suffix = (
                    f" · effort {effort_label(result.effort)}" if result.effort is not None else ""
                )
                await self._notice(
                    f"Model: {wanted}{suffix}",
                    "success",
                )
                return
        try:
            target = local_target(
                result.path,
                api_key_ref=result.api_key_ref,
                cwd=self.target.cwd,
            )
        except HarnessClientError as exc:
            await self._notice(
                f"Saved the configuration, but it cannot be used yet: {exc.message}", "error"
            )
            return
        self.app.target = target  # type: ignore[attr-defined]
        self.target = target
        await self._notice(
            "Restarting harness-local with the saved provider configuration; "
            "reloading this session afterwards.",
            "warning",
        )
        await self._reconnect()

    async def _show_subagents(self) -> None:
        if self.client.extension.supports("_harness/capabilities/list") and not self._bindings_raw:
            await self._refresh_capabilities()
        lines = ["Read-only overview. Configure subagents from `/plugins`.", ""]
        summaries = [
            summary
            for raw in self._bindings_raw
            for summary in (binding_summary(raw, None),)
            if "subagent"
            in f"{summary['name']} {summary['pluginId']} {summary['description']}".lower()
        ]
        if not self.client.extension.supports("_harness/capabilities/list"):
            lines.append(
                "This connection does not advertise the plugin catalog, "
                "so configured subagents cannot be inspected."
            )
        elif not summaries:
            lines.append(
                "No subagent binding is configured for this session. "
                "Add the Sub-agents plugin from `/plugins`."
            )
        else:
            for summary in summaries:
                state = "on" if summary["enabled"] else "off"
                lines.append(f"### {summary['name']} · {state}")
                lines.append(f"- id: `{summary['bindingId']}`")
                if summary["pluginId"]:
                    lines.append(f"- plugin: `{summary['pluginId']}`")
                lines.append(f"- change: {summary['modeLabel']}")
                config = summary.get("config") or {}
                if config:
                    lines.append("- configuration:")
                    for key, value in list(config.items())[:12]:
                        lines.append(f"  - `{key}`: {value}")
                if summary["requires"]:
                    lines.append(f"- requires: {', '.join(summary['requires'])}")
                lines.append("")
        activity = self.query_one("#side-panel", SidePanel).activity.subagent_lines()
        lines.append("### Recent delegated activity")
        if activity:
            lines.extend(f"- {line}" for line in activity)
        else:
            lines.append("No delegated work has been reported in this session.")
        self.app.push_screen(InfoDialog("Subagents", "\n".join(lines)))

    async def _show_model_dialog(self) -> None:
        if self.session.client is not self.client:
            await self._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        option = model_option(self.session.config_options)
        if option:
            values = option_values(option)
            if values:

                def apply(value: str | None) -> None:
                    if value is not None:
                        self._set_standard_value(option.get("id"), value)

                reasoning = effort_option(self.session.config_options)
                subtitle = "Model selection is a standard ACP session config option."
                if reasoning:
                    subtitle = (
                        "Model selection is a standard ACP session config option. "
                        f"Current effort: {config_option_value(reasoning)}."
                    )
                self.app.push_screen(
                    ChoiceDialog(
                        "Model",
                        subtitle,
                        values,
                        current=str(option.get("currentValue")),
                    ),
                    lambda value: apply(value),
                )
                return
        modes = (self.session.modes or {}).get("availableModes") or []
        if modes:
            choices = [
                {
                    "value": str(mode.get("id", "")),
                    "name": str(mode.get("name", "")),
                    "description": str(mode.get("description") or ""),
                }
                for mode in modes
            ]

            def apply_mode(value: str | None) -> None:
                if value:
                    self._set_mode(value)

            self.app.push_screen(
                ChoiceDialog(
                    "Mode",
                    "The agent advertises session modes instead of a model option.",
                    choices,
                    current=self.session.current_mode_id,
                ),
                lambda value: apply_mode(value),
            )
            return
        await self._notice(
            "This agent advertises neither a model config option nor session modes.", "warning"
        )

    @work(group="model", exclusive=True)
    async def _set_mode(self, mode_id: str) -> None:
        try:
            await self.session.set_mode(mode_id)
        except HarnessClientError as exc:
            await self._notice(f"Mode change failed: {exc.message}", "error")
            return
        self._update_chrome()
        await self._notice(f"Mode set to {mode_id}.", "success")

    # ----------------------------------------------------------------- actions

    def action_show_capabilities(self) -> None:
        self._show_panel("capabilities")

    def action_show_sessions(self) -> None:
        self._show_panel("sessions")
        self.refresh_sessions()

    def action_show_activity(self) -> None:
        self._show_panel("activity")

    def _show_panel(self, tab: str) -> None:
        panel = self.query_one("#side-panel", SidePanel)
        panel.display = True
        self._panel_forced = True
        # TabbedContent resolves its private ContentTabs by immediate-child
        # lookup. Assigning ``active`` before compose has mounted it raises
        # NoMatches, so wait until the show/refresh cycle has run.
        self.call_after_refresh(panel.activate, tab)

    def action_toggle_panel(self) -> None:
        panel = self.query_one("#side-panel", SidePanel)
        panel.display = not panel.display
        self._panel_forced = panel.display

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
                    asyncio.create_task(self._run_local_command(command.name))
            elif kind == "plugin":
                descriptor = self._find_plugin_command(name)
                if descriptor is not None:
                    composer = self.query_one("#composer", Composer)
                    takes_arguments = bool(
                        descriptor.get("takesArguments")
                        or descriptor.get("takes_arguments")
                    )
                    if takes_arguments:
                        composer.load_text(f"/{name} ")
                        composer.focus()
                    else:
                        asyncio.create_task(
                            self._run_plugin_command(
                                str(
                                    descriptor.get("bindingId")
                                    or descriptor.get("binding_id")
                                ),
                                str(
                                    descriptor.get("commandName")
                                    or descriptor.get("command_name")
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
                "display": self._plugin_command_display(command),
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
            f"- `/{self._plugin_command_display(command)}` — "
            f"{command.get('description', '')}"
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
Submitting during a running turn queues a follow-up with its attachments. Use the queue's
Edit and Remove controls to revise pending messages. Cancellation or errors pause the queue.
Drafts, prompt history, attachments, and queues stay with their session while this app is open.
Use `/` in the sessions panel to filter titles and workspaces; `Esc` clears the filter.

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
            self.app.apply_settings(self.settings)  # type: ignore[attr-defined]
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
                self.app.apply_settings(settings)  # type: ignore[attr-defined]
                self._apply_settings()

        self.app.push_screen(
            SettingsDialog(
                self.settings,
                can_persist=bool(self.app.profiles.sources),  # type: ignore[attr-defined]
            ),
            apply,
        )

    def action_interrupt_or_focus(self) -> None:
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
        composer = self.query_one("#composer", Composer)
        if not composer.text.strip():
            composer.focus()
            return
        composer.action_submit()

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
        self._refresh_queue()
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
            new_client = self.app.make_client()  # type: ignore[attr-defined]
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
        self.app.client = new_client  # type: ignore[attr-defined]
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
            self.app.session = loaded  # type: ignore[attr-defined]
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

    def action_quit_app(self) -> None:
        self.app.action_quit_app()  # type: ignore[attr-defined]
