"""Bundled Ask User capability: structured questions in the middle of a run.

The descriptor is metadata only. The upstream ``AskUser`` capability owns the tool
schema and validation; the factory wires it to the ``user_answers`` resource the host
binds to the connected ACP client.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AgentCapability

from harness.protocol import CapabilityPolicy

from ...registry import BindingContext, PluginDescriptor
from ...resources import ANSWERS
from .config import AskUserConfig

__all__ = ["AskUser", "AskUserConfig", "build_ask_user", "descriptor"]

_PLUGIN_ID = "harness.ask_user"


def build_ask_user(config: AskUserConfig, binding: BindingContext) -> AgentCapability:
    """Lazy factory reference; imports the upstream capability only when composed."""
    from ._capability import build_ask_user as _build

    return _build(config, binding)


def descriptor() -> PluginDescriptor[AskUserConfig]:
    """Metadata for the bundled Ask User capability."""
    return PluginDescriptor(
        plugin_id=_PLUGIN_ID,
        version="0.1.0",
        description=(
            "Structured multiple-choice questions the model asks the user mid-run, "
            "answered in the connected client."
        ),
        config_model=AskUserConfig,
        factory="harness.runtime.capabilities.ask_user:build_ask_user",
        needs=(ANSWERS,),
        state_mode="run",
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_toggle=True,
            change_mode="next_turn",
        ),
    )


def __getattr__(name: str) -> Any:
    """Expose ``AskUser`` without importing the upstream capability at discovery time."""
    if name == "AskUser":
        from pydantic_ai_harness.ask_user import AskUser

        return AskUser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
