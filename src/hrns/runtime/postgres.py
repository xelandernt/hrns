"""PostgreSQL persistence for ACP sessions.

Implements the same scoped contract as :mod:`hrns.runtime.store`: one row per
session holds the upstream ``StoredSession`` payload and host metadata. No turn,
transcript, revision, or workspace-lease tables are created.

The shared connection runs in autocommit mode so independent reads never leave an idle
transaction behind. Multi-statement mutations use an explicit transaction block, which
rolls back on database errors and cancellation instead of poisoning later requests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .acp_store import (
    SESSION_COLUMNS,
    SessionPage,
    SessionRecord,
    SessionStore,
    decode_session,
    decode_session_cursor,
    deserialize_bindings,
    encode_session,
    encode_session_cursor,
    now_ms,
    serialize_bindings,
)

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from pydantic_ai_harness.experimental.acp import StoredSession

    PgRow = dict[str, Any] | tuple[Any, ...]

__all__ = ["PostgresSessionStore", "is_postgres_dsn"]

_SCHEMA_TABLE = """
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
)
"""

_SCHEMA_INDEX = """
CREATE INDEX IF NOT EXISTS idx_acp_sessions_scope
    ON acp_sessions(principal, deployment_id, last_active_at DESC)
"""


def _columns(sql: Any) -> Any:
    """Quote the shared metadata projection for a PostgreSQL SELECT."""
    return sql.SQL(", ").join(sql.Identifier(name.strip()) for name in SESSION_COLUMNS.split(","))


def is_postgres_dsn(value: str | None) -> bool:
    """Whether a configured store value names a PostgreSQL connection."""
    if not value:
        return False
    scheme = urlsplit(value).scheme
    return scheme in {"postgres", "postgresql"}


def _row_dict(row: PgRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if not isinstance(row, Mapping):
        raise TypeError("PostgreSQL row factory returned a non-mapping row")
    return dict(row)


def _record_from_row(row: PgRow) -> SessionRecord:
    values = _row_dict(row)
    if values is None:
        raise TypeError("PostgreSQL row factory returned no row")
    return SessionRecord(
        session_id=str(values["session_id"]),
        principal=str(values["principal"]),
        deployment_id=str(values["deployment_id"]),
        agent_id=str(values["agent_id"]),
        definition_version=str(values["definition_version"]),
        cwd=str(values["cwd"]),
        title=values["title"],
        effort=values["effort"],
        model=values["model"],
        bindings=deserialize_bindings(values["bindings"]),
        usage=json.loads(values["usage"] or "{}"),
        created_at=int(values["created_at"]),
        last_active_at=int(values["last_active_at"]),
    )


class PostgresSessionStore(SessionStore):
    """Connection-backed PostgreSQL store with the same API as the SQLite store."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._connection: AsyncConnection[PgRow] | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        import psycopg
        from psycopg.rows import dict_row

        async with self._lock:
            # Another caller may have initialized while this one waited for the lock.
            if self._initialized:
                return
            connection: AsyncConnection[PgRow] = await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True, row_factory=dict_row
            )
            try:
                async with connection.cursor() as cursor:
                    await cursor.execute(_SCHEMA_TABLE)
                    await cursor.execute(_SCHEMA_INDEX)
            except BaseException:
                await connection.close()
                raise
            self._connection = connection
            self._initialized = True

    async def close(self) -> None:
        async with self._lock:
            connection = self._connection
            self._connection = None
            self._initialized = False
            if connection is not None:
                await connection.close()

    def _require_connection(self) -> AsyncConnection[PgRow]:
        if not self._initialized or self._connection is None:
            raise RuntimeError("SessionStore.initialize() must run before use")
        return self._connection

    async def create(self, record: SessionRecord) -> None:
        created = record.created_at or now_ms()
        active = record.last_active_at or created
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT principal, deployment_id FROM acp_sessions WHERE session_id = %s",
                        (record.session_id,),
                    )
                    existing = _row_dict(await cursor.fetchone())
                    if existing is not None:
                        if (
                            existing["principal"] != record.principal
                            or existing["deployment_id"] != record.deployment_id
                        ):
                            raise PermissionError("session id belongs to another scope")
                        await cursor.execute(
                            "UPDATE acp_sessions SET cwd = %s, last_active_at = %s "
                            "WHERE session_id = %s",
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

    async def get(
        self, principal: str, deployment_id: str, session_id: str
    ) -> SessionRecord | None:
        async with self._lock:
            from psycopg import sql

            connection = self._require_connection()
            async with connection.cursor() as cursor:
                query = sql.SQL(
                    "SELECT {} FROM acp_sessions "
                    "WHERE session_id = %s AND principal = %s AND deployment_id = %s"
                ).format(_columns(sql))
                await cursor.execute(query, (session_id, principal, deployment_id))
                row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def list(
        self,
        principal: str,
        deployment_id: str,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SessionPage:
        if not 1 <= limit <= 100:
            raise ValueError("Session limit must be between 1 and 100")
        from psycopg import sql

        clauses = [sql.SQL("principal = %s"), sql.SQL("deployment_id = %s")]
        values: list[Any] = [principal, deployment_id]
        if cwd is not None:
            clauses.append(sql.SQL("cwd = %s"))
            values.append(cwd)
        if cursor is not None:
            last_active_at, session_id = decode_session_cursor(cursor)
            clauses.append(
                sql.SQL("(last_active_at < %s OR (last_active_at = %s AND session_id > %s))")
            )
            values.extend([last_active_at, last_active_at, session_id])
        query = sql.SQL(
            "SELECT {} FROM acp_sessions WHERE {} "
            "ORDER BY last_active_at DESC, session_id ASC LIMIT %s"
        ).format(_columns(sql), sql.SQL(" AND ").join(clauses))
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor_handle:
                await cursor_handle.execute(query, [*values, limit + 1])
                rows = await cursor_handle.fetchall()
        records = [_record_from_row(row) for row in rows]
        next_cursor = None
        if len(records) > limit:
            records = records[:limit]
            next_cursor = encode_session_cursor(records[-1])
        return SessionPage(records=records, next_cursor=next_cursor)

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
        usage: Mapping[str, Any] | None = None,
        touch: bool = False,
    ) -> None:
        from psycopg import sql

        assignments: list[Any] = []
        values: list[Any] = []
        if title is not None:
            assignments.append(sql.SQL("title = %s"))
            values.append(title)
        if effort is not None:
            assignments.append(sql.SQL("effort = %s"))
            values.append(effort)
        if bindings is not None:
            assignments.append(sql.SQL("bindings = %s"))
            values.append(serialize_bindings(bindings))
        if usage is not None:
            assignments.append(sql.SQL("usage = %s"))
            values.append(json.dumps(dict(usage), separators=(",", ":")))
        if touch:
            assignments.append(sql.SQL("last_active_at = %s"))
            values.append(now_ms())
        if not assignments:
            return
        values.extend([session_id, principal, deployment_id])
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                query = sql.SQL(
                    "UPDATE acp_sessions SET {} "
                    "WHERE session_id = %s AND principal = %s AND deployment_id = %s"
                ).format(sql.SQL(", ").join(assignments))
                await cursor.execute(query, values)

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
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT principal, deployment_id FROM acp_sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    row = _row_dict(await cursor.fetchone())
                    if row is None:
                        raise PermissionError("session was not created in this scope")
                    if row["principal"] != principal or row["deployment_id"] != deployment_id:
                        raise PermissionError("session belongs to another scope")
                    await cursor.execute(
                        "UPDATE acp_sessions SET messages = %s, updates = %s, model = %s, "
                        "last_active_at = %s WHERE session_id = %s",
                        (messages, updates, model, now_ms(), session_id),
                    )

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
                row = _row_dict(await cursor.fetchone())
        if row is None:
            return None
        return decode_session(row["messages"], row["updates"], row["model"])
