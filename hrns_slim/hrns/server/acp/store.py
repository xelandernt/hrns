"""Pluggable persistence for ACP sessions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar, runtime_checkable

from acp import schema
from pydantic_ai.messages import ModelMessage

from hrns.server._types import PrincipalT
from hrns.server.capabilities import TodoItem

from .session import SessionUpdate

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
]
"""The requested use of a session passed to its principal-aware store lookup."""


@dataclass(frozen=True, kw_only=True)
class StoredSession:
    """A session's persisted state: the model's message history and client-visible transcript."""

    cwd: str
    messages: list[ModelMessage] = field(default_factory=list[ModelMessage])
    updates: list[SessionUpdate] = field(default_factory=list[SessionUpdate])
    model: str | None = None
    #: The session's setting selections; revalidated against the live options on load.
    setting_selections: dict[str, str] = field(default_factory=dict[str, str])
    todos: tuple[TodoItem, ...] = ()
    #: Skill ids a person invoked in this session, so ``session/load`` restores them.
    skills: tuple[str, ...] = ()


class SessionPersistence(Protocol):
    """Trusted runtime persistence, independent of any caller identity."""

    async def save(self, session_id: str, session: StoredSession) -> None: ...


class SessionStore(SessionPersistence, Protocol[PrincipalT_contra]):
    """Persistence and principal-aware session access implemented by the application.

    ``create``, ``load``, ``list_sessions``, and ``delete`` receive the caller's
    principal. Raise PermissionError to deny access. ``load`` receives the requested
    operation so applications can enforce separate permissions. It also gates resident
    sessions before client operations; it is called even when no restore is needed.
    ``save`` is trusted runtime persistence and has no connection-owned principal.
    """

    async def create(
        self, session_id: str, *, cwd: str, principal: PrincipalT_contra | None
    ) -> None: ...

    async def load(
        self, session_id: str, *, principal: PrincipalT_contra | None, operation: SessionAccess
    ) -> StoredSession | None: ...


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """The metadata ``session/list`` reports for one stored session."""

    session_id: str
    cwd: str
    title: str | None = None
    updated_at: datetime | None = None


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


def title_from_updates(updates: Sequence[object]) -> str | None:
    """Derive a session title from the first user text in its transcript."""
    for update in updates:
        if not isinstance(update, schema.UserMessageChunk):
            continue
        content = update.content
        if isinstance(content, schema.TextContentBlock):
            title = " ".join(content.text.split())
            if title:
                return title[:80]
    return None


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


class InMemorySessionStore(
    ListableSessionStore[PrincipalT], DeletableSessionStore[PrincipalT], Generic[PrincipalT]
):
    """Process-local persistence with unrestricted access for every principal."""

    def __init__(self) -> None:
        self._sessions: dict[str, StoredSession] = {}
        self._summaries: dict[str, SessionSummary] = {}

    async def save(self, session_id: str, session: StoredSession) -> None:
        self._sessions[session_id] = session
        previous = self._summaries.get(session_id)
        self._summaries[session_id] = SessionSummary(
            session_id=session_id,
            cwd=session.cwd,
            title=title_from_updates(session.updates)
            or (previous.title if previous is not None else None),
            updated_at=datetime.now(UTC),
        )

    async def load(
        self, session_id: str, *, principal: PrincipalT | None, operation: SessionAccess
    ) -> StoredSession | None:
        return self._sessions.get(session_id)

    async def create(self, session_id: str, *, cwd: str, principal: PrincipalT | None) -> None:
        await self.save(session_id, StoredSession(cwd=cwd))

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
        self._sessions.pop(session_id, None)
        self._summaries.pop(session_id, None)
