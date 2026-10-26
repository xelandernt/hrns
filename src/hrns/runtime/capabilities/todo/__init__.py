"""The bundled Todo capability: upstream plan tools plus a session-scoped store.

The descriptor is metadata only. The factory binds the upstream planning tools to the
host's ``plan_events`` resource, which owns the per-session store and mirrors every
mutation to the connected ACP client.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AgentCapability

from hrns.protocol import CapabilityPolicy

from ...registry import BindingContext, PluginDescriptor
from .config import TodoConfig

__all__ = ["Todo", "TodoConfig", "build_todo", "descriptor"]

_PLUGIN_ID = "hrns.todo"


def build_todo(config: TodoConfig, binding: BindingContext) -> AgentCapability:
    """Lazy factory reference; imports the planning capability only when composed."""
    from ._capability import build_todo as _build

    return _build(config, binding)


def descriptor() -> PluginDescriptor[TodoConfig]:
    """Metadata for the bundled Todo capability."""
    return PluginDescriptor(
        plugin_id=_PLUGIN_ID,
        version="0.1.0",
        description=(
            "A model-maintained plan for multi-step work, with live progress in the "
            "terminal's Todos panel."
        ),
        config_model=TodoConfig,
        factory="hrns.runtime.capabilities.todo:build_todo",
        state_mode="run",
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_toggle=True,
            change_mode="next_turn",
        ),
    )


def __getattr__(name: str) -> Any:
    """Expose ``Todo`` without importing the planning capability at discovery time."""
    if name == "Todo":
        from ._capability import Todo

        return Todo
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
