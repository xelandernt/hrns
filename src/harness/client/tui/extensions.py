"""Typed local view contracts for plugin command and tool widgets.

Plugins return ordinary Textual widgets. Harness mounts the widget inline, applies immutable
state snapshots to the same instance, and lets native Textual unmounting own teardown. These
classes are UI contracts, not serialized protocol models.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from textual.widget import Widget

__all__ = [
    "CommandViewState",
    "ToolViewState",
    "ViewContext",
    "ViewHandle",
    "build_view",
]

StateT = TypeVar("StateT")


@dataclass(frozen=True, slots=True)
class CommandViewState:
    """Immutable state for one inline command-result view."""

    session_id: str
    binding_id: str
    command_name: str
    presentation: dict[str, Any] | None = None
    fallback: str = ""
    live: bool = True
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolViewState:
    """Immutable merged state for one inline tool body."""

    session_id: str
    tool_call_id: str
    title: str = ""
    kind: str = ""
    status: str = ""
    raw_input: Any = None
    raw_output: Any = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    fallback: str = ""
    revision: int = 0
    live: bool = True
    binding_id: str | None = None


@dataclass(frozen=True, slots=True)
class ViewHandle(Generic[StateT]):
    """An ordinary Textual widget plus a synchronous snapshot applier."""

    widget: Widget
    apply: Callable[[StateT], None]


class ViewContext:
    """Small context for explicit widget actions.

    It can submit a structured plugin command through the same host route as slash
    invocation. It cannot execute arbitrary agent tools or reach the App/Screen internals.
    """

    def __init__(
        self,
        *,
        session_id: str,
        invoke: Callable[[str, str, dict[str, Any] | None, str | None], Awaitable[Any]],
    ) -> None:
        self.session_id = session_id
        self._invoke = invoke

    async def invoke_command(
        self,
        binding_id: str,
        command_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        text: str | None = None,
    ) -> Any:
        """Submit an explicit user command; mounting or replaying never calls this."""
        if arguments is not None and text is not None:
            raise ValueError("provide structured arguments or raw text, not both")
        return await self._invoke(binding_id, command_name, arguments, text)


def build_view(
    registry: Any,
    presentation: Any,
    kind: str,
    state: Any,
    context: ViewContext,
) -> ViewHandle | None:
    """Instantiate one local provider view, returning ``None`` for normal fallback.

    Loader/factory failures are contained here: the caller keeps its readable fallback and
    never retries with the other view kind.
    """
    if registry is None or presentation is None:
        return None
    try:
        renderer_id = (
            presentation.get("rendererId")
            if isinstance(presentation, dict)
            else presentation.renderer_id
        )
        version = (
            presentation.get("schemaVersion")
            if isinstance(presentation, dict)
            else presentation.schema_version
        )
        factory = registry.factory(str(renderer_id), int(version or 0), kind)
        if factory is None:
            return None
        handle = factory(state, context)
    except Exception:
        return None
    widget = getattr(handle, "widget", None)
    apply = getattr(handle, "apply", None)
    if not isinstance(widget, Widget) or not callable(apply):
        return None
    return ViewHandle(widget=widget, apply=apply)
