"""Per-request identity metadata for attribution.

A connection is not an identity. Two developers may share one session, and one
developer may hold several API keys. This record carries the stable user, credential,
and connection identifiers from the authenticated transport into prompts, steering,
approvals, policy decisions, queue rows, and usage attribution.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

__all__ = ["RequestContext", "current_connection_id", "current_request"]

#: Attribution for the request currently running in this task/connection.
current_request: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "hrns_current_request", default=None
)

#: Transport connection that owns the current request; selects the routed ACP client.
current_connection_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hrns_current_connection_id", default=None
)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Stable attribution metadata for one authenticated request.

    ``request_id`` is reserved for a per-ACP-request identifier and for queued prompts;
    ACP-originated requests currently leave it ``None``. ``credential_id`` identifies the
    key or token, not the human: rotating a key keeps ``principal`` stable and changes
    only ``credential_id``.
    """

    principal: str
    credential_id: str | None = None
    connection_id: str | None = None
    request_id: str | None = None
    agent_id: str | None = None
