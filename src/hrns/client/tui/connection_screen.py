"""Searchable connection picker."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, ListItem, ListView, Static

from hrns.config import LocalSettings, thinking_from_settings

from ..catalog import AgentDeployment
from ..errors import HrnsClientError
from ..launch import ConnectionTarget, catalog_for, default_remote_cwd, local_cwd
from ..local import is_local_hrns, local_target
from ..profiles import Profile, Profiles, default_profiles_path
from .choice_keys import handle_choice_arrow
from .dialogs import FormDialog
from .local import (
    CustomModelDialog,
    CustomModelRequest,
    ModelPick,
    ModelPickerDialog,
    effort_label,
    save_custom_model,
)
from .providers import ProvidersResult
from .screen_base import _HrnsScreen


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


class ConnectScreen(_HrnsScreen):
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
            "l configure local providers · n add target · / filter · r refresh · q quit",
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
        theme = self.app.current_theme
        text = Text()
        text.append("hrns", style=f"bold {theme.accent or theme.primary}")
        text.append("  a terminal for ACP agents", style=theme.variables.get("text-muted", "dim"))
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
            if settings is None or not settings.model:
                self.entries.append(
                    PickEntry(
                        kind="configure_local",
                        label="Configure local providers…",
                        detail=(
                            "Add a provider and its key, then pick a model with /models "
                            "once a session is open."
                        ),
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
            if settings is not None and settings.model:
                self.entries.append(
                    PickEntry(
                        kind="configure_local",
                        label="Change local providers…",
                        detail="Edit the configured providers, their endpoints and their keys.",
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
            except HrnsClientError as exc:
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
        except HrnsClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        self._connect_worker(target)

    def action_configure_local(self) -> None:
        from .providers import ProvidersDialog

        if self._connecting:
            return
        try:
            target = self.initial_target
            args = target.profile.args if target and is_local_hrns(target.profile) else []
            config = Path(args[1]) if len(args) == 2 and args[0] == "--config" else None
            settings = LocalSettings(config_file=config) if config else LocalSettings()
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        self.app.push_screen(ProvidersDialog(settings), self._providers_saved)

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
        except HrnsClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        self._connect_worker(target)

    def _providers_saved(self, result: ProvidersResult | None) -> None:
        """Providers are saved; a model is still needed before the runtime can start."""
        if result is None:
            return
        try:
            settings = LocalSettings(config_file=result.path)
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        if not settings.provider or not settings.model:
            self.app.push_screen(
                ModelPickerDialog(settings, subtitle="Pick the model to start with."),
                lambda pick: self._local_model_picked(result.path, pick),
            )
            return
        self._connect_local(result.path)

    def _local_model_picked(self, path: Path, pick: ModelPick | CustomModelRequest | None) -> None:
        if isinstance(pick, CustomModelRequest):
            self._add_local_custom_model(path, pick)
            return
        if pick is None:
            self._status.update("Providers saved · no model chosen yet")
            return
        try:
            settings = LocalSettings(config_file=path)
            settings.model_copy(update={"provider": pick.provider, "model": pick.model}).save(path)
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        self._connect_local(path)

    def _add_local_custom_model(self, path: Path, request: CustomModelRequest) -> None:
        """Remember an id the catalog does not list, then pick it like any other."""
        try:
            settings = LocalSettings(config_file=path)
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        provider_id = settings.provider_id(request.provider)

        def apply(values: Any) -> None:
            if not isinstance(values, dict):
                return
            model = str(values.get("model") or "").strip()
            if not model:
                return
            try:
                saved, _updated = save_custom_model(settings, request.provider, model)
            except (ValueError, OSError) as exc:
                self._status.update(f"error · {exc}")
                return
            self._local_model_picked(saved, ModelPick(request.provider, model))

        self.app.push_screen(CustomModelDialog(request.provider, provider_id, request.model), apply)

    def _connect_local(self, path: Path) -> None:
        try:
            target = local_target(path)
        except HrnsClientError as exc:
            self._status.update(f"Saved configuration · {exc.message}")
            return
        self._connect_worker(target)

    def _target_for(self, entry: PickEntry) -> ConnectionTarget:
        profile = entry.profile
        if profile is None:
            raise HrnsClientError("This entry has no connection profile.")
        if entry.kind == "local" or (profile.transport == "stdio" and entry.kind != "agent"):
            if not profile.command:
                raise HrnsClientError(f"Profile '{profile.name}' has no command.")
            return ConnectionTarget(
                name=profile.name,
                kind="local",
                profile=profile,
                agent=None,
                cwd=local_cwd(profile),
            )
        deployment = entry.deployment
        if deployment is None:
            raise HrnsClientError("Select a deployed agent first.")
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
        except HrnsClientError as exc:
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
            await self.app.connect_target(target)
        except HrnsClientError as exc:
            self._status.update(f"error · {exc.message}")
        except Exception as exc:  # unexpected startup failures stay on screen
            self._status.update(f"error · {exc}")
        finally:
            self._connecting = False
            if self.is_attached:
                self._list.focus()

    async def action_escape_picker(self) -> None:
        if self._browsing is not None:
            self._browsing = None
            self._render_entries()
            self._status.update("Back to saved connections.")
        else:
            await self.app.action_quit_app()

    def action_refresh(self) -> None:
        if self._browsing is not None:
            self._browse_profile(self._browsing)
        else:
            self._render_entries()
            self._status.update(
                "Profiles reloaded. Press n to add a target or edit the TOML file directly."
            )

    async def action_quit_app(self) -> None:
        await self.app.action_quit_app()
