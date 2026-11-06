"""The standalone hrns client: ACP terminal and headless runner.

This module is independent of ``hrns.app``. It speaks standard ACP v1 over
stdio or a WebSocket. The UI is Textual; the protocol layer is ``acp``.
"""

from hrns.config import HrnsConfig, Profile, default_config_path

from ._version import __version__
from .client import HrnsAcpClient
from .errors import (
    ConnectionLost,
    ExtensionUnavailable,
    HrnsClientError,
    InteractionRejected,
    InteractionRequired,
    ProtocolError,
    SecretMissing,
    TransportError,
)
from .events import (
    AgentTextChunk,
    CommandsUpdated,
    ConfigOptionsUpdated,
    ConnectionEvent,
    ModesUpdated,
    PlanChanged,
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
from .renderers import RendererRegistry, readable_text
from .session import SessionProxy
from .transport import StdioAgentTransport, WebSocketAgentTransport, secret_value

__all__ = [
    "__version__",
    "AgentTextChunk",
    "CommandsUpdated",
    "ConfigOptionsUpdated",
    "ConnectionEvent",
    "ConnectionLost",
    "ElicitationAnswer",
    "ElicitationRequest",
    "ExtensionUnavailable",
    "HrnsAcpClient",
    "HrnsClientError",
    "HeadlessInteractions",
    "InteractionPolicy",
    "InteractionRejected",
    "InteractionRequired",
    "ModesUpdated",
    "PERMISSION_MODES",
    "PermissionDecision",
    "PermissionRequest",
    "PlanChanged",
    "HrnsConfig",
    "Profile",
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
    "WebSocketAgentTransport",
    "auto_permission",
    "default_config_path",
    "readable_text",
    "secret_value",
]
