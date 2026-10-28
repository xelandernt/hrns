"""Capability/plugin panel and its events."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..choice_keys import choice_direction
from ..formatting import truncate_text
from ..state import binding_summary, config_option_value


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
        self.accepts_client_capabilities = False
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
        accepts_client_capabilities: bool = False,
    ) -> None:
        self.extension_mode = True
        self.can_write = can_write
        self.accepts_client_capabilities = accepts_client_capabilities
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
            if summary["origin"] == "client" and not self.accepts_client_capabilities:
                summary.update(
                    available=False,
                    locked=True,
                    reason="the server does not accept client capabilities",
                )
            binding_id = summary["bindingId"]
            if not binding_id:
                continue
            self._summary[binding_id] = summary
            self._order.append(binding_id)
            self._options[binding_id] = raw
        self._name_chars = self._name_column()
        for origin, title in (("host", "Server plugins"), ("client", "Client plugins")):
            self._list.add_option(Option(Text(title, style="bold"), disabled=True))
            group = [
                binding_id
                for binding_id in self._order
                if self._summary[binding_id]["origin"] == origin
            ]
            for binding_id in group:
                summary = self._summary[binding_id]
                self._list.add_option(
                    Option(
                        Text(self._row_text(summary), style="dim" if summary["locked"] else ""),
                        id=binding_id,
                    )
                )
            if not group:
                self._list.add_option(Option(Text("None offered", style="dim"), disabled=True))
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
        if not self.accepts_client_capabilities:
            notes.append("client capabilities unavailable")
        if not self.can_write:
            notes.append("read-only")
        self._status.update(Text(" · ".join(notes), style="dim"))
        self._hint.update(
            "read-only · r refresh · Esc close"
            if not self.can_write
            else "e edit · Space toggle · a add · r refresh · Esc close"
        )
        if self._order:
            self._list.highlighted = self._highlight_index(previous)
            self._show_detail(self.current_binding_id())
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
            return self._list.get_option_index(previous)
        return self._list.get_option_index(self._order[0]) if self._order else 0

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
        elif not summary["canToggle"]:
            tail.append("toggle locked")
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
        if not summary["canToggle"]:
            self.show_message(f"{summary['name']} cannot be toggled from this client.")
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
        if summary and summary["locked"]:
            self.show_message(f"{summary['name']} is locked: {summary['reason'] or 'unavailable'}")
            return
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
        option = self._list.highlighted_option
        return option.id if option is not None else None

    def binding(self, binding_id: str) -> dict[str, Any] | None:
        return self._summary.get(binding_id)

    def option(self, option_id: str) -> dict[str, Any] | None:
        return self._options.get(option_id)
