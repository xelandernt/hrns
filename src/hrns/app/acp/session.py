"""Per-session value types: client setup, run configuration, live state, and documents."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Hashable, Sequence
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeAlias

import anyio
from acp import Client, schema
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef

from hrns.capabilities import TodoStore

# A single MCP server configuration, in any of the transports ACP carries.
McpServer: TypeAlias = (
    schema.HttpMcpServer | schema.SseMcpServer | schema.AcpMcpServer | schema.McpServerStdio
)

# MCP server configuration list, as carried by ACP session methods.
McpServers: TypeAlias = list[McpServer] | None

# A single ``session/update`` payload. The adapter records these per session so
# ``session/load`` can replay them verbatim.
SessionUpdate: TypeAlias = (
    schema.UserMessageChunk
    | schema.AgentMessageChunk
    | schema.AgentThoughtChunk
    | schema.ToolCallStart
    | schema.ToolCallProgress
    | schema.AgentPlanUpdate
    | schema.AvailableCommandsUpdate
    | schema.CurrentModeUpdate
    | schema.ConfigOptionUpdate
    | schema.SessionInfoUpdate
    | schema.UsageUpdate
)


@dataclass(frozen=True, kw_only=True)
class AcpSession:
    """The setup an ACP client provides when it starts a session."""

    cwd: str
    mcp_servers: list[McpServer]
    client_capabilities: schema.ClientCapabilities | None
    client: Client
    session_id: str


@dataclass(frozen=True, kw_only=True)
class AcpSessionConfig(Generic[AgentDepsT]):
    """Per-session run configuration returned by a ``session_config`` factory."""

    deps: AgentDepsT
    capabilities: Sequence[AbstractCapability[AgentDepsT]] | None = None
    toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None
    workspace: WorkspaceBackend | WorkspaceRef | None = None


class SessionConfigFunc(Protocol[AgentDepsT]):
    """Factory mapping an ACP session's setup to its run configuration; may be sync or async."""

    def __call__(
        self, session: AcpSession, /
    ) -> AcpSessionConfig[AgentDepsT] | Awaitable[AcpSessionConfig[AgentDepsT]]: ...


@dataclass(kw_only=True)
class DocumentState:
    """The client's live view of one open document in a session."""

    uri: str
    language_id: str
    version: int
    text: str
    saved: bool = False
    focus: schema.Position | None = None
    visible_range: schema.Range | None = None


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _position_to_offset(text: str, position: schema.Position) -> int:
    """Convert a zero-based line/character position to a string offset, clamped to the text."""
    starts = _line_starts(text)
    line = min(max(position.line, 0), len(starts) - 1)
    start = starts[line]
    end = starts[line + 1] - 1 if line + 1 < len(starts) else len(text)
    character = min(max(position.character, 0), end - start)
    return start + character


def apply_content_changes(
    text: str, changes: Sequence[schema.TextDocumentContentChangeEvent]
) -> str:
    """Apply ACP content changes to a document's text.

    A change without a range replaces the whole document; otherwise its range is replaced
    in order. Positions outside the document are clamped rather than rejected, matching how
    editors recover from a stale version.
    """
    for change in changes:
        if change.range is None:
            text = change.text
            continue
        start = _position_to_offset(text, change.range.start)
        end = _position_to_offset(text, change.range.end)
        if end < start:
            start, end = end, start
        text = text[:start] + change.text + text[end:]
    return text


@dataclass(kw_only=True)
class SessionState(Generic[AgentDepsT]):
    """All per-session adapter state."""

    session_id: str
    config: AcpSessionConfig[AgentDepsT]
    cwd: str
    history: list[ModelMessage] = field(default_factory=list[ModelMessage])
    transcript: list[SessionUpdate] = field(default_factory=list[SessionUpdate])
    documents: dict[str, DocumentState] = field(default_factory=dict[str, DocumentState])
    # The model the client selected via the ``model`` config option, or ``None`` to use the
    # agent's own model. Applied as a per-run override so the shared agent is never mutated.
    model: str | None = None
    lock: anyio.Lock = field(default_factory=lambda: anyio.Lock(fast_acquire=True))
    active_turn: asyncio.Task[schema.PromptResponse] | None = None
    cancel_requested: bool = False
    todos: TodoStore = field(default_factory=TodoStore)
    always_allow: set[Hashable] = field(default_factory=set[Hashable])
    always_reject: set[Hashable] = field(default_factory=set[Hashable])
