"""Value types for the session document.

These are the transport-neutral records stored in a session's Loro document.
All timestamp fields are Unix milliseconds. All text offsets in the document
contract are Unicode code points; clients that index by UTF-16 convert at their
boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hrns.server.capabilities import TodoItem

SCHEMA_VERSION = 1
"""The only document schema version this server reads or writes."""

AuthorKind = Literal["python", "acp", "loro", "agent"]
"""Where a contribution entered the system. Not an authentication claim."""

ContributionState = Literal["draft", "ready", "captured", "delivered", "cancelled", "failed"]
"""The acceptance/consumption stages of one contribution."""

ConsumptionState = Literal["captured", "delivered", "failed"]
"""The native-delivery stages of a captured contribution."""

InteractionKind = Literal["approval", "question"]
InteractionState = Literal["pending", "resolved", "cancelled", "obsolete"]

DisplayKind = Literal[
    "user",
    "assistant",
    "thinking",
    "tool_call",
    "plan",
    "usage",
    "title",
    "error",
    "skill",
]
"""Display-only records. They never substitute for native model history."""

LifecycleStatus = Literal["idle", "running", "waiting", "paused", "closed", "interrupted"]


class Author(BaseModel):
    """Application-owned actor description; the label is not authenticated."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    label: str = ""
    kind: AuthorKind = "loro"


class Contribution(BaseModel):
    """One pending-input contribution, frozen as read at a capture boundary."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    author: Author
    state: ContributionState
    text: str = ""
    blocks: tuple[dict[str, object], ...] = ()
    origin: str = "python"
    created_at: int = 0
    #: Server-assigned arrival order; client clocks never order execution.
    received_seq: int | None = None
    #: Server-assigned consumption order, frozen together with the content.
    consumed_seq: int | None = None
    #: Opaque version token (base64 frontiers) captured with the content.
    capture_version: str | None = None


class Consumption(BaseModel):
    """The durable record of what one model request received and when."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    seq: int
    contribution_id: str
    author: Author
    captured_at: int
    version: str
    content: str
    state: ConsumptionState = "captured"
    outcome: str | None = None


class DisplayRecord(BaseModel):
    """A display-only projection of one live or completed item."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    kind: DisplayKind
    seq: int
    status: str = ""
    text: str = ""
    payload: dict[str, object] = Field(default_factory=dict)


class InteractionRecord(BaseModel):
    """One session-owned question or approval awaiting exactly one answer."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    kind: InteractionKind
    state: InteractionState
    request: dict[str, object]
    created_at: int = 0
    response: dict[str, object] | None = None
    resolved_by: str | None = None
    detail: str | None = None


class SessionMetadata(BaseModel):
    """Identity and schema fields stored at the document root."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: int
    agent_id: str
    session_id: str
    cwd: str
    created_at: int = 0
    updated_at: int = 0


class Lifecycle(BaseModel):
    """Durable execution state; live activity still comes from the server process."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: LifecycleStatus = "idle"
    revision: int = 0
    active_contribution_id: str | None = None
    last_outcome: str | None = None
    resume_required: bool = False


class DocumentSettings(BaseModel):
    """Session-scoped settings mirrored into the document for every client."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model: str | None = None
    setting_selections: dict[str, str] = Field(default_factory=dict)
    cost: str | None = None
    todos: tuple[TodoItem, ...] = ()
    skills: tuple[str, ...] = ()


class SchemaVersionError(ValueError):
    """The document was written by a schema this server does not support."""
