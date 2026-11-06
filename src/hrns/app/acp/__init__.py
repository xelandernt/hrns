"""Our own Agent Client Protocol adapter for Pydantic AI agents.

The public surface mirrors what ``hrns.app`` previously imported from
``pydantic_ai_harness.experimental.acp``, but the implementation lives here and targets
ACP 1.x directly.
"""

from . import updates
from .adapter import PydanticAIACPAgent
from .content import PromptContentBlock, prompt_blocks_to_user_content
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
from .serialize import bounded_jsonable, chunk_text, jsonable
from .server import ACPModels, create_acp_agent, run_acp_stdio
from .session import (
    AcpSession,
    AcpSessionConfig,
    DocumentState,
    McpServer,
    McpServers,
    SessionConfigFunc,
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
    "ACPModels",
    "AcpFileSystemToolset",
    "AcpSession",
    "AcpSessionConfig",
    "AcpTerminalToolset",
    "DeletableSessionStore",
    "DocumentState",
    "InMemorySessionStore",
    "ListableSessionStore",
    "McpServer",
    "McpServers",
    "PermissionPolicy",
    "PromptContentBlock",
    "PydanticAIACPAgent",
    "SessionConfigFunc",
    "SessionStore",
    "SessionSummary",
    "SessionUpdate",
    "StoredSession",
    "ToolCallContent",
    "ToolCallPermission",
    "ToolCallPresentation",
    "ToolCallPresenter",
    "absolutize",
    "acp_filesystem",
    "acp_terminal",
    "apply_content_changes",
    "bounded_jsonable",
    "chain_presenters",
    "chunk_text",
    "create_acp_agent",
    "default_coding_presenter",
    "default_permission_scope",
    "jsonable",
    "prompt_blocks_to_user_content",
    "run_acp_stdio",
    "updates",
]
