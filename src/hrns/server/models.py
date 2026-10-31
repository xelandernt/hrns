"""Python registration and authentication contracts for embedded agent servers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import TypeAlias

from hrns.runtime import AgentDefinition, OperationKind
from hrns.runtime.creation import InitialSessionPolicy, SessionNewInput


@dataclass(frozen=True)
class Principal:
    """Authenticated user identity used to isolate persisted agent sessions.

    Args:
        id: Stable, non-empty user or service ID, independent of its credentials.
            Returning the same ID after rotating a token preserves session access.
        agent_ids: Agent IDs this identity may discover and connect to. ``None``
            permits all registered agents; an empty frozenset permits none.
        credential_id: Stable identifier for the credential that authenticated this
            request (an API key or token record), not for the human. Two keys for one
            user share ``id`` and differ here, so usage attribution and revocation can
            target one key without changing session ownership.
        scopes: Credential permissions. ``None`` grants the historical unrestricted
            behavior; an explicit set must contain ``queue:mutate`` for queue writes
            and ``session:create`` for authoritative session creation.

    Authentication dependencies return this object after validating credentials;
    it is not itself a token. Without an authentication dependency, every caller
    shares ``Principal("local")`` and that identity's session access.
    """

    id: str
    agent_ids: frozenset[str] | None = None
    credential_id: str | None = None
    scopes: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Principal.id must not be empty")

    def may_use(self, agent_id: str) -> bool:
        return self.agent_ids is None or agent_id in self.agent_ids


AuthenticationDependency: TypeAlias = Callable[
    ..., Principal | Awaitable[Principal] | Iterator[Principal] | AsyncIterator[Principal]
]
"""FastAPI dependency returning a Principal, synchronously, asynchronously, or via yield.

Parameters are supplied by FastAPI dependency injection. Use HTTPConnection for
request access on both HTTP and WebSocket routes. Reject invalid credentials by
raising HTTPException(401), rather than returning None. Yield dependencies must
yield exactly one Principal and may release application resources during cleanup.
"""

InitialSessionPolicyFactory: TypeAlias = Callable[
    [Principal, SessionNewInput], InitialSessionPolicy | Awaitable[InitialSessionPolicy]
]
"""Server-owned initial policy resolver for hosted ACP creation, never client policy."""


@dataclass(frozen=True)
class RegisteredAgent:
    definition: AgentDefinition
    allowed_cwds: tuple[str, ...]
    default_cwd: str
    models: tuple[str, ...] = ()
    resources: dict[str, object] = field(default_factory=dict)
    accepts_client_capabilities: bool = False

    @property
    def id(self) -> str:
        return self.definition.agent_id

    @property
    def session_namespace(self) -> str:
        # The namespace is the logical agent, not the revision. A version bump must not
        # orphan existing rows: the row itself records the definition version that
        # created it, and the host resolves that revision for composition.
        import json

        return json.dumps([self.id], separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SessionLifecycleResult:
    """Completed authoritative embedding close/delete result."""

    operation_id: str
    session_id: str
    agent_id: str
    operation: OperationKind
