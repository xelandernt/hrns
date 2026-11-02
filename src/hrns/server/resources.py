"""Session-scoped resource resolution for embedded agent servers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from hrns.runtime.acp_store import SessionLease
from hrns.runtime.creation import SessionCreationOperation
from hrns.runtime.definitions import DefinitionBinding
from hrns.runtime.resources import ResourceSet
from hrns.runtime.session_policy import SessionContext


class SessionResourcePhase(StrEnum):
    """The authority boundary at which resource composition is requested."""

    CREATION = "creation"
    LOAD = "load"
    TURN = "turn"
    RECOMPOSITION = "recomposition"


@dataclass(frozen=True, slots=True)
class SessionResourceContext:
    """Authoritative context for composition under a fenced session lease."""

    phase: SessionResourcePhase
    session_id: str
    deployment_id: str
    agent_id: str
    definition_version: str
    principal: str
    owner: str
    lease: SessionLease
    policy: SessionContext
    creation: SessionCreationOperation | None = None


@dataclass(frozen=True, slots=True)
class SessionResources:
    """Bindings for declared slots and dependencies passed to the native agent run."""

    resources: ResourceSet = field(default_factory=ResourceSet)
    deps: object = None
    capability_bindings: tuple[DefinitionBinding, ...] = ()


SessionResourceResolver = Callable[[SessionResourceContext], Awaitable[SessionResources]]
