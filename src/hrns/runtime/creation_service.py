"""Fenced creation attempts and idempotent external-resource recovery."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic_ai_harness.experimental.acp import StoredSession

from .acp_store import SessionLease, SessionRecord, SessionStore, now_ms
from .creation import (
    SessionCreationOperation,
    SessionCreationResult,
    SessionCreationState,
    creation_conflict,
)
from .lifecycle import LifecycleEvent, SessionLifecycleContext, SessionLifecycleHooks, run_hook


def creation_lifecycle_context(operation: SessionCreationOperation) -> SessionLifecycleContext:
    return SessionLifecycleContext(
        session_id=operation.session_id,
        principal=operation.principal,
        deployment_id=operation.deployment_id,
        agent_id=operation.request.agent_id,
        definition_version=operation.request.definition_version,
        cwd=operation.request.cwd,
        credential_id=operation.credential_id,
        connection_id=operation.connection_id,
        operation_id=operation.operation_id,
        mutation_id=operation.mutation_id,
        operation_fence=operation.fence,
        metadata=operation.request.policy.metadata,
    )


class CreationLease:
    """Serialize renewal and state changes for one database-fenced attempt."""

    def __init__(self, store: SessionStore, operation: SessionCreationOperation, ttl_ms: int):
        self.store = store
        self.operation = operation
        self.ttl_ms = ttl_ms
        self._lock = asyncio.Lock()
        self._lost = False

    async def transition(
        self,
        state: SessionCreationState,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        release: bool = False,
    ) -> None:
        async with self._lock:
            if self._lost:
                raise creation_conflict()
            now = now_ms()
            updated = self.operation.model_copy(
                update={
                    "state": state,
                    "error_code": error_code,
                    "error_message": error_message,
                    "updated_at": now,
                    "expires_at": 0 if release else now + self.ttl_ms,
                }
            )
            await self.store.update_creation(updated)
            self.operation = updated

    async def commit(
        self,
        record: SessionRecord,
        session: StoredSession,
        result: SessionCreationResult,
        session_lease: SessionLease | None = None,
    ) -> None:
        async with self._lock:
            if self._lost:
                raise creation_conflict()
            updated = self.operation.model_copy(update={"result": result, "updated_at": now_ms()})
            await self.store.commit_creation(updated, record, session, session_lease)
            self.operation = updated.model_copy(
                update={
                    "state": SessionCreationState.SUCCEEDED,
                    "owner_id": None,
                    "expires_at": 0,
                }
            )

    async def renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.ttl_ms / 3000)
                async with self._lock:
                    if self.operation.expires_at == 0:
                        return
                    updated = self.operation.model_copy(
                        update={
                            "expires_at": now_ms() + self.ttl_ms,
                            "updated_at": now_ms(),
                        }
                    )
                    await self.store.update_creation(updated)
                    self.operation = updated
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost = True


@asynccontextmanager
async def creation_lease(
    store: SessionStore, operation: SessionCreationOperation, ttl_ms: int
) -> AsyncIterator[CreationLease]:
    lease = CreationLease(store, operation, ttl_ms)
    renewal = asyncio.create_task(lease.renew())
    try:
        yield lease
    finally:
        renewal.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewal


async def recover_creation(lease: CreationLease, hooks: SessionLifecycleHooks) -> None:
    """Compensate uncertain create effects before allowing the original intent to retry.

    A hook can have succeeded just before process loss. It must therefore implement
    idempotent create/destroy keyed by operation/session ID, and fence external effects
    when they can outlive an attempt. Cleanup failure stays durably pending.
    """
    operation = lease.operation
    if operation.state in {SessionCreationState.CREATING, SessionCreationState.CLEANUP}:
        await lease.transition(
            SessionCreationState.CLEANUP,
            error_code=operation.error_code,
            error_message=operation.error_message,
        )
        await run_hook(hooks, LifecycleEvent.DESTROY, creation_lifecycle_context(lease.operation))
    await lease.transition(
        SessionCreationState.FAILED if operation.error_code else SessionCreationState.RESERVED,
        error_code=operation.error_code,
        error_message=operation.error_message,
        release=True,
    )
