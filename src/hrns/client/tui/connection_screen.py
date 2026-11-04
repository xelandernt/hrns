"""Searchable connection picker for local models and stdio ACP agents."""

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

from ..errors import HrnsClientError
from ..launch import ConnectionTarget, local_cwd, remote_cwd
from ..local import local_detail, local_target
from ..profiles import Profile, Profiles, default_profiles_path
from ..transport import check_websocket_url
from .choice_keys import handle_choice_arrow
from .dialogs import FormDialog
from .screen_base import _HrnsScreen


def _short_path(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return "…" + value[-(limit - 1) :]


@dataclass(slots=True)
class PickEntry:
    kind: str  # "local" | "profile" | "add"
    label: str
    detail: str
    profile: Profile | None = None

    @property
    def is_selectable(self) -> bool:
        return self.kind in {"local", "profile"}


class ConnectScreen(_HrnsScreen):
    """Pick the configured local model, a saved stdio profile, or add one."""

    BINDINGS = [
        Binding("q", "quit_app", "Quit", show=False),
        Binding("escape", "escape_picker", "Back", show=False),
        Binding("r", "refresh", "Refresh", show=False),
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
            "l local providers · n add ACP command · / filter · r refresh · q quit",
            markup=False,
            classes="panel-hint",
            id="connect-hint",
        )

    def on_mount(self) -> None:
        self._render_banner()
        self._render_entries()
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
        text.append("Local model or stdio ACP agent", style="dim")
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
                kind="add",
                label="Add ACP command…",
                detail="Save a stdio ACP command as a profile.",
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
            elif entry.kind == "add":
                label.append("+      ", style="bold")
            elif entry.profile is not None and entry.profile.is_remote:
                label.append("remote ", style="bold")
            else:
                label.append("stdio  ", style="bold")
            label.append(entry.label)
            self._list.append(ListItem(Static(label, markup=False)))
        if self._visible:
            self._list.index = 0
            self._show_detail(self._visible[0])

    def _show_detail(self, entry: PickEntry) -> None:
        lines = [entry.label, entry.detail]
        if entry.profile is not None:
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
        else:
            self.action_add_target()

    def action_configure_local(self) -> None:
        self._status.update(
            "Edit hrns.local.toml or use HRNS_LOCAL_* environment variables to change providers."
        )

    def action_add_target(self) -> None:
        self.app.push_screen(
            FormDialog(
                "Add ACP command",
                "Save a stdio ACP agent as a reusable connection.",
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
            ),
            self._target_added,
        )

    def _target_added(self, values: dict[str, Any] | None) -> None:
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
        if not self.profiles.sources:
            self.profiles.sources.append(default_profiles_path())
        self.profiles.upsert(profile)
        self.profiles.save(self.profiles.sources[0])
        self._render_entries()
        self._status.update(f"Saved profile '{profile.name}'.")

    def _connect_local(self) -> None:
        try:
            target = local_target()
        except HrnsClientError as exc:
            self._status.update(f"error · {exc.message}")
            return
        self._connect_worker(target)

    def _connect_profile(self, profile: Profile) -> None:
        if profile.is_remote:
            if not profile.url:
                self._status.update(f"Profile '{profile.name}' has no server URL.")
                return
            try:
                check_websocket_url(profile.url)
                cwd = remote_cwd(profile)
            except HrnsClientError as exc:
                self._status.update(f"error · {exc.message}")
                return
            self._connect_worker(
                ConnectionTarget(
                    name=profile.name,
                    kind="remote",
                    profile=profile,
                    cwd=cwd,
                )
            )
            return
        if not profile.command:
            self._status.update(f"Profile '{profile.name}' has no command.")
            return
        self._connect_worker(
            ConnectionTarget(
                name=profile.name,
                kind="local",
                profile=profile,
                cwd=local_cwd(profile),
            )
        )

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
