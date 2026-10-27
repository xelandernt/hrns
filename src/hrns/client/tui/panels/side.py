"""Side panel assembly."""

from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import TabbedContent, TabPane

from .activity import ActivityPanel
from .capability import CapabilityPanel
from .mcp import McpPanel
from .session import SessionPanel
from .todo import TodoPanel


class SidePanelCloseRequested(Message):
    """The user asked to dismiss the on-demand side panel."""


class SidebarResizeRequested(Message):
    """Move the sidebar divider by ``delta`` columns."""

    def __init__(self, delta: int, *, final: bool) -> None:
        super().__init__()
        self.delta = delta
        self.final = final


class SidebarResizeHandle(Widget, can_focus=True):
    """Mouse- and keyboard-accessible divider for the docked sidebar."""

    BINDINGS = [
        Binding("left", "resize_left", "Widen sidebar", show=False),
        Binding("right", "resize_right", "Narrow sidebar", show=False),
        Binding("shift+left", "resize_left_large", "Widen sidebar", show=False),
        Binding("shift+right", "resize_right_large", "Narrow sidebar", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(name="Resize sidebar", id="side-resize")
        self.tooltip = "Drag to resize · arrow keys adjust width"
        self._drag_screen_x: float | None = None

    def render(self) -> str:
        return "<"

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        self._drag_screen_x = event.screen_x
        self.capture_mouse()
        self.add_class("dragging")
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_screen_x is None:
            return
        delta = round(self._drag_screen_x - event.screen_x)
        if delta:
            self._drag_screen_x = event.screen_x
            self.post_message(SidebarResizeRequested(delta, final=False))
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button != 1 or self._drag_screen_x is None:
            return
        self._drag_screen_x = None
        self.release_mouse()
        self.remove_class("dragging")
        self.post_message(SidebarResizeRequested(0, final=True))
        event.stop()

    def action_resize_left(self) -> None:
        self.post_message(SidebarResizeRequested(2, final=True))

    def action_resize_right(self) -> None:
        self.post_message(SidebarResizeRequested(-2, final=True))

    def action_resize_left_large(self) -> None:
        self.post_message(SidebarResizeRequested(8, final=True))

    def action_resize_right_large(self) -> None:
        self.post_message(SidebarResizeRequested(-8, final=True))


class SidePanel(TabbedContent):
    """Todos, plugins, MCP servers, sessions and activity in a responsive pane."""

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
