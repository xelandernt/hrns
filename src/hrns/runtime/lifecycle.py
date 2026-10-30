"""Public session/queue lifecycle hooks for external resources.

An embedding application may own a sandbox, a database, or a container that must follow
a session's lifecycle. These hooks are the public contract between the host and that
resource: create-or-resume before work, suspend when the queue goes idle, and destroy
exactly once when the session is closed or deleted.

Hooks may be synchronous or asynchronous. A hook failure is reported as
``LIFECYCLE_FAILED`` and no prompt runs without its resource; ``destroy`` is retried
only by the application and must be idempotent.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hrns.protocol import HrnsError

__all__ = [
    "LifecycleEvent",
    "NoopLifecycleHooks",
    "SessionLifecycleContext",
    "SessionLifecycleHooks",
    "run_hook",
]


@dataclass(frozen=True, slots=True)
class SessionLifecycleContext:
    """Identity of the session a lifecycle hook is acting for."""

    session_id: str
    principal: str
    deployment_id: str
    agent_id: str
    cwd: str
    credential_id: str | None = None
    connection_id: str | None = None
    #: Stable close/delete idempotency key; ``None`` for create/resume/suspend/queue.
    operation_id: str | None = None


class LifecycleEvent:
    """Stable hook names used in error details."""

    CREATE = "create"
    RESUME = "resume"
    SUSPEND = "suspend"
    DESTROY = "destroy"
    QUEUE = "queue_changed"


@runtime_checkable
class SessionLifecycleHooks(Protocol):
    """Optional lifecycle callbacks; return ``None`` when not implemented.

    Implementations may return an awaitable. Raising is allowed; the host wraps the
    failure so the session cannot silently run without its external resource.
    """

    def create(self, context: SessionLifecycleContext) -> None | Awaitable[None]: ...

    def resume(self, context: SessionLifecycleContext) -> None | Awaitable[None]: ...

    def suspend(self, context: SessionLifecycleContext) -> None | Awaitable[None]: ...

    def destroy(self, context: SessionLifecycleContext) -> None | Awaitable[None]: ...

    def queue_changed(
        self, context: SessionLifecycleContext, pending: int
    ) -> None | Awaitable[None]: ...


class NoopLifecycleHooks:
    """Default hooks: no external resource to coordinate."""

    def create(self, context: SessionLifecycleContext) -> None:
        del context

    def resume(self, context: SessionLifecycleContext) -> None:
        del context

    def suspend(self, context: SessionLifecycleContext) -> None:
        del context

    def destroy(self, context: SessionLifecycleContext) -> None:
        del context

    def queue_changed(self, context: SessionLifecycleContext, pending: int) -> None:
        del context, pending


async def run_hook(
    hooks: SessionLifecycleHooks,
    name: str,
    context: SessionLifecycleContext,
    *args: object,
) -> None:
    """Invoke one hook and normalize sync/async and failure behavior."""
    callback = getattr(hooks, name, None)
    if callback is None:
        return
    try:
        result = callback(context, *args)
        if inspect.isawaitable(result):
            await result
    except HrnsError:
        raise
    except Exception as exc:
        details: dict[str, str] = {
            "sessionId": context.session_id,
            "hook": name,
            "error": str(exc) or type(exc).__name__,
        }
        if context.operation_id is not None:
            details["operationId"] = context.operation_id
        raise HrnsError(
            "LIFECYCLE_FAILED",
            f"The session {name} hook failed; the session cannot continue safely.",
            details,
        ) from exc
