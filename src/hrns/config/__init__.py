"""Local agent configuration shared by the independent client and runtime."""

from .mcp import McpOAuthConfig, McpServerConfig, McpServerSettings, McpSettings
from .settings import (
    EFFORT_DEFAULT,
    EFFORT_OFF,
    THINKING_LEVELS,
    CapabilitiesSettings,
    EffortLevel,
    LocalSettings,
    default_local_config_path,
    thinking_from_settings,
    with_thinking,
)

__all__ = [
    "EFFORT_DEFAULT",
    "EFFORT_OFF",
    "THINKING_LEVELS",
    "CapabilitiesSettings",
    "EffortLevel",
    "LocalSettings",
    "McpOAuthConfig",
    "McpServerConfig",
    "McpServerSettings",
    "McpSettings",
    "default_local_config_path",
    "thinking_from_settings",
    "with_thinking",
]
