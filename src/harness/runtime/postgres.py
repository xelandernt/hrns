"""PostgreSQL persistence for ACP sessions.

Implements the same scoped contract as :mod:`harness.runtime.store`: one row per
session holds the upstream ``StoredSession`` payload and host metadata. No turn,
transcript, revision, or workspace-lease tables are created.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .acp_store import (
    SessionRecord,
    SessionStore,
    decode_session,
    deserialize_bindings,
    encode_session,
    now_ms,
    serialize_bindings,
)

if TYPE_CHECKING:
    from pydantic_ai_harness.experimental.acp import StoredSession

__all__ = ["PostgresSessionStore", "is_postgres_dsn"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS acp_sessions (
    session_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT,
    effort TEXT,
    bindings TEXT NOT NULL DEFAULT '{}',
    usage TEXT NOT NULL DEFAULT '{}',
    messages TEXT NOT NULL DEFAULT '[]',
    updates TEXT NOT NULL DEFAULT '[]',
    model TEXT,
    created_at BIGINT NOT NULL,
    last_active_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acp_sessions_scope
    ON acp_sessions(principal, deployment_id, last_active_at DESC);
"""


def is_postgres_dsn(value: str | None) -> bool:
    """Whether a configured store value names a PostgreSQL connection."""
    if not value:
        return False
    scheme = urlsplit(value).scheme
    return scheme in {"postgres", "postgresql"}


def _record_from_row(row: Mapping[str, Any]) -> SessionRecord:
    return SessionRecord(
        session_id=str(row["session_id"]),
        principal=str(row["principal"]),
        deployment_id=str(row["deployment_id"]),
        agent_id=str(row["agent_id"]),
        definition_version=str(row["definition_version"]),
        cwd=str(row["cwd"]),
        title=row["title"],
        effort=row["effort"],
        model=row["model"],
        bindings=deserialize_bindings(row["bindings"]),
        usage=json.loads(row["usage"] or "{}"),
        created_at=int(row["created_at"]),
        last_active_at=int(row["last_active_at"]),
    )


class PostgresSessionStore(SessionStore):
    """Connection-backed PostgreSQL store with the same API as the SQLite store."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._connection: Any = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        import psycopg
        from psycopg.rows import dict_row

        async with self._lock:
            self._connection = await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row)
            async with self._connection.cursor() as cursor:
                await cursor.execute(_SCHEMA)
            await self._connection.commit()
            self._initialized = True

    async def close(self) -> None:
        async with self._lock:
            connection = self._connection
            self._connection = None
            self._initialized = False
            if connection is not None:
                await connection.close()

    def _require_connection(self) -> Any:
        if not self._initialized or self._connection is None:
            raise RuntimeError("SessionStore.initialize() must run before use")
        return self._connection

    async def create(self, record: SessionRecord) -> None:
        created = record.created_at or now_ms()
        active = record.last_active_at or created
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT principal, deployment_id FROM acp_sessions WHERE session_id = %s",
                    (record.session_id,),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    if (
                        existing["principal"] != record.principal
                        or existing["deployment_id"] != record.deployment_id
                    ):
                        raise PermissionError("session id belongs to another scope")
                    await cursor.execute(
                        "UPDATE acp_sessions SET cwd = %s, last_active_at = %s WHERE session_id = %s",
                        (record.cwd, active, record.session_id),
                    )
                else:
                    await cursor.execute(
                        """
                        INSERT INTO acp_sessions (
                            session_id, principal, deployment_id, agent_id, definition_version,
                            cwd, title, effort, bindings, usage, created_at, last_active_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            record.session_id,
                            record.principal,
                            record.deployment_id,
                            record.agent_id,
                            record.definition_version,
                            record.cwd,
                            record.title,
                            record.effort,
                            serialize_bindings(record.bindings),
                            json.dumps(record.usage, separators=(",", ":")),
                            created,
                            active,
                        ),
                    )
            await connection.commit()

    async def get(
        self, principal: str, deployment_id: str, session_id: str
    ) -> SessionRecord | None:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM acp_sessions WHERE session_id = %s AND principal = %s "
                    "AND deployment_id = %s",
                    (session_id, principal, deployment_id),
                )
                row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def list(self, principal: str, deployment_id: str) -> list[SessionRecord]:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM acp_sessions WHERE principal = %s AND deployment_id = %s "
                    "ORDER BY last_active_at DESC, session_id ASC",
                    (principal, deployment_id),
                )
                rows = await cursor.fetchall()
        return [_record_from_row(row) for row in rows]

    async def delete(self, principal: str, deployment_id: str, session_id: str) -> bool:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM acp_sessions WHERE session_id = %s AND principal = %s "
                    "AND deployment_id = %s",
                    (session_id, principal, deployment_id),
                )
                deleted = cursor.rowcount
            await connection.commit()
        return bool(deleted)

    async def update(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        *,
        title: str | None = None,
        effort: str | None = None,
        bindings: Mapping[str, Mapping[str, Any]] | None = None,
        usage: Mapping[str, int] | None = None,
        touch: bool = False,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        if title is not None:
            assignments.append("title = %s")
            values.append(title)
        if effort is not None:
            assignments.append("effort = %s")
            values.append(effort)
        if bindings is not None:
            assignments.append("bindings = %s")
            values.append(serialize_bindings(bindings))
        if usage is not None:
            assignments.append("usage = %s")
            values.append(json.dumps(dict(usage), separators=(",", ":")))
        if touch:
            assignments.append("last_active_at = %s")
            values.append(now_ms())
        if not assignments:
            return
        values.extend([session_id, principal, deployment_id])
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    f"UPDATE acp_sessions SET {', '.join(assignments)} "
                    "WHERE session_id = %s AND principal = %s AND deployment_id = %s",
                    values,
                )
            await connection.commit()

    async def save_scoped(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        session: "StoredSession",
    ) -> None:
        messages, updates, model = encode_session(session)
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT principal, deployment_id FROM acp_sessions WHERE session_id = %s",
                    (session_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise PermissionError("session was not created in this scope")
                if row["principal"] != principal or row["deployment_id"] != deployment_id:
                    raise PermissionError("session belongs to another scope")
                await cursor.execute(
                    "UPDATE acp_sessions SET messages = %s, updates = %s, model = %s, "
                    "last_active_at = %s WHERE session_id = %s",
                    (messages, updates, model, now_ms(), session_id),
                )
            await connection.commit()

    async def load_scoped(
        self, principal: str, deployment_id: str, session_id: str
    ) -> StoredSession | None:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT messages, updates, model FROM acp_sessions "
                    "WHERE session_id = %s AND principal = %s AND deployment_id = %s",
                    (session_id, principal, deployment_id),
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        return decode_session(row["messages"], row["updates"], row["model"])
