"""Agent Client Protocol transport, registered sessions, and configuration types."""

from . import updates
from .adapter import PydanticAIACPAgent
from .content import PromptContentBlock, prompt_blocks_to_user_content
from .host import AcpServer, RegisteredAgent, Session, TurnHandle, TurnResult
from .mcp import acp_mcp_toolsets
from .models import (
    ModelChoice,
    ModelResolver,
    ProviderModelResolver,
    ResolvedModel,
    SettingChoice,
    SettingOption,
)
from .permission import (
    PermissionPolicy,
    ToolCallPermission,
    default_permission_scope,
)
from .presentation import (
    ToolCallContent,
    ToolCallPresentation,
    ToolCallPresenter,
    absolutize,
    chain_presenters,
    default_coding_presenter,
)
from .providers import (
    InMemoryProviderRegistry,
    ProviderConfig,
    ProviderDescriptor,
    ProviderDisabled,
    ProviderRegistry,
    ProviderRoute,
)
from .serialize import bounded_jsonable, chunk_text, jsonable
from .server import run_acp_stdio
from .session import (
    AcpAttachmentConfig,
    AcpSession,
    AcpSessionConfig,
    AttachmentConfigFunc,
    DocumentState,
    McpServer,
    McpServers,
    SessionConfigFunc,
    SessionContext,
    SessionUpdate,
    apply_content_changes,
)
from .store import (
    DeletableSessionStore,
    InMemorySessionStore,
    ListableSessionStore,
    SessionStore,
    SessionSummary,
    StoredSession,
)
from .toolsets import (
    AcpFileSystemToolset,
    AcpTerminalToolset,
    acp_filesystem,
    acp_terminal,
)

__all__ = [
    "AcpFileSystemToolset",
    "AcpAttachmentConfig",
    "AcpSession",
    "AcpSessionConfig",
    "AcpTerminalToolset",
    "DeletableSessionStore",
    "DocumentState",
    "InMemoryProviderRegistry",
    "InMemorySessionStore",
    "ListableSessionStore",
    "McpServer",
    "McpServers",
    "ModelChoice",
    "ModelResolver",
    "PermissionPolicy",
    "PromptContentBlock",
    "ProviderConfig",
    "ProviderDescriptor",
    "ProviderDisabled",
    "ProviderModelResolver",
    "ProviderRegistry",
    "ProviderRoute",
    "PydanticAIACPAgent",
    "ResolvedModel",
    "SessionConfigFunc",
    "SessionStore",
    "SessionSummary",
    "SessionUpdate",
    "SettingChoice",
    "SettingOption",
    "StoredSession",
    "ToolCallContent",
    "ToolCallPermission",
    "ToolCallPresentation",
    "ToolCallPresenter",
    "absolutize",
    "acp_filesystem",
    "acp_mcp_toolsets",
    "acp_terminal",
    "apply_content_changes",
    "bounded_jsonable",
    "chain_presenters",
    "chunk_text",
    "AcpServer",
    "RegisteredAgent",
    "Session",
    "TurnHandle",
    "TurnResult",
    "AttachmentConfigFunc",
    "SessionContext",
    "default_coding_presenter",
    "default_permission_scope",
    "jsonable",
    "prompt_blocks_to_user_content",
    "run_acp_stdio",
    "updates",
]
