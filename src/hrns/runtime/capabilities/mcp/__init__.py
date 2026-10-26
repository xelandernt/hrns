"""Bundled MCP servers: one capability binding per configured server.

Configuration lives in ``hrns.local.toml`` under ``[mcp.servers.<name>]`` and is managed
by ``hrns mcp``; this plugin only turns one validated server entry into a Pydantic AI
``MCP`` capability. The descriptor stays metadata-only so discovery never imports FastMCP.
"""

from __future__ import annotations

from pydantic_ai.capabilities import AgentCapability

from hrns.protocol import CapabilityPolicy
from hrns.protocol.extensions import CommandResult

from ...extensions import Command, CommandContext, TextArguments
from ...registry import BindingContext, CompatibilityConstraints, PluginDescriptor
from .config import McpServerConfig

__all__ = ["McpServerConfig", "build_mcp_server", "descriptor"]

_PLUGIN_ID = "hrns.mcp"


def build_mcp_server(config: McpServerConfig, binding: BindingContext) -> AgentCapability:
    """Lazy factory reference; imports FastMCP only when a server binding composes."""
    from ._capability import build_mcp_server as _build

    return _build(config, binding)


async def _status_handler(context: CommandContext, arguments: TextArguments) -> CommandResult:
    """Lazy command handler; probing a server imports FastMCP on first invocation."""
    from ._status import status_command

    return await status_command(context, arguments)


def descriptor() -> PluginDescriptor[McpServerConfig]:
    """Metadata for the bundled MCP server capability."""
    return PluginDescriptor(
        plugin_id=_PLUGIN_ID,
        version="0.1.0",
        description=(
            "Model Context Protocol servers configured with `hrns mcp`: stdio commands and "
            "streamable HTTP endpoints, with tools exposed to the local agent."
        ),
        config_model=McpServerConfig,
        factory="hrns.runtime.capabilities.mcp:build_mcp_server",
        commands=[
            Command(
                name="status",
                handler=_status_handler,
                description="Connect to this MCP server and list its tools.",
            )
        ],
        state_mode="run",
        compatibility=CompatibilityConstraints(requires_extras=["fastmcp"]),
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=False,
            client_can_toggle=True,
            change_mode="next_turn",
        ),
    )
