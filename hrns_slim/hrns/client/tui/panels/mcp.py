"""MCP server health panel."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ...mcp_provider import McpServerHealth


class McpRefreshRequested(Message):
    """The user asked to refresh the MCP health view."""


class McpPanel(Vertical, can_focus=True):
    """Read-only health of every configured MCP server.

    The panel is a live snapshot: ``ChatScreen._refresh_mcp_status`` updates it
    after a connection, a session change, or a provider failure.
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="mcp-panel")
        self._status = Static("", markup=False, classes="panel-status")
        self._body = Static("", markup=False, classes="panel-detail")
        self._hint = Static(
            "r refresh · Esc closes the panel",
            markup=False,
            classes="panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._hint
        with VerticalScroll():
            yield self._body

    def set_health(
        self,
        health: Sequence[McpServerHealth],
        *,
        connected: bool,
        acp: bool,
    ) -> None:
        running = sum(1 for item in health if item.state == "running")
        failed = sum(1 for item in health if item.state == "failed")
        connection = "connected" if connected else "disconnected"
        self._status.update(
            f"{len(health)} configured · {running} running · {failed} failed · "
            f"{connection} · agent ACP: {'yes' if acp else 'no'}"
        )
        if not health:
            self._body.update(
                Text(
                    "No MCP servers configured. Add one with "
                    "`hrns mcp add NAME -- npx -y PACKAGE`.",
                    style="dim",
                )
            )
            return
        text = Text()
        for item in health:
            failed_item = item.state == "failed"
            text.append(item.name, style="bold")
            text.append(
                f"  {item.state}",
                style="bold red" if failed_item else "dim",
            )
            text.append(f"  {item.transport}\n", style="dim")
            if item.protocol:
                text.append(f"  protocol {item.protocol}\n", style="dim")
            text.append(f"  {item.target}\n", style="dim")
            if item.detail:
                text.append(
                    f"  {item.detail}\n",
                    style="yellow" if failed_item else "dim",
                )
            text.append("\n")
        self._body.update(text)

    def action_refresh(self) -> None:
        self.post_message(McpRefreshRequested())
