"""Public authoritative session creation and durable reconciliation contracts."""

from __future__ import annotations

from enum import StrEnum

from acp import schema
from pydantic import BaseModel, ConfigDict, Field

from hrns.protocol import BindingChange, HrnsError

from .session_policy import SessionAccess


class InitialSessionPolicy(BaseModel):
    """Server-owned initial authority; never deserialize this from ACP client metadata.

    The caller is trusted embedding code. Client configuration cannot override this
    gate. Metadata is available to authorization and lifecycle hooks, not to tools.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    access: SessionAccess = SessionAccess.PRIVATE
    collaborators: frozenset[str] = frozenset()
    accepts_client_capabilities: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)


class SessionCreateRequest(BaseModel):
    """Complete immutable creation input, retained for recovery and payload matching."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1, max_length=200)
    definition_version: str = Field(min_length=1, max_length=200)
    cwd: str = Field(min_length=1)
    policy: InitialSessionPolicy = Field(default_factory=InitialSessionPolicy)
    initial_configuration: list[BindingChange] = Field(default_factory=list, max_length=100)
    effort: str | None = None
    client_capabilities: schema.ClientCapabilities | None = None


class SessionNewInput(BaseModel):
    """ACP client-origin inputs, deliberately excluding initial server policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cwd: str
    mutation_id: str | None = Field(default=None, min_length=1, max_length=200)
    definition_version: str | None = Field(default=None, min_length=1, max_length=200)
    initial_configuration: list[BindingChange] = Field(default_factory=list, max_length=100)
    effort: str | None = None
    client_capabilities: schema.ClientCapabilities | None = None


class SessionCreationState(StrEnum):
    RESERVED = "reserved"
    CREATING = "creating"
    CLEANUP = "cleanup"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SessionCreationResult(BaseModel):
    """Stable committed result; a replay returns exactly this snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    session_id: str
    agent_id: str
    deployment_id: str
    definition_version: str
    config_options: list[schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean]


class SessionCreationOperation(BaseModel):
    """Durable creation status. Pending operations are not visible sessions.

    The mutation ID is scoped to a principal, not a deployment or credential. Keys
    are retained even after session deletion, so retries cannot resurrect an intent.
    Owner/fence fields describe recovery coordination, not a product authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    mutation_id: str = Field(min_length=1, max_length=200)
    session_id: str
    principal: str
    credential_id: str | None = None
    connection_id: str | None = None
    scopes: frozenset[str] | None = None
    deployment_id: str
    request: SessionCreateRequest
    request_hash: str
    state: SessionCreationState = SessionCreationState.RESERVED
    result: SessionCreationResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    owner_id: str | None = None
    fence: int = 0
    expires_at: int = 0
    created_at: int
    updated_at: int


CREATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS acp_session_creations (
    principal TEXT NOT NULL,
    mutation_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    owner_id TEXT,
    fence BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (principal, mutation_id)
)
"""


def creation_conflict() -> HrnsError:
    return HrnsError(
        "SESSION_CREATION_CONFLICT",
        "The session creation operation is held by another attempt or needs reconciliation.",
    )


def check_creation_owner(
    current: SessionCreationOperation, updated: SessionCreationOperation, now: int
) -> None:
    if (
        current.owner_id is None
        or current.owner_id != updated.owner_id
        or current.fence != updated.fence
        or current.expires_at <= now
        or current.state in {SessionCreationState.SUCCEEDED, SessionCreationState.FAILED}
        or current.request_hash != updated.request_hash
        or current.operation_id != updated.operation_id
        or current.session_id != updated.session_id
        or current.request != updated.request
    ):
        raise creation_conflict()
