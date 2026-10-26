"""Capability, plugin, and MCP workflow."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from ...errors import ExtensionUnavailable, HrnsClientError
from ..dialogs import ChoiceDialog, FormDialog
from ..panels import SidePanel
from ..state import binding_summary, build_form_fields

if TYPE_CHECKING:
    from .screen import ChatScreen


class CapabilityWorkflow:
    """Owns plugin bindings, capability configuration, and MCP probing."""

    def __init__(self, screen: ChatScreen) -> None:
        self.screen = screen

    # ------------------------------------------------------------ commands

    def set_plugin_commands(self, commands: tuple[Any, ...], *, revision: int) -> None:
        screen = self.screen
        screen._plugin_commands = tuple(
            command.model_dump(mode="json", by_alias=True)
            if hasattr(command, "model_dump")
            else dict(command)
            for command in commands
        )
        screen._plugin_revision = revision
        screen.prompts.sync_command_suggestions()

    async def refresh_plugin_commands(self) -> None:
        screen = self.screen
        if not screen.client.extension.supports("_hrns/commands/list"):
            self.set_plugin_commands((), revision=0)
            return
        try:
            result = await screen.session.list_commands()
        except (ExtensionUnavailable, HrnsClientError):
            self.set_plugin_commands((), revision=0)
            return
        self.set_plugin_commands(result.commands, revision=result.selected_revision)

    async def refresh(self) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        await self.refresh_plugin_commands()
        if screen.session.client is not screen.client:
            panel.show_message(
                "This session is not attached to the reconnected client. "
                "Start a new session to change plugins."
            )
            return
        if screen.client.extension.supports(
            "_hrns/capabilities/list"
        ) and screen.client.extension.supports("_hrns/configuration/get"):
            try:
                bindings = await self.all_capability_pages()
                snapshot = await screen.session.configuration()
                screen._bindings_raw = [
                    binding.model_dump(mode="json", by_alias=True) for binding in bindings
                ]
                panel.set_bindings(
                    self.plugin_bindings(),
                    snapshot.to_wire(),
                    active_revision=snapshot.active_revision,
                    selected_revision=snapshot.selected_revision,
                    can_write=screen.client.extension.supports("_hrns/configuration/update"),
                )
                self.sync_mcp_panel(snapshot)
                await screen.prompts.refresh_skills()
                return
            except ExtensionUnavailable:
                pass
            except HrnsClientError as exc:
                panel.show_message(exc.message)
                return
        panel.show_message("Loading standard configuration…")
        panel.set_standard_options(list(screen.session.config_options))
        await screen.prompts.refresh_skills()

    def apply_snapshot(self, snapshot: Any) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        if screen._bindings_raw:
            panel.set_bindings(
                self.plugin_bindings(),
                snapshot.to_wire(),
                active_revision=snapshot.active_revision,
                selected_revision=snapshot.selected_revision,
                can_write=screen.client.extension.supports("_hrns/configuration/update"),
            )
            self.sync_mcp_panel(snapshot)

    # ------------------------------------------------------------- bindings

    async def toggle_binding(self, binding_id: str, enabled: bool) -> None:
        screen = self.screen
        side = screen.query_one("#side-panel", SidePanel)
        panel: Any = side.capabilities
        summary = panel.binding(binding_id)
        if summary is None:
            panel = side.mcp
            summary = panel.binding(binding_id)
        if summary is None:
            return
        label = str(summary["name"])
        if summary.get("pluginId") == "hrns.mcp":
            label = str(summary["bindingId"]).removeprefix("mcp.")
        if not summary["canToggle"]:
            panel.show_message(f"{label} cannot be toggled from this client.")
            return
        try:
            snapshot = await screen.session.update_configuration(
                [{"type": "enable", "bindingId": binding_id, "enabled": enabled}]
            )
        except HrnsClientError as exc:
            panel.show_message(exc.message)
            if "REVISION_CONFLICT" in exc.message or "revision" in exc.message.lower():
                await self.refresh()
            return
        self.apply_snapshot(snapshot)
        await self.refresh()
        state = "enabled" if enabled else "disabled"
        panel.show_message(
            f"{label} {state} for the next turn."
            if summary["changeMode"] == "next_turn"
            else f"{label} {state}; change takes effect with a new session."
        )

    def edit_binding(self, binding_id: str) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
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
            screen._apply_binding_config(binding_id, fields_values)

        subtitle = (
            "Field replacements apply at the next turn boundary when the binding mode allows it."
        )
        if summary["requires"]:
            subtitle += " Requires: " + ", ".join(summary["requires"]) + "."
        if summary["conflicts"]:
            subtitle += " Conflicts: " + ", ".join(summary["conflicts"]) + "."
        screen.app.push_screen(
            FormDialog(
                f"Configure {summary['name']}",
                subtitle,
                fields,
                accept_label="Save configuration",
                current=summary.get("config") or {},
            ),
            lambda result: apply(result),
        )

    def plugin_bindings(self) -> list[dict[str, Any]]:
        """Bindings shown in the Plugins panel; MCP servers have their own tab."""
        return [
            binding
            for binding in self.screen._bindings_raw
            if str(binding.get("pluginId") or binding.get("plugin_id") or "") != "hrns.mcp"
        ]

    def mcp_bindings(self) -> list[dict[str, Any]]:
        return [
            binding
            for binding in self.screen._bindings_raw
            if str(binding.get("pluginId") or binding.get("plugin_id") or "") == "hrns.mcp"
        ]

    def sync_mcp_panel(self, snapshot: Any = None) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).mcp
        wire = snapshot.to_wire() if snapshot is not None else None
        panel.set_bindings(
            self.mcp_bindings(),
            wire,
            can_write=screen.client.extension.supports("_hrns/configuration/update"),
        )

    # ------------------------------------------------------------------ mcp

    async def refresh_mcp(self) -> None:
        """Probe every enabled MCP server; discovery stays host-side."""
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).mcp
        bindings = self.mcp_bindings()
        if not bindings and screen.client.extension.supports("_hrns/capabilities/list"):
            await self.refresh()
            bindings = self.mcp_bindings()
        if not bindings:
            self.sync_mcp_panel()
            panel.show_message(
                "No MCP servers are offered by this agent. "
                "Local servers are added with `hrns mcp add`."
            )
            return

        await self.refresh_plugin_commands()

        snapshot = None
        if screen.client.extension.supports("_hrns/configuration/get"):
            try:
                snapshot = await screen.session.configuration()
            except (ExtensionUnavailable, HrnsClientError):
                snapshot = None
        self.sync_mcp_panel(snapshot)
        wire = snapshot.to_wire() if snapshot is not None else None
        enabled = [binding for binding in bindings if binding_summary(binding, wire)["enabled"]]
        if not enabled:
            panel.show_message("Every MCP server is disabled. Press Space to enable one.")
            return
        if not screen.client.extension.supports("_hrns/commands/invoke"):
            panel.show_message("This host cannot probe MCP servers (commands are unavailable).")
            return

        panel.show_message(f"Probing {len(enabled)} MCP server(s)…")
        results = await asyncio.gather(
            *(self.probe_mcp(binding) for binding in enabled),
            return_exceptions=True,
        )
        statuses: dict[str, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, tuple):
                statuses[result[0]] = result[1]
        panel.set_statuses(statuses)
        connected = sum(1 for status in statuses.values() if status.get("state") == "connected")
        failed = len(statuses) - connected
        note = f"Probed {len(statuses)} server(s): {connected} connected"
        if failed:
            note += f", {failed} unavailable"
        panel.show_message(note + ".")

    async def probe_mcp(self, binding: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        screen = self.screen
        binding_id = str(binding.get("bindingId") or binding.get("binding_id") or "")
        if not binding_id or screen.commands.find_plugin_command(f"{binding_id}:status") is None:
            return binding_id, {}
        try:
            response = await screen.session.invoke_command(
                binding_id,
                "status",
                expected_revision=screen._plugin_revision,
                request_id=f"ui-{uuid.uuid4().hex}",
            )
        except (ExtensionUnavailable, HrnsClientError) as exc:
            return binding_id, {"state": "failed", "error": str(exc)}
        presentation = response.result.presentation
        data = presentation.data if presentation is not None else {}
        return binding_id, dict(data) if isinstance(data, dict) else {}

    async def all_capability_pages(self) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            listing = await self.screen.session.list_capabilities(cursor=cursor, limit=100)
            items.extend(listing.bindings)
            cursor = listing.next_cursor
            if not cursor or cursor in seen:
                return items
            seen.add(cursor)

    async def all_plugin_pages(self) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            listing = await self.screen.session.list_plugins(cursor=cursor, limit=100)
            items.extend(listing.plugins)
            cursor = listing.next_cursor
            if not cursor or cursor in seen:
                return items
            seen.add(cursor)

    async def choose_plugin_to_add(self) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        if not (
            screen.client.extension.local_binding_creation
            and screen.client.extension.supports("_hrns/plugins/list")
        ):
            panel.show_message(
                "This host does not advertise local binding creation; only offered bindings "
                "can be enabled here."
            )
            return
        try:
            inventory = await self.all_plugin_pages()
        except HrnsClientError as exc:
            panel.show_message(exc.message)
            return
        available = [
            plugin
            for plugin in inventory
            if plugin.runtime_compatible
            and plugin.change_mode == "next_turn"
            and plugin.plugin_id != "hrns.mcp"
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
                        f"needs: {', '.join(need.name for need in plugin.needs)}"
                        if plugin.needs
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
                screen._plugin_add_form_worker(plugin_id)

        screen.app.push_screen(
            ChoiceDialog(
                "Add a local plugin",
                "Only plugins the host advertises for local binding creation are listed.",
                choices,
            ),
            start_plugin,
        )

    async def plugin_add_form(self, plugin_id: str) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        try:
            inventory = await self.all_plugin_pages()
        except HrnsClientError as exc:
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
            screen._add_capability(
                plugin.plugin_id,
                binding_id,
                enabled,
                values,
                plugin.change_mode,
            )

        subtitle = f"Create a new binding from {plugin.plugin_id}."
        if plugin.requirements:
            subtitle += " Requires: " + ", ".join(plugin.requirements) + "."
        if plugin.needs:
            subtitle += " Needs: " + ", ".join(need.name for need in plugin.needs) + "."
        if plugin.constraint:
            subtitle += " " + plugin.constraint
        screen.app.push_screen(
            FormDialog(
                "Add a local plugin",
                subtitle,
                fields,
                accept_label="Add plugin",
            ),
            apply,
        )

    async def add_capability(
        self,
        plugin_id: str,
        binding_id: str,
        enabled: bool,
        config: dict[str, Any],
        change_mode: str = "next_turn",
    ) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        try:
            snapshot = await screen.session.configuration()
            updated = await screen.session.update_configuration(
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
        except HrnsClientError as exc:
            panel.show_message(exc.message)
            return
        self.apply_snapshot(updated)
        await self.refresh()
        timing = "with a new session" if change_mode == "new_session" else "for the next turn"
        state = "enabled" if enabled else "disabled"
        panel.show_message(f"Added {binding_id} from {plugin_id} ({state}) {timing}.")

    async def apply_binding_config(self, binding_id: str, values: dict[str, Any]) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        try:
            snapshot = await screen.session.update_configuration(
                [{"type": "configure", "bindingId": binding_id, "fields": values}]
            )
        except HrnsClientError as exc:
            panel.show_message(exc.message)
            return
        self.apply_snapshot(snapshot)
        await self.refresh()
        panel.show_message(f"Configuration saved for {binding_id}.")
