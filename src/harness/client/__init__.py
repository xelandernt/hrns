"""The standalone harness client: ACP terminal, headless runner, bridge.

This module does not import ``harness.runtime``. It speaks ACP v1
with third-party agents over stdio and with project hosts over the versioned
WebSocket transport. The UI is Textual; the network layer is ``acp``,
``websockets`` and ``httpx``.
"""

from ._version import __version__
from .catalog import AgentDeployment, AgentPage, CatalogClient
from .client import HarnessAcpClient, SessionProxy
from .errors import (
    CatalogError,
    ConnectionLost,
    ExtensionUnavailable,
    HarnessClientError,
    InteractionRejected,
    InteractionRequired,
    ProtocolError,
    SecretMissing,
    TransportError,
)
from .events import (
    AgentTextChunk,
    CapabilitiesListed,
    CommandsUpdated,
    ConfigOptionsUpdated,
    ConfigurationChanged,
    ConnectionEvent,
    ModesUpdated,
    PlanChanged,
    PluginListed,
    SessionsListed,
    ToolEvent,
    TurnCompleted,
    TurnStarted,
    UsageUpdated,
    UserMessageReplayed,
)
from .interactions import (
    PERMISSION_MODES,
    READ_ONLY_KINDS,
    ElicitationAnswer,
    ElicitationRequest,
    HeadlessInteractions,
    InteractionPolicy,
    PermissionDecision,
    PermissionRequest,
    RequireInteractions,
    auto_permission,
)
from .profiles import Profile, Profiles, default_profiles_path, secret_value
from .renderers import RendererRegistry, readable_text
from .transport import StdioAgentTransport, WebSocketAgentTransport

__all__ = [
    "__version__",
    "AgentDeployment",
    "AgentPage",
    "AgentTextChunk",
    "CapabilitiesListed",
    "CatalogClient",
    "CatalogError",
    "CommandsUpdated",
    "ConfigOptionsUpdated",
    "ConfigurationChanged",
    "ConnectionEvent",
    "ConnectionLost",
    "ElicitationAnswer",
    "ElicitationRequest",
    "ExtensionUnavailable",
    "HarnessAcpClient",
    "HarnessClientError",
    "HeadlessInteractions",
    "InteractionPolicy",
    "InteractionRejected",
    "InteractionRequired",
    "ModesUpdated",
    "PERMISSION_MODES",
    "PermissionDecision",
    "PermissionRequest",
    "PlanChanged",
    "PluginListed",
    "Profile",
    "Profiles",
    "ProtocolError",
    "READ_ONLY_KINDS",
    "RendererRegistry",
    "RequireInteractions",
    "SecretMissing",
    "SessionProxy",
    "SessionsListed",
    "StdioAgentTransport",
    "ToolEvent",
    "TransportError",
    "TurnCompleted",
    "TurnStarted",
    "UsageUpdated",
    "UserMessageReplayed",
    "auto_permission",
    "WebSocketAgentTransport",
    "default_profiles_path",
    "readable_text",
    "secret_value",
]
