"""The client-side counterpart of the coder's ``shell``: commands run in the ACP client.

The bundled coder capability runs commands in the host workspace. This plugin is offered
separately and marked ``origin="client"``: enabling it asks the connected client to run the
command through its terminal lifecycle, so output, cancellation, and the environment belong to
the editor instead of this host. It is available only while the client advertises terminal
support.
"""

from __future__ import annotations

from pydantic_ai.capabilities import AgentCapability

from hrns.protocol import TERMINAL, CapabilityPolicy

from ...registry import BindingContext, PluginDescriptor
from ...resources import CLIENT

__all__ = ["build_client_terminal", "descriptor"]

_PLUGIN_ID = "hrns.client_terminal"
_NEEDS_CLIENT: tuple[str, ...] = (TERMINAL,)


def build_client_terminal(_config: dict[str, object], binding: BindingContext) -> AgentCapability:
    """Lazy factory reference; imports the ACP toolset only when composed."""
    from ._capability import build_client_terminal as _build

    return _build(binding)


def descriptor() -> PluginDescriptor:
    """Metadata for terminal commands routed through the connected client."""
    return PluginDescriptor(
        plugin_id=_PLUGIN_ID,
        version="0.1.0",
        description=(
            "Run the agent's commands in the connected ACP client's terminal instead of this "
            "host. Needs a client that advertises terminal support."
        ),
        factory="hrns.runtime.capabilities.client_terminal:build_client_terminal",
        needs=(CLIENT,),
        origin="client",
        requires_client=_NEEDS_CLIENT,
        shadows=("shell",),
        state_mode="run",
        presentation=["hrns.tool.shell"],
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=False,
            client_can_toggle=True,
            change_mode="next_turn",
        ),
    )
