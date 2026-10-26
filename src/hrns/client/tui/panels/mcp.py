"""MCP server panel and its events."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..state import binding_summary
from .capability import BindingToggleRequested


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
