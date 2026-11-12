"""Document checkpoint persistence with principal-aware session access.

The store payload is a Loro checkpoint, never a JSON conversation mirror. A
checkpoint carries the integrated snapshot plus pending imports so a document
that received an update before its dependencies is preserved verbatim.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar, runtime_checkable

from hrns.server._types import PrincipalT

PrincipalT_contra = TypeVar("PrincipalT_contra", contravariant=True)

SessionAccess: TypeAlias = Literal[
    "session/load",
    "session/prompt",
    "session/cancel",
    "session/close",
    "session/delete",
    "session/set_config_option",
    "document/didOpen",
    "document/didChange",
    "document/didClose",
    "document/didSave",
    "document/didFocus",
    "ag-ui/snapshot",
    "ag-ui/events",
    "loro/snapshot",
    "loro/sync",
    "loro/write",
]
"""The requested use of a session passed to its principal-aware store lookup."""


@dataclass(frozen=True, slots=True)
class DocumentCheckpoint:
    """One durable document image and the metadata needed to list it."""

    session_id: str
    agent_id: str
    cwd: str
    snapshot: bytes
    pending: bytes | None
    schema_version: int
    updated_at: datetime
    title: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """The metadata ``session/list`` reports for one stored session."""

    session_id: str
    cwd: str
    title: str | None = None
    updated_at: datetime | None = None


class CheckpointPersistence(Protocol):
    """Trusted runtime persistence, independent of any caller identity."""

    async def save(self, session_id: str, checkpoint: DocumentCheckpoint) -> None: ...


class SessionStore(CheckpointPersistence, Protocol[PrincipalT_contra]):
    """Document persistence and principal-aware session access.

    ``create``, ``load``, ``list_sessions``, and ``delete`` receive the caller's
    principal. Raise PermissionError to deny access. ``load`` receives the
    requested operation so applications can enforce separate permissions. It
    also gates resident sessions before client operations; it is called even
    when no restore is needed. ``save`` is trusted runtime persistence and has
    no connection-owned principal.
    """

    async def create(
        self,
        session_id: str,
        *,
        checkpoint: DocumentCheckpoint,
        principal: PrincipalT_contra | None,
    ) -> None: ...

    async def load(
        self, session_id: str, *, principal: PrincipalT_contra | None, operation: SessionAccess
    ) -> DocumentCheckpoint | None: ...


@runtime_checkable
class ListableSessionStore(SessionStore[PrincipalT_contra], Protocol[PrincipalT_contra]):
    """A session store the adapter can enumerate for ``session/list``."""

    async def list_sessions(
        self,
        *,
        principal: PrincipalT_contra | None,
        cwd: str | None = None,
        cursor: str | None = None,
    ) -> tuple[Sequence[SessionSummary], str | None]: ...


@runtime_checkable
class DeletableSessionStore(SessionStore[PrincipalT_contra], Protocol[PrincipalT_contra]):
    """A session store the adapter can delete from for ``session/delete``."""

    async def delete(self, session_id: str, *, principal: PrincipalT_contra | None) -> None: ...


_PAGE_SIZE = 100


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(cursor)
    except ValueError as exc:
        raise ValueError("cursor is not a valid session-list offset") from exc
    if offset < 0:
        raise ValueError("cursor is not a valid session-list offset")
    return offset


class InMemoryDocumentStore(
    ListableSessionStore[PrincipalT], DeletableSessionStore[PrincipalT], Generic[PrincipalT]
):
    """Process-local document persistence with unrestricted access for every principal."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, DocumentCheckpoint] = {}
        self._summaries: dict[str, SessionSummary] = {}

    async def save(self, session_id: str, checkpoint: DocumentCheckpoint) -> None:
        self._checkpoints[session_id] = checkpoint
        self._summaries[session_id] = SessionSummary(
            session_id=session_id,
            cwd=checkpoint.cwd,
            title=checkpoint.title,
            updated_at=checkpoint.updated_at,
        )

    async def load(
        self, session_id: str, *, principal: PrincipalT | None, operation: SessionAccess
    ) -> DocumentCheckpoint | None:
        return self._checkpoints.get(session_id)

    async def create(
        self,
        session_id: str,
        *,
        checkpoint: DocumentCheckpoint,
        principal: PrincipalT | None,
    ) -> None:
        await self.save(session_id, checkpoint)

    async def list_sessions(
        self, *, principal: PrincipalT | None, cwd: str | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SessionSummary], str | None]:
        sessions = [
            summary
            for summary in self._summaries.values()
            if summary.cwd and (cwd is None or summary.cwd == cwd)
        ]
        sessions.sort(
            key=lambda summary: (
                -(summary.updated_at.timestamp() if summary.updated_at is not None else 0.0),
                summary.session_id,
            )
        )
        offset = _cursor_offset(cursor)
        page = tuple(sessions[offset : offset + _PAGE_SIZE])
        next_offset = offset + _PAGE_SIZE
        next_cursor = str(next_offset) if next_offset < len(sessions) else None
        return page, next_cursor

    async def delete(self, session_id: str, *, principal: PrincipalT | None) -> None:
        self._checkpoints.pop(session_id, None)
        self._summaries.pop(session_id, None)
