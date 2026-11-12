"""Opt-in durable document storage in SQLite.

One row is one session checkpoint, partitioned by the registration's agent id.
Metadata (cwd, title, schema version, update time) is listed without reading the
snapshot blob. A checkpoint is written in one transaction, so a snapshot and its
pending imports are either both durable or both absent.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Generic, TypeVar

from hrns.server._types import PrincipalT

from .store import (
    _PAGE_SIZE,
    DocumentCheckpoint,
    SessionAccess,
    SessionSummary,
    _cursor_offset,
)

Authorize = Callable[[str, SessionAccess, PrincipalT | None], bool]
T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT,
    schema_version INTEGER NOT NULL,
    snapshot BLOB NOT NULL,
    pending BLOB,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_agent_updated
    ON sessions (agent_id, updated_at DESC, session_id);
"""


class SQLiteDocumentStore(Generic[PrincipalT]):
    """Durable checkpoints scoped to one registration's agent id."""

    def __init__(
        self,
        path: str | Path,
        *,
        agent_id: str,
        authorize: Authorize[PrincipalT] | None = None,
    ) -> None:
        self._path = str(path)
        self._agent_id = agent_id
        self._authorize = authorize

    def _authorize_access(
        self, session_id: str, operation: SessionAccess, principal: PrincipalT | None
    ) -> None:
        if self._authorize is not None and not self._authorize(session_id, operation, principal):
            raise PermissionError

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.executescript(_SCHEMA)
        return connection

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def run() -> T:
            connection = self._connect()
            try:
                with connection:
                    return operation(connection)
            finally:
                connection.close()

        return await asyncio.to_thread(run)

    async def create(
        self,
        session_id: str,
        *,
        checkpoint: DocumentCheckpoint,
        principal: PrincipalT | None,
    ) -> None:
        self._authorize_access(session_id, "loro/write", principal)
        if checkpoint.agent_id != self._agent_id:
            raise ValueError("checkpoint belongs to another agent partition")

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, agent_id, cwd, title, schema_version, snapshot, pending, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                self._row(session_id, checkpoint),
            )

        await self._run(insert)

    async def save(self, session_id: str, checkpoint: DocumentCheckpoint) -> None:
        if checkpoint.agent_id != self._agent_id:
            raise ValueError("checkpoint belongs to another agent partition")

        def upsert(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, agent_id, cwd, title, schema_version, snapshot, pending, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "agent_id=excluded.agent_id, cwd=excluded.cwd, title=excluded.title, "
                "schema_version=excluded.schema_version, snapshot=excluded.snapshot, "
                "pending=excluded.pending, updated_at=excluded.updated_at",
                self._row(session_id, checkpoint),
            )

        await self._run(upsert)

    @staticmethod
    def _row(session_id: str, checkpoint: DocumentCheckpoint) -> tuple[object, ...]:
        return (
            session_id,
            checkpoint.agent_id,
            checkpoint.cwd,
            checkpoint.title,
            checkpoint.schema_version,
            checkpoint.snapshot,
            checkpoint.pending,
            checkpoint.updated_at.isoformat(),
        )

    async def load(
        self, session_id: str, *, principal: PrincipalT | None, operation: SessionAccess
    ) -> DocumentCheckpoint | None:
        self._authorize_access(session_id, operation, principal)

        def select(connection: sqlite3.Connection) -> DocumentCheckpoint | None:
            row = connection.execute(
                "SELECT session_id, agent_id, cwd, title, snapshot, pending, schema_version, updated_at "
                "FROM sessions WHERE session_id = ? AND agent_id = ?",
                (session_id, self._agent_id),
            ).fetchone()
            if row is None:
                return None
            return DocumentCheckpoint(
                session_id=row[0],
                agent_id=row[1],
                cwd=row[2],
                title=row[3],
                snapshot=bytes(row[4]),
                pending=bytes(row[5]) if row[5] is not None else None,
                schema_version=row[6],
                updated_at=datetime.fromisoformat(row[7]),
            )

        return await self._run(select)

    async def list_sessions(
        self, *, principal: PrincipalT | None, cwd: str | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SessionSummary], str | None]:
        self._authorize_access("", "loro/list", principal)
        offset = _cursor_offset(cursor)

        def select(connection: sqlite3.Connection) -> list[SessionSummary]:
            query = "SELECT session_id, cwd, title, updated_at FROM sessions WHERE agent_id = ?"
            arguments: list[object] = [self._agent_id]
            if cwd is not None:
                query += " AND cwd = ?"
                arguments.append(cwd)
            query += " ORDER BY updated_at DESC, session_id LIMIT ? OFFSET ?"
            arguments.extend((_PAGE_SIZE + 1, offset))
            rows = connection.execute(query, arguments).fetchall()
            return [
                SessionSummary(
                    session_id=row[0],
                    cwd=row[1],
                    title=row[2],
                    updated_at=datetime.fromisoformat(row[3]),
                )
                for row in rows
            ]

        summaries = await self._run(select)
        has_next = len(summaries) > _PAGE_SIZE
        page = tuple(summaries[:_PAGE_SIZE])
        next_cursor = str(offset + _PAGE_SIZE) if has_next else None
        return page, next_cursor

    async def delete(self, session_id: str, *, principal: PrincipalT | None) -> None:
        self._authorize_access(session_id, "session/delete", principal)

        def remove(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM sessions WHERE session_id = ? AND agent_id = ?",
                (session_id, self._agent_id),
            )

        await self._run(remove)

    async def close(self) -> None:
        """Connections are opened per operation; nothing stays open."""

    @property
    def path(self) -> str:
        return self._path

    @property
    def agent_id(self) -> str:
        return self._agent_id


__all__ = ["SQLiteDocumentStore"]
