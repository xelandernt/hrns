"""Terminal widgets: transcript, composer, status, todo/capability/session/activity panels."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import (
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from .choice_keys import choice_direction
from .formatting import truncate_text
from .panels import ActivityPanel, SessionPanel, TodoPanel
from .state import binding_summary, config_option_value

__all__ = [
    "CapabilityPanel",
    "McpPanel",
    "McpRefreshRequested",
    "SidePanel",
    "SidePanelCloseRequested",
]


class McpRefreshRequested(Message):
    """The local user asked to probe every configured MCP server again."""


class McpPanel(Vertical):
    """Configured MCP servers, their connection state and discovered tools.

    Adding, authenticating and removing servers belong to ``hrns mcp``; this panel is the
    live view, with enable/disable going through the same capability policy as plugins.
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
        Binding("space", "toggle_server", "Toggle", show=False),
        Binding("a", "authenticate", "Authenticate", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.can_write = False
        self._server_bindings: list[dict[str, Any]] = []
        self._snapshot: dict[str, Any] | None = None
        self._statuses: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._summary: dict[str, dict[str, Any]] = {}
        self._status = Static("MCP servers load when you open /mcp.", markup=False)
        self._status.add_class("panel-status")
        self._list = OptionList(id="mcp-list", markup=False)
        self._detail = Static("", markup=False, classes="panel-detail")
        self._hint = Static(
            "r refresh · Space enable/disable · a auth hint · Esc close",
            markup=False,
            classes="panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._hint
        yield self._list
        yield self._detail

    # ------------------------------------------------------------------ data

    def show_message(self, text: str) -> None:
        self._status.update(Text(text))

    def set_bindings(
        self,
        bindings: list[dict[str, Any]],
        snapshot: dict[str, Any] | None = None,
        *,
        can_write: bool = True,
    ) -> None:
        self.can_write = can_write
        self._server_bindings = list(bindings)
        self._snapshot = snapshot
        self._render_rows()

    def set_statuses(self, statuses: dict[str, dict[str, Any]]) -> None:
        self._statuses = dict(statuses)
        self._render_rows()

    def statuses(self) -> dict[str, dict[str, Any]]:
        return dict(self._statuses)

    def binding(self, binding_id: str) -> dict[str, Any] | None:
        return self._summary.get(binding_id)

    def current_binding_id(self) -> str | None:
        index = self._list.highlighted
        if index is None or index < 0 or index >= len(self._order):
            return None
        return self._order[index]

    def _render_rows(self) -> None:
        previous = self.current_binding_id()
        self._summary = {}
        self._order = []
        self._list.clear_options()
        for raw in self._server_bindings:
            summary = binding_summary(raw, self._snapshot)
            binding_id = summary["bindingId"]
            if not binding_id:
                continue
            self._summary[binding_id] = summary
            self._order.append(binding_id)
            self._list.add_option(Option(self._row_text(summary), id=binding_id))

        total = len(self._order)
        deferred = sum(1 for status in self._statuses.values() if bool(status.get("deferred")))
        notes = [f"{total} server" + ("" if total == 1 else "s")]
        if deferred:
            notes.append(f"{deferred} deferred")
        if not self.can_write:
            notes.append("read-only")
        self._status.update(Text(" · ".join(notes), style="dim"))
        self._hint.update(
            "r refresh · Esc close"
            if not self.can_write
            else "r refresh · Space enable/disable · a auth hint · Esc close"
        )
        if self._list.option_count:
            index = self._order.index(previous) if previous in self._order else 0
            self._list.highlighted = index
            self._show_detail(self._order[index])
        else:
            self._detail.update(
                Text(
                    "No MCP servers configured. Add one with "
                    "`hrns mcp add NAME -- npx -y PACKAGE`.",
                    style="dim",
                )
            )
        if not self._server_bindings:
            self._status.update(Text("No MCP servers configured.", style="dim"))

    def _row_text(self, summary: dict[str, Any]) -> str:
        name = str(
            (self._statuses.get(summary["bindingId"]) or {}).get("name")
            or server_name_of(summary["bindingId"])
        )
        if not summary["enabled"]:
            return f"{name}   disabled"
        status = self._statuses.get(summary["bindingId"]) or {}
        state = str(status.get("state") or "checking…")
        bits = [f"{name}   {state}"]
        tools = status.get("tools")
        if isinstance(tools, list) and state == "connected":
            bits.append(f"{len(tools)} tool" + ("" if len(tools) == 1 else "s"))
        if status.get("deferred"):
            bits.append("deferred")
        return "   ".join(bits)

    def _show_detail(self, binding_id: str) -> None:
        summary = self._summary.get(binding_id) or {}
        status = self._statuses.get(binding_id) or {}
        if not summary.get("enabled"):
            self._detail.update(
                Text(
                    f"{server_name_of(binding_id)} is disabled. "
                    f"Press Space or run `hrns mcp enable {server_name_of(binding_id)}`.",
                    style="dim",
                )
            )
            return
        lines: list[str] = []
        target = str(status.get("target") or "")
        if target:
            lines.append(target)
        error = status.get("error")
        if error:
            lines.append(str(error))
        tools = status.get("tools")
        if isinstance(tools, list) and tools:
            names = [str(tool.get("name", "")) for tool in tools if isinstance(tool, dict)]
            lines.append("tools: " + ", ".join(names))
        if not lines:
            lines.append("Probing on next refresh…")
        self._detail.update(Text("\n".join(lines), style="dim"))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id:
            self._show_detail(event.option_id)

    def action_refresh(self) -> None:
        self.post_message(McpRefreshRequested())

    def action_toggle_server(self) -> None:
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        summary = self._summary.get(binding_id)
        if summary is None:
            return
        if not self.can_write:
            self.show_message("This host advertises a read-only plugin catalog.")
            return
        if summary["locked"]:
            self.show_message(f"{summary['name']} is locked: {summary['reason'] or 'unavailable'}")
            return
        self.post_message(BindingToggleRequested(binding_id, not summary["enabled"]))

    def action_authenticate(self) -> None:
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        name = server_name_of(binding_id)
        status = self._statuses.get(binding_id) or {}
        if status.get("authenticated") is None:
            self.show_message(f"{name} does not use OAuth. Inspect it with `hrns mcp get {name}`.")
        elif status.get("authenticated") is False or status.get("state") == "needs auth":
            self.show_message(f"Run `hrns mcp auth {name}` in a terminal, then press r to refresh.")
        else:
            self.show_message(
                f"OAuth is managed by the CLI: `hrns mcp auth {name}` / `hrns mcp logout {name}`."
            )


def server_name_of(binding_id: str) -> str:
    """The configured server name behind an ``mcp.<name>`` binding id."""
    return binding_id.removeprefix("mcp.")


class SidePanelCloseRequested(Message):
    """The user asked to dismiss the on-demand side panel."""


class SidePanel(TabbedContent):
    """Todos, plugins, MCP servers, sessions and activity, opened as a right drawer."""

    PANE_ORDER: tuple[str, ...] = ("todos", "capabilities", "mcp", "sessions", "activity")

    BINDINGS = [
        Binding("tab", "next_pane", "Next panel", show=False),
        Binding("shift+tab", "previous_pane", "Previous panel", show=False),
        Binding("escape", "close", "Close panel", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(initial="capabilities", id="side-panel")
        self.todos = TodoPanel(id="todo-panel")
        self.capabilities = CapabilityPanel(id="capability-panel")
        self.mcp = McpPanel(id="mcp-panel")
        self.sessions = SessionPanel(id="session-panel")
        self.activity = ActivityPanel(id="activity-panel")
        # TabbedContent.compose() builds its private ContentTabs from this list.
        # Overriding compose() here would omit ContentTabs entirely.
        self._tab_content.extend(
            [
                TabPane("Todos", self.todos, id="todos"),
                TabPane("Plugins", self.capabilities, id="capabilities"),
                TabPane("MCP", self.mcp, id="mcp"),
                TabPane("Sessions", self.sessions, id="sessions"),
                TabPane("Activity", self.activity, id="activity"),
            ]
        )

    def activate(self, tab: str) -> None:
        """Show ``tab`` and move focus into its content."""
        self.active = tab
        # The pane is only focusable once the switch has been laid out.
        self.call_after_refresh(self._focus_pane, tab)

    def select(self, tab: str) -> None:
        """Show ``tab`` without taking focus away from the composer."""
        self.active = tab

    def _focus_pane(self, tab: str) -> None:
        if tab == "todos":
            self.todos.focus()
        elif tab == "capabilities":
            self.capabilities._list.focus()
        elif tab == "mcp":
            self.mcp._list.focus()
        elif tab == "sessions":
            self.sessions._table.focus()
        else:
            self.activity.focus()

    def action_next_pane(self) -> None:
        self._cycle_pane(1)

    def action_previous_pane(self) -> None:
        self._cycle_pane(-1)

    def action_close(self) -> None:
        self.post_message(SidePanelCloseRequested())

    def _cycle_pane(self, step: int) -> None:
        order = self.PANE_ORDER
        current = self.active
        index = order.index(current) if current in order else 0
        self.activate(order[(index + step) % len(order)])


class BindingToggleRequested(Message):
    def __init__(self, binding_id: str, enabled: bool) -> None:
        super().__init__()
        self.binding_id = binding_id
        self.enabled = enabled


class BindingEditRequested(Message):
    def __init__(self, binding_id: str) -> None:
        super().__init__()
        self.binding_id = binding_id


class ConfigOptionRequested(Message):
    def __init__(self, option_id: str) -> None:
        super().__init__()
        self.option_id = option_id


class ConfigOptionStepRequested(Message):
    """Move a standard ACP select option one value in a direction."""

    def __init__(self, option_id: str, direction: int) -> None:
        super().__init__()
        self.option_id = option_id
        self.direction = direction


class CapabilityRefreshRequested(Message):
    pass


class CapabilityAddRequested(Message):
    """The local user wants to add a registered plugin as a new binding."""


class CapabilityPanel(Vertical):
    """Selected vs active capability state, with honest locked reasons."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
        Binding("e", "edit", "edit", show=False),
        Binding("space", "toggle_binding", "Toggle", show=False),
        Binding("a", "add", "Add local plugin", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.extension_mode = False
        self.can_write = False
        self._plugin_bindings: list[dict[str, Any]] = []
        self._snapshot: dict[str, Any] | None = None
        self._summary: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._options: dict[str, dict[str, Any]] = {}
        self._row_chars = 0
        self._name_chars = 6
        self._status = Static("Loading plugins…", markup=False)
        self._status.add_class("panel-status")
        self._list = OptionList(id="binding-list", markup=False)
        self._detail = Static("", markup=False, classes="panel-detail")
        self._detail.display = False
        self._hint = Static(
            "e edit · Space toggle · a add · r refresh · Esc close",
            markup=False,
            classes="panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._hint
        yield self._list
        yield self._detail

    # ------------------------------------------------------------------ data

    def show_message(self, text: str) -> None:
        self._status.update(Text(text))

    def set_bindings(
        self,
        bindings: list[dict[str, Any]],
        snapshot: dict[str, Any] | None,
        *,
        active_revision: int | None = None,
        selected_revision: int | None = None,
        can_write: bool = True,
    ) -> None:
        self.extension_mode = True
        self.can_write = can_write
        self._plugin_bindings = list(bindings)
        self._snapshot = snapshot
        self._render_rows(active_revision=active_revision, selected_revision=selected_revision)

    def _render_rows(
        self,
        *,
        active_revision: int | None = None,
        selected_revision: int | None = None,
    ) -> None:
        """Rebuild every plugin row; descriptions follow the current panel width."""
        previous = self.current_binding_id()
        self._row_chars = self._row_width()
        self._summary = {}
        self._order = []
        self._options = {}
        self._list.clear_options()
        for raw in self._plugin_bindings:
            summary = binding_summary(raw, self._snapshot)
            binding_id = summary["bindingId"]
            if not binding_id:
                continue
            self._summary[binding_id] = summary
            self._order.append(binding_id)
            self._options[binding_id] = raw
        self._name_chars = self._name_column()
        for binding_id in self._order:
            self._list.add_option(Option(self._row_text(self._summary[binding_id]), id=binding_id))
        selected = (
            selected_revision
            if selected_revision is not None
            else (self._snapshot or {}).get("selectedRevision")
        )
        active = (
            active_revision
            if active_revision is not None
            else (self._snapshot or {}).get("activeRevision")
        )
        total = len(self._order)
        notes = [f"{total} plugin" + ("" if total == 1 else "s")]
        if selected is not None and active is not None and selected != active:
            notes.append("selected changes apply next turn")
        if not self.can_write:
            notes.append("read-only")
        self._status.update(Text(" · ".join(notes), style="dim"))
        self._hint.update(
            "read-only · r refresh · Esc close"
            if not self.can_write
            else "e edit · Space toggle · a add · r refresh · Esc close"
        )
        if self._list.option_count:
            index = self._highlight_index(previous)
            self._list.highlighted = index
            self._show_detail(self._order[index])
        else:
            self._show_detail(None)

    def _row_width(self) -> int:
        """Characters available to one list row."""
        width = self._list.content_size.width
        if width <= 0:
            width = self.size.width - 6
        return max(0, width)

    def _show_detail(self, binding_id: str | None) -> None:
        """Render the highlighted plugin's full description below the list."""
        summary = self._summary.get(binding_id) if binding_id else None
        if summary is None:
            self._detail.update(Text(""))
            self._detail.display = False
            return
        lines = [
            line for line in (" ".join(str(summary.get("description") or "").split()),) if line
        ]
        if summary["locked"]:
            lines.append(f"locked: {summary['reason'] or 'unavailable'}")
        requires = [str(item) for item in summary.get("requires") or ()]
        if requires:
            lines.append("requires: " + ", ".join(requires))
        needs = [str(item) for item in summary.get("needs") or ()]
        if needs:
            lines.append("needs: " + ", ".join(needs))
        conflicts = [str(item) for item in summary.get("conflicts") or ()]
        if conflicts:
            lines.append("conflicts: " + ", ".join(conflicts))
        self._detail.update(Text("\n".join(lines), style="dim"))
        self._detail.display = bool(lines)

    def set_standard_options(self, options: list[dict[str, Any]]) -> None:
        self.extension_mode = False
        self.can_write = True
        self._plugin_bindings = []
        self._snapshot = None
        previous = self.current_binding_id()
        self._summary = {}
        self._order = []
        self._options = {}
        self._list.clear_options()
        self._show_detail(None)
        for option in options:
            option_id = str(option.get("id", ""))
            if not option_id:
                continue
            self._order.append(option_id)
            self._options[option_id] = option
            name = option.get("name") or option_id
            value = config_option_value(option)
            self._list.add_option(Option(f"{name}: <{value}>", id=option_id))
        self._status.update(
            Text(
                "Standard ACP configuration only. This agent does not advertise the richer "
                "capability extension.",
                style="dim",
            )
        )
        self._hint.update("←/→ change · Enter chooser · r refresh · Esc close")
        if self._list.option_count:
            self._list.highlighted = self._highlight_index(previous)
        else:
            self.show_message("This agent advertises no configuration options.")

    def _highlight_index(self, previous: str | None) -> int:
        """Keep the edited row selected instead of jumping back to the top."""
        if previous and previous in self._order:
            return self._order.index(previous)
        return 0

    def _name_column(self) -> int:
        """Width of the name column, so toggles and descriptions line up."""
        longest = max(
            (len(str(self._summary[binding_id]["name"])) for binding_id in self._order),
            default=0,
        )
        # Keep at least a sliver of description visible on narrow drawers.
        cap = max(6, self._row_chars - 24)
        return max(6, min(longest, cap, 24))

    def _row_text(self, summary: dict[str, Any]) -> str:
        enabled = "on" if summary["enabled"] else "off"
        name = truncate_text(str(summary["name"]), self._name_chars)
        toggle = f"<{enabled}>"
        prefix = f"{name:<{self._name_chars}}  {toggle:<5}  "
        tail: list[str] = []
        if summary["locked"]:
            tail.append("locked")
        elif summary.get("pending"):
            active = summary.get("state") or {}
            active_on = (
                active.get("enabled") if isinstance(active, dict) else summary.get("activeEnabled")
            )
            if active_on is not None and bool(active_on) != summary["enabled"]:
                tail.append("next turn")
        if summary["changeMode"] == "new_session":
            tail.append("new session")
        description = " ".join(str(summary.get("description") or "").split())
        # The description starts in its own column and is clipped to whatever room is
        # left; selecting the row shows the full text in the detail line.
        room = self._row_chars - len(prefix) - sum(len(bit) + 3 for bit in tail)
        description = truncate_text(description, room) if room >= 8 else ""
        suffix = "".join(f"   {bit}" for bit in tail)
        head = f"{name:<{self._name_chars}}  {toggle:<5}"
        if description:
            head += f"  {description}"
        return f"{head}{suffix}".rstrip()

    # --------------------------------------------------------------- events

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.extension_mode:
            self._show_detail(event.option_id)

    def on_resize(self, event: events.Resize) -> None:
        """Re-clip descriptions when the drawer changes width."""
        if not self.extension_mode or not self._plugin_bindings:
            return
        width = self._row_width()
        if width and width != self._row_chars:
            self._render_rows()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id
        if option_id is None:
            return
        if not self.extension_mode:
            self.post_message(ConfigOptionRequested(option_id))
            return
        self._toggle_binding(option_id)

    def on_key(self, event: Any) -> None:
        """Left/Right change the highlighted capability or standard option."""
        direction = choice_direction(str(event.key))
        if direction is None or not self._list.has_focus:
            return
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        event.stop()
        if self.extension_mode:
            self._toggle_binding(binding_id)
            return
        option = self._options.get(binding_id) or {}
        if option.get("type") == "boolean":
            self.post_message(ConfigOptionRequested(binding_id))
        else:
            self.post_message(ConfigOptionStepRequested(binding_id, direction))

    def _toggle_binding(self, binding_id: str) -> None:
        summary = self._summary.get(binding_id)
        if summary is None:
            return
        if not self.can_write:
            self.show_message("This host advertises a read-only plugin catalog.")
            return
        if summary["locked"]:
            self.show_message(f"{summary['name']} is locked: {summary['reason'] or 'unavailable'}")
            return
        self.post_message(BindingToggleRequested(binding_id, not summary["enabled"]))

    def action_refresh(self) -> None:
        self.post_message(CapabilityRefreshRequested())

    def action_edit(self) -> None:
        if self.extension_mode and not self.can_write:
            self.show_message("This host advertises a read-only plugin catalog.")
            return
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        summary = self._summary.get(binding_id)
        if summary and summary["editableFields"]:
            self.post_message(BindingEditRequested(binding_id))
        elif summary:
            self.show_message(f"{summary['name']} has no editable fields.")

    def action_add(self) -> None:
        if self.extension_mode and not self.can_write:
            self.show_message("This host advertises a read-only plugin catalog.")
            return
        self.post_message(CapabilityAddRequested())

    def action_toggle_binding(self) -> None:
        if self.extension_mode and not self.can_write:
            self.show_message("This host advertises a read-only plugin catalog.")
            return
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        if not self.extension_mode:
            self.post_message(ConfigOptionRequested(binding_id))
            return
        self._toggle_binding(binding_id)

    def current_binding_id(self) -> str | None:
        index = self._list.highlighted
        if index is None or index < 0 or index >= len(self._order):
            return None
        return self._order[index]

    def binding(self, binding_id: str) -> dict[str, Any] | None:
        return self._summary.get(binding_id)

    def option(self, option_id: str) -> dict[str, Any] | None:
        return self._options.get(option_id)
