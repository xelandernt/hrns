"""Searchable connection picker for local models, ACP commands, and remote ACP."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, ListItem, ListView, Static

from hrns.config import LocalSettings

from ..constants import LOCAL_CONNECTION
from ..errors import HrnsClientError
from ..launch import ConnectionTarget, profile_target, remote_target
from ..local import local_detail, local_target
from ..profiles import Profile, Profiles, default_profiles_path
from .choice_keys import handle_choice_arrow
from .dialogs import ChoiceDialog, FormDialog
from .local import (
    CustomModelDialog,
    CustomModelRequest,
    ModelPick,
    ModelPickerDialog,
    save_custom_model,
)
from .providers import ProvidersDialog, ProvidersResult
from .screen_base import _HrnsScreen


def _short_path(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return "…" + value[-(limit - 1) :]


@dataclass(slots=True)
class PickEntry:
    kind: str  # "local" | "profile" | "add_remote" | "add_stdio"
    label: str
    detail: str
    profile: Profile | None = None

    @property
    def is_selectable(self) -> bool:
        return self.kind in {"local", "profile", "add_remote", "add_stdio"}


class ConnectScreen(_HrnsScreen):
    """Pick a connection, add one, or edit a saved profile."""

    BINDINGS = [
        Binding("q", "quit_app", "Quit", show=False),
        Binding("escape", "escape_picker", "Back", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("slash", "filter", "Filter", show=False),
        Binding("l", "configure_local", "Local model", show=False),
        Binding("n", "add_target", "New target", show=False),
        Binding("e", "edit_target", "Edit target", show=False),
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
            "l local providers · n add · e edit · / filter · r refresh · q quit",
            markup=False,
            classes="panel-hint",
            id="connect-hint",
        )

    def on_mount(self) -> None:
        self._render_banner()
        self._render_entries()
        self._list.focus()
        if self.status_message:
            self._status.update(self.status_message)
        if self.auto_connect and self.initial_target is not None:
            self._connect_worker(self.initial_target)

    def on_screen_resume(self) -> None:
        self._render_entries()

    def on_resize(self, event: Any) -> None:
        del event
        self._apply_layout()

    def _apply_layout(self) -> None:
        try:
            self._filter.styles.width = "100%"
        except Exception:
            return

    def _render_banner(self) -> None:
        text = Text()
        text.append("hrns", style="bold")
        text.append("  standard ACP terminal\n", style="dim")
        text.append("Local model, stdio ACP agent, or remote ACP server", style="dim")
        self._banner.update(text)

    def _render_entries(self) -> None:
        local_ok = True
        detail = "Configured hrns.local.toml or HRNS_LOCAL_* environment"
        try:
            detail = local_detail()
        except HrnsClientError as exc:
            local_ok = False
            detail = exc.message
        entries = [
            PickEntry(
                kind="local",
                label="Local model",
                detail=detail,
            )
        ]
        for profile in sorted(self.profiles.profiles.values(), key=lambda item: item.name):
            if not (profile.command or (profile.is_remote and profile.url)):
                continue
            transport = "remote" if profile.is_remote else "stdio"
            entries.append(
                PickEntry(
                    kind="profile",
                    label=f"{transport} · {profile.name}",
                    detail=profile.describe_location(),
                    profile=profile,
                )
            )
        entries.append(
            PickEntry(
                kind="add_remote",
                label="Add remote ACP server…",
                detail="Connect to a wss:// ACP WebSocket endpoint with an optional token.",
            )
        )
        entries.append(
            PickEntry(
                kind="add_stdio",
                label="Add stdio ACP command…",
                detail="Save a local ACP command as a profile.",
            )
        )
        self.entries = entries
        self._render_visible("")
        if not local_ok:
            self._status.update(detail)

    def _render_visible(self, needle: str) -> None:
        needle = needle.lower()
        self._visible = [
            entry
            for entry in self.entries
            if not needle or needle in entry.label.lower() or needle in entry.detail.lower()
        ]
        self._list.clear()
        for entry in self._visible:
            label = Text()
            if entry.kind == "local":
                label.append("local  ", style="bold")
            elif entry.kind in {"add_remote", "add_stdio"}:
                label.append("+      ", style="bold")
            elif entry.profile is not None and entry.profile.is_remote:
                label.append("remote ", style="bold")
            else:
                label.append("stdio  ", style="bold")
            label.append(entry.label)
            self._list.append(ListItem(Static(label, markup=False)))
        if self._visible:
            index = self._remembered_index()
            self._list.index = index
            self._show_detail(self._visible[index])

    def _remembered_index(self) -> int:
        """Where the cursor starts: the connection used last, else the top of the list."""
        identity = self.profiles.last_connection
        if not identity:
            return 0
        for index, entry in enumerate(self._visible):
            if identity == LOCAL_CONNECTION and entry.kind == "local":
                return index
            if entry.profile is not None and entry.profile.name == identity:
                return index
        return 0

    def _show_detail(self, entry: PickEntry) -> None:
        lines = [entry.label, entry.detail]
        if entry.profile is not None:
            if entry.profile.is_remote:
                lines.append(f"server: {entry.profile.url}")
                if entry.profile.token:
                    token = entry.profile.token
                    shown = token if token.startswith(("env:", "keyring:")) else "<literal>"
                    lines.append(f"token: {shown}")
            else:
                lines.append(f"command: {entry.profile.command} {' '.join(entry.profile.args)}")
        self._detail.update("\n".join(lines))

    def on_key(self, event: Any) -> None:
        handle_choice_arrow(self._list, event.key)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._render_visible(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._visible:
            self._activate(self._visible[0])

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._visible):
            self._show_detail(self._visible[index])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._visible):
            self._activate(self._visible[index])

    def action_filter(self) -> None:
        self._filter.focus()

    def _activate(self, entry: PickEntry) -> None:
        if entry.kind == "local":
            self._connect_local()
        elif entry.kind == "profile" and entry.profile is not None:
            self._connect_profile(entry.profile)
        elif entry.kind == "add_remote":
            self._open_remote_form(None)
        else:
            self._open_stdio_form(None)

    def action_configure_local(self) -> None:
        try:
            settings = LocalSettings()
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        self.app.push_screen(ProvidersDialog(settings), self._local_providers_closed)

    def _local_providers_closed(self, result: ProvidersResult | None) -> None:
        if result is None:
            return
        try:
            settings = LocalSettings()
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        if not settings.providers:
            self._status.update("No local providers are configured.")
            return
        if not settings.provider or not settings.model:
            self.app.push_screen(
                ModelPickerDialog(settings),
                lambda pick: self._local_model_picked(settings, pick),
            )
            return
        self._render_entries()
        self._status.update(f"Local model: {settings.provider}:{settings.model}")

    def _local_model_picked(
        self, settings: LocalSettings, result: ModelPick | CustomModelRequest | None
    ) -> None:
        if isinstance(result, ModelPick):
            self._save_local_model(settings, result.provider, result.model)
            return
        if not isinstance(result, CustomModelRequest):
            return
        if result.model:
            self._add_local_model(settings, result.provider, result.model)
            return
        provider_id = settings.provider_id(result.provider)

        def apply(values: Any) -> None:
            if not isinstance(values, dict):
                return
            model = str(values.get("model") or "").strip()
            if model:
                self._add_local_model(settings, result.provider, model)

        self.app.push_screen(
            CustomModelDialog(result.provider, provider_id),
            apply,
        )

    def _add_local_model(self, settings: LocalSettings, provider: str, model: str) -> None:
        try:
            _path, updated = save_custom_model(settings, provider, model)
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        self._save_local_model(updated, provider, model)

    def _save_local_model(self, settings: LocalSettings, provider: str, model: str) -> None:
        try:
            configured = settings.model_copy(update={"provider": provider, "model": model})
            path = configured.save(settings.config_file)
        except (ValueError, OSError) as exc:
            self._status.update(f"error · {exc}")
            return
        self._render_entries()
        self._status.update(f"Local model saved to {path}")

    def action_add_target(self) -> None:
        self.app.push_screen(
            ChoiceDialog(
                "Add connection",
                "Choose how the ACP agent runs.",
                [
                    {
                        "value": "remote",
                        "name": "Remote ACP server",
                        "description": "Connect to wss://host/acp with an optional bearer token.",
                    },
                    {
                        "value": "stdio",
                        "name": "Stdio ACP command",
                        "description": "Launch a local ACP agent as a subprocess.",
                    },
                ],
            ),
            self._target_kind_chosen,
        )

    def _target_kind_chosen(self, kind: str | None) -> None:
        if kind == "remote":
            self._open_remote_form(None)
        elif kind == "stdio":
            self._open_stdio_form(None)

    def _selected_entry(self) -> PickEntry | None:
        index = self._list.index
        if index is None or not 0 <= index < len(self._visible):
            return None
        return self._visible[index]

    def action_edit_target(self) -> None:
        entry = self._selected_entry()
        if entry is None or entry.profile is None:
            self._status.update("Select a saved remote or stdio connection to edit.")
            return
        self._open_connection_form(entry.profile)

    def _open_connection_form(self, profile: Profile | None) -> None:
        if profile is None or profile.is_remote:
            self._open_remote_form(profile)
        else:
            self._open_stdio_form(profile)

    def _open_remote_form(self, profile: Profile | None) -> None:
        editing = profile is not None
        current = None
        if profile is not None:
            current = {
                "name": profile.name,
                "server": profile.url,
                "token": profile.token,
                "cwd": profile.remote_cwd,
            }
        self.app.push_screen(
            FormDialog(
                "Edit remote ACP server" if editing else "Add remote ACP server",
                (
                    "Update the saved WebSocket ACP endpoint."
                    if editing
                    else "Save a WebSocket ACP endpoint as a reusable connection."
                ),
                [
                    {
                        "name": "name",
                        "title": "Profile name",
                        "kind": "string",
                        "required": True,
                    },
                    {
                        "name": "server",
                        "title": "Server URL",
                        "description": "ACP WebSocket endpoint, for example wss://agents.example.com/acp.",
                        "kind": "string",
                        "required": True,
                    },
                    {
                        "name": "token",
                        "title": "Bearer token",
                        "description": (
                            "Secret reference (env:NAME or keyring:service/user) or a literal "
                            "token. Literals are stored in the profile file, so prefer a "
                            "reference. Leave blank when the server does not require auth."
                        ),
                        "kind": "string",
                        "required": False,
                    },
                    {
                        "name": "cwd",
                        "title": "Remote working directory",
                        "description": "Absolute path on the server. Defaults to this machine's cwd.",
                        "kind": "string",
                        "required": False,
                    },
                ],
                accept_label="Save",
                current=current,
            ),
            lambda values: self._remote_target_saved(values, profile),
        )

    def _remote_target_saved(self, values: dict[str, Any] | None, original: Profile | None) -> None:
        if not values:
            return
        name = str(values.get("name") or "").strip()
        server = str(values.get("server") or "").strip()
        token = str(values.get("token") or "").strip()
        cwd = str(values.get("cwd") or "").strip()
        try:
            target = remote_target(server, name=name, token=token, cwd=cwd)
        except HrnsClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        profile = target.profile
        if original is not None:
            self._carry_over_settings(original, profile)
        self._save_profile(profile, replacing=original.name if original is not None else None)
        self._render_entries()
        self._status.update(f"Saved profile '{profile.name}'.")
        if original is None:
            self._connect_worker(target)

    def _open_stdio_form(self, profile: Profile | None) -> None:
        editing = profile is not None
        current = None
        if profile is not None:
            current = {
                "name": profile.name,
                "command": shlex.join([profile.command, *profile.args]),
                "cwd": profile.cwd,
            }
        self.app.push_screen(
            FormDialog(
                "Edit ACP command" if editing else "Add ACP command",
                (
                    "Update the saved stdio ACP command."
                    if editing
                    else "Save a stdio ACP agent as a reusable connection."
                ),
                [
                    {
                        "name": "name",
                        "title": "Profile name",
                        "kind": "string",
                        "required": True,
                    },
                    {
                        "name": "command",
                        "title": "Command",
                        "description": "For example: python -m hrns.app acp --model test",
                        "kind": "string",
                        "required": True,
                    },
                    {
                        "name": "cwd",
                        "title": "Working directory",
                        "description": "Optional absolute path.",
                        "kind": "string",
                        "required": False,
                    },
                ],
                accept_label="Save",
                current=current,
            ),
            lambda values: self._stdio_target_saved(values, profile),
        )

    def _stdio_target_saved(self, values: dict[str, Any] | None, original: Profile | None) -> None:
        if not values:
            return
        words = shlex.split(str(values.get("command") or ""))
        if not words:
            self._status.update("A command is required.")
            return
        profile = Profile(
            name=str(values.get("name") or "stdio"),
            transport="stdio",
            command=words[0],
            args=words[1:],
            cwd=str(values.get("cwd") or ""),
        )
        if original is not None:
            self._carry_over_settings(original, profile)
        self._save_profile(profile, replacing=original.name if original is not None else None)
        self._render_entries()
        self._status.update(f"Saved profile '{profile.name}'.")

    @staticmethod
    def _carry_over_settings(original: Profile, profile: Profile) -> None:
        """Keep advanced profile settings the simple form does not edit."""
        profile.agent = original.agent
        profile.env = dict(original.env)
        profile.auth_method = original.auth_method
        profile.client_io = original.client_io
        profile.default_for_server = original.default_for_server
        profile.notes = original.notes

    def _save_profile(self, profile: Profile, *, replacing: str | None = None) -> None:
        if replacing is not None and replacing != profile.name:
            self.profiles.profiles.pop(replacing, None)
        if not self.profiles.sources:
            self.profiles.sources.append(default_profiles_path())
        self.profiles.upsert(profile)
        self.profiles.save(self.profiles.sources[0])

    def _connect_local(self) -> None:
        try:
            target = local_target()
        except HrnsClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        self._connect_worker(target)

    def _connect_profile(self, profile: Profile) -> None:
        try:
            target = profile_target(profile)
        except HrnsClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        self._connect_worker(target)

    @work(group="connect", exclusive=True)
    async def _connect_worker(self, target: ConnectionTarget) -> None:
        self._connecting = True
        self._status.update(f"Connecting to {target.location_text()}…")
        try:
            await self.app.connect_target(target)
        except HrnsClientError as exc:
            self._status.update(f"error · {exc.message}")
        except Exception as exc:
            self._status.update(f"error · {exc}")
        finally:
            self._connecting = False
            if self.is_attached:
                self._list.focus()

    async def action_escape_picker(self) -> None:
        await self.app.action_quit_app()

    def action_refresh(self) -> None:
        self._render_entries()
        self._status.update("Profiles reloaded.")

    async def action_quit_app(self) -> None:
        await self.app.action_quit_app()
