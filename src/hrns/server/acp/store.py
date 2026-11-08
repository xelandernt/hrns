"""Pluggable persistence for ACP sessions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from acp import schema
from pydantic_ai.messages import ModelMessage

from hrns.server.capabilities import TodoItem

from .session import SessionUpdate


@dataclass(frozen=True, kw_only=True)
class StoredSession:
    """A session's persisted state: the model's message history and client-visible transcript."""

    messages: list[ModelMessage] = field(default_factory=list[ModelMessage])
    updates: list[SessionUpdate] = field(default_factory=list[SessionUpdate])
    model: str | None = None
    #: The session's setting selections; revalidated against the live options on load.
    setting_selections: dict[str, str] = field(default_factory=dict[str, str])
    todos: tuple[TodoItem, ...] = ()


class SessionStore(Protocol):
    """Where the adapter saves and restores sessions so ``session/load`` can reopen them.

    A ``save`` that raises is logged and swallowed; a ``load`` that raises fails
    ``session/load`` with an internal error. Implementations do not need to translate their
    own errors into ACP errors.
    """

    async def save(self, session_id: str, session: StoredSession) -> None: ...

    async def load(self, session_id: str) -> StoredSession | None: ...


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """The metadata ``session/list`` reports for one stored session."""

    session_id: str
    cwd: str
    title: str | None = None
    updated_at: datetime | None = None


@runtime_checkable
class ListableSessionStore(SessionStore, Protocol):
    """A session store the adapter can enumerate for ``session/list``."""

    async def remember(self, session_id: str, *, cwd: str) -> None: ...

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SessionSummary], str | None]: ...


@runtime_checkable
class DeletableSessionStore(SessionStore, Protocol):
    """A session store the adapter can delete from for ``session/delete``."""

    async def delete(self, session_id: str) -> None: ...


_PAGE_SIZE = 100


def _title_from_updates(updates: Sequence[object]) -> str | None:
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


class InMemorySessionStore(ListableSessionStore, DeletableSessionStore):
    """Process-local store that also supports ``session/list`` and ``session/delete``."""

    def __init__(self) -> None:
        self._sessions: dict[str, StoredSession] = {}
        self._summaries: dict[str, SessionSummary] = {}

    async def save(self, session_id: str, session: StoredSession) -> None:
        self._sessions[session_id] = session
        previous = self._summaries.get(session_id)
        self._summaries[session_id] = SessionSummary(
            session_id=session_id,
            cwd=previous.cwd if previous is not None else "",
            title=_title_from_updates(session.updates)
            or (previous.title if previous is not None else None),
            updated_at=datetime.now(UTC),
        )

    async def load(self, session_id: str) -> StoredSession | None:
        return self._sessions.get(session_id)

    async def remember(self, session_id: str, *, cwd: str) -> None:
        previous = self._summaries.get(session_id)
        self._summaries[session_id] = SessionSummary(
            session_id=session_id,
            cwd=cwd,
            title=previous.title if previous is not None else None,
            updated_at=previous.updated_at if previous is not None else datetime.now(UTC),
        )

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
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

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._summaries.pop(session_id, None)
