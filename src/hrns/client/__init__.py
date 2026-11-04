"""The standalone hrns client: ACP terminal and headless runner.

This module is independent of ``hrns.app``. It speaks standard ACP v1 over
stdio. The UI is Textual; the protocol layer is ``acp``.
"""

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
    AskUserAnswer,
    AskUserOption,
    AskUserQuestion,
    AskUserRequest,
    AskUserResponse,
    ElicitationAnswer,
    ElicitationRequest,
    HeadlessInteractions,
    InteractionPolicy,
    PermissionDecision,
    PermissionRequest,
    RequireInteractions,
    ask_user_payload,
    auto_permission,
    parse_ask_user_request,
)
from .profiles import Profile, Profiles, default_profiles_path, secret_value
from .renderers import RendererRegistry, readable_text
from .session import SessionProxy
from .transport import StdioAgentTransport

__all__ = [
    "__version__",
    "AgentTextChunk",
    "AskUserAnswer",
    "AskUserOption",
    "AskUserQuestion",
    "AskUserRequest",
    "AskUserResponse",
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
    "ask_user_payload",
    "auto_permission",
    "parse_ask_user_request",
    "default_profiles_path",
    "readable_text",
    "secret_value",
]
