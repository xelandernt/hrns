"""Bundled coding capability, registered by every Harness host.

The descriptor is metadata only: configuration is a plain Pydantic model, and the factory is
an import reference so the tool implementations load on first composition. The presentation
identifiers listed here are the ones :mod:`harness.client.tui.tool_views` implements.
"""

from __future__ import annotations

from pydantic_ai.capabilities import AgentCapability

from harness.protocol import CapabilityPolicy

from ...registry import BindingContext, PluginDescriptor
from ._ids import RENDERER_IDS
from ._needs import NEEDS
from .config import CoderConfig

__all__ = ["Coder", "CoderConfig", "build_coder", "descriptor"]

_PLUGIN_ID = "harness.coder"


def build_coder(config: CoderConfig, binding: BindingContext) -> AgentCapability:
    """Lazy factory reference; imports the capability module only when composed."""
    from ._capability import build_coder as _build

    return _build(config, binding)


def descriptor() -> PluginDescriptor[CoderConfig]:
    """Metadata for the bundled coding capability."""
    return PluginDescriptor(
        plugin_id=_PLUGIN_ID,
        version="0.1.0",
        description=(
            "Coding agent for the run workspace: file tools, search, and a shell, with "
            "hash-checked edits and bounded context."
        ),
        config_model=CoderConfig,
        factory="harness.runtime.capabilities.coder:build_coder",
        needs=NEEDS,
        state_mode="run",
        presentation=list(RENDERER_IDS),
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_toggle=True,
            change_mode="next_turn",
        ),
    )


def __getattr__(name: str) -> object:
    """Expose ``Coder`` without importing the tool implementations at discovery time."""
    if name == "Coder":
        from ._capability import Coder

        return Coder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
