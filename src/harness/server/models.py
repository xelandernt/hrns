"""Python registration and authentication contracts for embedded agent servers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from harness.runtime import AgentDefinition


@dataclass(frozen=True)
class Principal:
    """Authenticated user identity used to isolate persisted agent sessions.

    Args:
        id: Stable, non-empty user or service ID, independent of its credentials.
            Returning the same ID after rotating a token preserves session access.
        agent_ids: Agent IDs this identity may discover and connect to. ``None``
            permits all registered agents; an empty frozenset permits none.

    Authentication dependencies return this object after validating credentials;
    it is not itself a token. Without an authentication dependency, every caller
    shares ``Principal("local")`` and that identity's session access.
    """

    id: str
    agent_ids: frozenset[str] | None = None

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


@dataclass(frozen=True)
class RegisteredAgent:
    definition: AgentDefinition
    allowed_cwds: tuple[str, ...]
    default_cwd: str
    models: tuple[str, ...] = ()
    resources: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.definition.agent_id

    @property
    def session_namespace(self) -> str:
        # JSON avoids ambiguous delimiters in definition IDs and versions.
        import json

        return json.dumps([self.id, self.definition.definition_version], separators=(",", ":"))
