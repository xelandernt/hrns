"""Local agent configuration shared by the independent client and runtime."""

from .credentials import (
    GITHUB_COPILOT_CLIENT_ID,
    KEYRING_SERVICE,
    keyring_account,
    secret_ref,
    store_api_key,
    store_secret,
    stored_api_key_ref,
    stored_secret,
)
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
    "GITHUB_COPILOT_CLIENT_ID",
    "KEYRING_SERVICE",
    "THINKING_LEVELS",
    "CapabilitiesSettings",
    "EffortLevel",
    "LocalSettings",
    "McpOAuthConfig",
    "McpServerConfig",
    "McpServerSettings",
    "McpSettings",
    "default_local_config_path",
    "keyring_account",
    "secret_ref",
    "store_api_key",
    "store_secret",
    "stored_api_key_ref",
    "stored_secret",
    "thinking_from_settings",
    "with_thinking",
]
