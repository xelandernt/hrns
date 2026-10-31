"""Public per-session authorization policy contracts.

The store's ``(principal, deployment_id)`` scope is the coarse isolation boundary;
this module is the per-session authority. An embedding application supplies a
:class:`SessionPolicy` when it needs owner-private, shared, archived-read-only, or
custom rules that a single principal string cannot express.

A policy receives one :class:`SessionContext` per attempted operation and returns a
:class:`PolicyDecision`. Decisions are tri-state: allow, read-only, or deny. Read-only
is not a denial: replay and inspection stay available while every mutating operation
fails with a stable ``SESSION_READ_ONLY`` error.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "DefaultSessionPolicy",
    "PolicyAction",
    "PolicyDecision",
    "READ_ONLY_ACTIONS",
    "READ_ACTIONS",
    "SessionAccess",
    "SessionContext",
    "SessionPolicy",
    "decide_policy",
]


class SessionAccess(StrEnum):
    """Built-in per-session access modes.

    ``PRIVATE`` is owner-only. ``SHARED`` admits the owner and every principal named in
    ``collaborators``. ``ARCHIVED`` admits readers but rejects every mutation, including
    prompts, steering, configuration, approvals, commands, rename, and delete.
    """

    PRIVATE = "private"
    SHARED = "shared"
    ARCHIVED = "archived"


class PolicyAction(StrEnum):
    """One operation a policy may authorize."""

    CREATE = "create"
    LIST = "list"
    LOAD = "load"
    REPLAY = "replay"
    PROMPT = "prompt"
    STEER = "steer"
    CONFIGURE = "configure"
    APPROVE = "approve"
    COMMAND = "command"
    RENAME = "rename"
    ARCHIVE = "archive"
    DELETE = "delete"
    RETRY = "retry"
    CLOSE = "close"
    CANCEL = "cancel"


#: Actions that only read a session; they remain allowed on archived sessions.
READ_ACTIONS: frozenset[PolicyAction] = frozenset(
    {
        PolicyAction.LIST,
        PolicyAction.LOAD,
        PolicyAction.REPLAY,
    }
)

#: Mutating actions rejected by an archived session.
READ_ONLY_ACTIONS: frozenset[PolicyAction] = frozenset(
    action for action in PolicyAction if action not in READ_ACTIONS
)


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Everything a policy may inspect about one request.

    ``credential_id`` and ``connection_id`` are stable per authenticated credential and
    per transport connection. They are ``None`` only for host-internal calls that have no
    authenticated transport (for example a scheduled archive operation).
    """

    session_id: str
    principal: str
    deployment_id: str
    agent_id: str
    owner: str
    access: SessionAccess = SessionAccess.PRIVATE
    collaborators: frozenset[str] = frozenset()
    credential_id: str | None = None
    connection_id: str | None = None
    request_id: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The result of one policy evaluation."""

    allowed: bool
    read_only: bool = False
    code: str = "POLICY_DENIED"
    message: str = "The session policy does not permit this operation."

    @classmethod
    def allow(cls) -> PolicyDecision:
        return cls(allowed=True)

    @classmethod
    def deny(
        cls,
        message: str = "The session policy does not permit this operation.",
        *,
        code: str = "POLICY_DENIED",
    ) -> PolicyDecision:
        return cls(allowed=False, code=code, message=message)

    @classmethod
    def read_only_denial(
        cls,
        message: str = "This session is archived and cannot be modified.",
        *,
        code: str = "SESSION_READ_ONLY",
    ) -> PolicyDecision:
        return cls(allowed=False, read_only=True, code=code, message=message)


@runtime_checkable
class SessionPolicy(Protocol):
    """Authorize one operation on one session.

    Implementations may be synchronous or asynchronous. Returning a denial hides the
    session exactly like a missing session does; returning read-only keeps replay and
    listing available while rejecting mutations.
    """

    def decide(
        self, context: SessionContext, action: PolicyAction
    ) -> PolicyDecision | Awaitable[PolicyDecision]: ...


def _not_found() -> PolicyDecision:
    return PolicyDecision.deny(
        "No session with this id is available to this principal.",
        code="SESSION_NOT_FOUND",
    )


class DefaultSessionPolicy:
    """Built-in private/shared/archived semantics.

    The deployment's ``accepts_client_capabilities`` value is a default for new
    sessions, not a per-session decision; capability gating is stored on the session and
    exposed through :attr:`SessionContext.metadata` as ``clientCapabilities``.
    """

    def decide(self, context: SessionContext, action: PolicyAction) -> PolicyDecision:
        if context.access is SessionAccess.SHARED:
            admitted = context.principal == context.owner or (
                context.principal in context.collaborators
            )
        else:
            admitted = context.principal == context.owner

        if context.access is SessionAccess.ARCHIVED:
            if action in READ_ACTIONS:
                return PolicyDecision.allow() if admitted else _not_found()
            if action is PolicyAction.ARCHIVE and context.principal == context.owner:
                # The owner may restore an archived session; every other mutation stays
                # rejected. This is the ACP escape hatch for the embedding API.
                return PolicyDecision.allow()
            return PolicyDecision.read_only_denial()

        if admitted:
            return PolicyDecision.allow()
        return _not_found()


async def decide_policy(
    policy: SessionPolicy, context: SessionContext, action: PolicyAction
) -> PolicyDecision:
    """Evaluate a policy that may be synchronous or asynchronous."""
    decision = policy.decide(context, action)
    if inspect.isawaitable(decision):
        return await decision
    return decision
