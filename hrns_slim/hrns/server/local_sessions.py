"""SQLite persistence for the built-in stdio agent."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from .acp.store import (
    _PAGE_SIZE,
    SessionAccess,
    SessionSummary,
    StoredSession,
    _cursor_offset,
    title_from_updates,
)

_SESSION = TypeAdapter(StoredSession)


class LocalSessionStore:
    """Keep saved sessions available when the TUI restarts its stdio agent."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS acp_sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    title TEXT,
                    updated TEXT NOT NULL,
                    state BLOB NOT NULL
                )
            """)
            yield db

    async def create(self, session_id: str, *, cwd: str, principal: object) -> None:
        await asyncio.to_thread(self._write, session_id, StoredSession(cwd=cwd), True)

    async def save(self, session_id: str, session: StoredSession) -> None:
        await asyncio.to_thread(self._write, session_id, session, False)

    def _write(self, session_id: str, session: StoredSession, create: bool) -> None:
        title = title_from_updates(session.updates)
        updated = datetime.now(UTC).isoformat()
        payload = _SESSION.dump_json(session)
        with self._connect() as db:
            if create:
                db.execute(
                    "INSERT INTO acp_sessions (id, cwd, title, updated, state) VALUES (?, ?, ?, ?, ?)",
                    (session_id, session.cwd, title, updated, payload),
                )
            else:
                db.execute(
                    "UPDATE acp_sessions SET title = coalesce(?, title), updated = ?, state = ? WHERE id = ?",
                    (title, updated, payload, session_id),
                )

    async def load(
        self, session_id: str, *, principal: object, operation: SessionAccess
    ) -> StoredSession | None:
        return await asyncio.to_thread(self._load, session_id)

    def _load(self, session_id: str) -> StoredSession | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT state FROM acp_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _SESSION.validate_json(row[0]) if row is not None else None

    async def list_sessions(
        self, *, principal: object, cwd: str | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SessionSummary], str | None]:
        return await asyncio.to_thread(self._list, cwd, _cursor_offset(cursor))

    def _list(self, cwd: str | None, offset: int) -> tuple[list[SessionSummary], str | None]:
        if cwd is not None:
            cwd = str(Path(cwd).resolve())
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, cwd, title, updated FROM acp_sessions
                WHERE (? IS NULL OR cwd = ?)
                ORDER BY updated DESC, id
                LIMIT ? OFFSET ?
                """,
                (cwd, cwd, _PAGE_SIZE + 1, offset),
            ).fetchall()
        page = [
            SessionSummary(session_id, workspace, title, datetime.fromisoformat(updated))
            for session_id, workspace, title, updated in rows[:_PAGE_SIZE]
        ]
        return page, str(offset + _PAGE_SIZE) if len(rows) > _PAGE_SIZE else None

    async def delete(self, session_id: str, *, principal: object) -> None:
        await asyncio.to_thread(self._delete, session_id)

    def _delete(self, session_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM acp_sessions WHERE id = ?", (session_id,))
