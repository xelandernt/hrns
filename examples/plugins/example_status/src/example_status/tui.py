"""Optional local Textual views for the example plugin.

Nothing in this module is imported by the runtime descriptor provider or the protocol
package. The terminal that loads these widgets ships with hrns.
"""

from __future__ import annotations

from textual.widgets import Static

from hrns.client.tui.extensions import CommandViewState, ToolViewState, ViewContext, ViewHandle


class _StaticView:
    def __init__(self, widget: Static) -> None:
        self.widget = widget

    def apply(self, state: str) -> None:
        self.widget.update(state)


def _command_factory(state: CommandViewState, context: ViewContext) -> ViewHandle:
    del context
    widget = Static(state.fallback)
    handle = _StaticView(widget)
    return ViewHandle(widget=widget, apply=handle.apply)


def _tool_factory(state: ToolViewState, context: ViewContext) -> ViewHandle:
    del context
    output = (state.output or {}).get("data") or {}
    text = str(output.get("text") or state.fallback)
    widget = Static(f"echo body: {text}" if text else "echo")
    view = _StaticView(widget)
    return ViewHandle(widget=widget, apply=view.apply)


class Provider:
    command_view = staticmethod(_command_factory)
    tool_view = staticmethod(_tool_factory)


provider = Provider()
