"""PostgreSQL persistence for ACP sessions.

Implements the same scoped contract as :mod:`hrns.runtime.store`: one row per
session holds the upstream ``StoredSession`` payload and host metadata, and two side
tables hold the fenced writer lease and the per-session ordered prompt queue. No turn,
transcript, or revision tables are created.

The shared connection runs in autocommit mode so independent reads never leave an idle
transaction behind. Multi-statement mutations use an explicit transaction block, which
rolls back on database errors and cancellation instead of poisoning later requests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .acp_store import (
    SESSION_COLUMNS,
    DeploymentRecord,
    PromptState,
    QueuedPrompt,
    SessionLease,
    SessionPage,
    SessionRecord,
    SessionStore,
    _DeploymentList,
    _PromptList,
    _session_conflict,
    decode_session,
    decode_session_cursor,
    deserialize_bindings,
    deserialize_collaborators,
    encode_session,
    encode_session_cursor,
    now_ms,
    serialize_bindings,
    serialize_collaborators,
)
from .session_policy import SessionAccess

if TYPE_CHECKING:
    from psycopg import AsyncConnection, AsyncCursor
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
    access TEXT NOT NULL DEFAULT 'private',
    owner TEXT,
    collaborators TEXT NOT NULL DEFAULT '[]',
    client_capabilities BOOLEAN,
    created_at BIGINT NOT NULL,
    last_active_at BIGINT NOT NULL
)
"""

_SCHEMA_INDEX = """
CREATE INDEX IF NOT EXISTS idx_acp_sessions_scope
    ON acp_sessions(principal, deployment_id, last_active_at DESC)
"""

_SCHEMA_DEPLOYMENT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_acp_sessions_deployment
    ON acp_sessions(deployment_id, last_active_at DESC)
"""

_SCHEMA_LEASES = """
CREATE TABLE IF NOT EXISTS acp_session_leases (
    session_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    fence BIGINT NOT NULL,
    expires_at BIGINT NOT NULL
)
"""

_SCHEMA_PROMPT_QUEUE = """
CREATE TABLE IF NOT EXISTS acp_prompt_queue (
    prompt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    credential_id TEXT,
    connection_id TEXT,
    sequence BIGINT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt BIGINT NOT NULL DEFAULT 0,
    parent_id TEXT,
    owner_id TEXT,
    fence BIGINT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE(session_id, sequence)
)
"""

_SCHEMA_PROMPT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_acp_prompt_queue_order
    ON acp_prompt_queue(deployment_id, session_id, state, sequence)
"""

_SCHEMA_DEPLOYMENTS = """
CREATE TABLE IF NOT EXISTS acp_deployments (
    agent_id TEXT NOT NULL,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    visible BOOLEAN NOT NULL DEFAULT TRUE,
    retained BOOLEAN NOT NULL DEFAULT TRUE,
    registered_at BIGINT NOT NULL,
    PRIMARY KEY (agent_id, version)
)
"""

_DEPLOYMENT_COLUMNS = "agent_id, version, name, description, visible, retained, registered_at"

_DEPLOYMENT_UPSERT = """
INSERT INTO acp_deployments (
    agent_id, version, name, description, visible, retained, registered_at
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (agent_id, version) DO UPDATE SET
    name = excluded.name,
    description = excluded.description,
    visible = excluded.visible,
    retained = excluded.retained,
    registered_at = excluded.registered_at
"""

_ADDED_COLUMNS = (
    ("access", "TEXT NOT NULL DEFAULT 'private'"),
    ("owner", "TEXT"),
    ("collaborators", "TEXT NOT NULL DEFAULT '[]'"),
    ("client_capabilities", "BOOLEAN"),
)


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
        access=SessionAccess(values["access"] or "private"),
        owner=values["owner"],
        collaborators=deserialize_collaborators(values["collaborators"]),
        accepts_client_capabilities=(
            None if values["client_capabilities"] is None else bool(values["client_capabilities"])
        ),
        created_at=int(values["created_at"]),
        last_active_at=int(values["last_active_at"]),
    )


_PROMPT_INSERT = """
INSERT INTO acp_prompt_queue (
    prompt_id, session_id, principal, deployment_id, credential_id, connection_id,
    sequence, payload, state, attempt, parent_id, owner_id, fence, created_at, updated_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

#: States that may only be reached through ``finish_prompt``.
_TERMINAL_PROMPT_STATES = frozenset(
    {
        PromptState.COMPLETED,
        PromptState.FAILED,
        PromptState.CANCELLED,
        PromptState.UNCERTAIN,
    }
)


def _lease_from_row(row: PgRow) -> SessionLease:
    values = _row_dict(row)
    if values is None:
        raise TypeError("PostgreSQL row factory returned no row")
    return SessionLease(
        session_id=str(values["session_id"]),
        principal=str(values["principal"]),
        deployment_id=str(values["deployment_id"]),
        owner_id=str(values["owner_id"]),
        fence=int(values["fence"]),
        expires_at=int(values["expires_at"]),
    )


def _prompt_from_row(row: PgRow) -> QueuedPrompt:
    values = _row_dict(row)
    if values is None:
        raise TypeError("PostgreSQL row factory returned no row")
    return QueuedPrompt(
        prompt_id=str(values["prompt_id"]),
        session_id=str(values["session_id"]),
        principal=str(values["principal"]),
        deployment_id=str(values["deployment_id"]),
        credential_id=values["credential_id"],
        connection_id=values["connection_id"],
        sequence=int(values["sequence"]),
        payload=str(values["payload"]),
        state=PromptState(values["state"]),
        attempt=int(values["attempt"]),
        parent_id=values["parent_id"],
        owner_id=values["owner_id"],
        fence=None if values["fence"] is None else int(values["fence"]),
        created_at=int(values["created_at"]),
        updated_at=int(values["updated_at"]),
    )


def _deployment_from_row(row: PgRow) -> DeploymentRecord:
    values = _row_dict(row)
    if values is None:
        raise TypeError("PostgreSQL row factory returned no row")
    return DeploymentRecord(
        agent_id=str(values["agent_id"]),
        version=str(values["version"]),
        name=str(values["name"]),
        description=str(values["description"] or ""),
        visible=bool(values["visible"]),
        retained=bool(values["retained"]),
        registered_at=int(values["registered_at"]),
    )


async def _insert_prompt(cursor: AsyncCursor[PgRow], prompt: QueuedPrompt) -> None:
    await cursor.execute(
        _PROMPT_INSERT,
        (
            prompt.prompt_id,
            prompt.session_id,
            prompt.principal,
            prompt.deployment_id,
            prompt.credential_id,
            prompt.connection_id,
            prompt.sequence,
            prompt.payload,
            prompt.state.value,
            prompt.attempt,
            prompt.parent_id,
            prompt.owner_id,
            prompt.fence,
            prompt.created_at,
            prompt.updated_at,
        ),
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
        from psycopg import sql
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
                    await cursor.execute(_SCHEMA_DEPLOYMENT_INDEX)
                    await cursor.execute(_SCHEMA_LEASES)
                    await cursor.execute(_SCHEMA_PROMPT_QUEUE)
                    await cursor.execute(_SCHEMA_PROMPT_INDEX)
                    await cursor.execute(_SCHEMA_DEPLOYMENTS)
                    for name, declaration in _ADDED_COLUMNS:
                        await cursor.execute(
                            sql.SQL(
                                "ALTER TABLE acp_sessions ADD COLUMN IF NOT EXISTS {} {}"
                            ).format(sql.Identifier(name), sql.SQL(declaration))
                        )
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
                                cwd, title, effort, bindings, usage, access, owner, collaborators,
                                client_capabilities, created_at, last_active_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                                record.access.value,
                                record.effective_owner,
                                serialize_collaborators(record.collaborators),
                                record.accepts_client_capabilities,
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
        del principal  # authorized against the session policy by the host
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM acp_sessions WHERE session_id = %s AND deployment_id = %s",
                    (session_id, deployment_id),
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
        del principal  # authorized against the session policy by the host
        values.extend([session_id, deployment_id])
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                query = sql.SQL(
                    "UPDATE acp_sessions SET {} WHERE session_id = %s AND deployment_id = %s"
                ).format(sql.SQL(", ").join(assignments))
                await cursor.execute(query, values)

    async def set_access(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        *,
        access: SessionAccess,
        owner: str | None = None,
        collaborators: frozenset[str] | None = None,
    ) -> bool:
        from psycopg import sql

        assignments: list[Any] = [sql.SQL("access = %s")]
        values: list[Any] = [access.value]
        if owner is not None:
            assignments.append(sql.SQL("owner = %s"))
            values.append(owner)
        if collaborators is not None:
            assignments.append(sql.SQL("collaborators = %s"))
            values.append(serialize_collaborators(collaborators))
        values.extend([session_id, deployment_id, principal])
        query = sql.SQL(
            "UPDATE acp_sessions SET {} WHERE session_id = %s AND deployment_id = %s "
            "AND COALESCE(owner, principal) = %s"
        ).format(sql.SQL(", ").join(assignments))
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(query, values)
                changed = cursor.rowcount
        return bool(changed)

    async def set_client_capabilities(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        accepts: bool | None,
    ) -> bool:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "UPDATE acp_sessions SET client_capabilities = %s "
                    "WHERE session_id = %s AND deployment_id = %s "
                    "AND COALESCE(owner, principal) = %s",
                    (accepts, session_id, deployment_id, principal),
                )
                changed = cursor.rowcount
        return bool(changed)

    async def get_visible(
        self, deployment_id: str, session_id: str, principal: str
    ) -> SessionRecord | None:
        from psycopg import sql

        query = sql.SQL(
            "SELECT {} FROM acp_sessions WHERE session_id = %s AND deployment_id = %s "
            "AND (COALESCE(owner, principal) = %s "
            "OR (access != 'private' AND collaborators::jsonb ? %s))"
        ).format(_columns(sql))
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(query, (session_id, deployment_id, principal, principal))
                row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def list_visible(
        self,
        deployment_id: str,
        principal: str,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SessionPage:
        if not 1 <= limit <= 100:
            raise ValueError("Session limit must be between 1 and 100")
        from psycopg import sql

        clauses = [
            sql.SQL("deployment_id = %s"),
            sql.SQL(
                "(COALESCE(owner, principal) = %s "
                "OR (access != 'private' AND collaborators::jsonb ? %s))"
            ),
        ]
        values: list[Any] = [deployment_id, principal, principal]
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

    async def save_scoped(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        session: "StoredSession",
        *,
        fence: int | None = None,
    ) -> None:
        messages, updates, model = encode_session(session)
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT deployment_id FROM acp_sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    row = _row_dict(await cursor.fetchone())
                    if row is None:
                        raise PermissionError("session was not created in this scope")
                    if row["deployment_id"] != deployment_id:
                        raise PermissionError("session belongs to another deployment")
                    if fence is not None:
                        await cursor.execute(
                            "SELECT fence FROM acp_session_leases "
                            "WHERE session_id = %s AND deployment_id = %s FOR UPDATE",
                            (session_id, deployment_id),
                        )
                        lease = _row_dict(await cursor.fetchone())
                        if lease is None or int(lease["fence"]) != fence:
                            raise _session_conflict(deployment_id, session_id, fence)
                    await cursor.execute(
                        "UPDATE acp_sessions SET messages = %s, updates = %s, model = %s, "
                        "last_active_at = %s WHERE session_id = %s",
                        (messages, updates, model, now_ms(), session_id),
                    )

    async def load_scoped(
        self, principal: str, deployment_id: str, session_id: str
    ) -> StoredSession | None:
        del principal  # read authorization is the session policy's job
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT messages, updates, model FROM acp_sessions "
                    "WHERE session_id = %s AND deployment_id = %s",
                    (session_id, deployment_id),
                )
                row = _row_dict(await cursor.fetchone())
        if row is None:
            return None
        return decode_session(row["messages"], row["updates"], row["model"])

    # -- Cross-worker session leases -----------------------------------------

    async def acquire_lease(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        ttl_ms: int,
    ) -> SessionLease | None:
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    active = now_ms()
                    expires_at = active + ttl_ms
                    lease = SessionLease(
                        session_id=session_id,
                        principal=principal,
                        deployment_id=deployment_id,
                        owner_id=owner_id,
                        fence=1,
                        expires_at=expires_at,
                    )
                    await cursor.execute(
                        "INSERT INTO acp_session_leases "
                        "(session_id, principal, deployment_id, owner_id, fence, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (session_id) DO NOTHING",
                        (
                            lease.session_id,
                            lease.principal,
                            lease.deployment_id,
                            lease.owner_id,
                            lease.fence,
                            lease.expires_at,
                        ),
                    )
                    if cursor.rowcount:
                        return lease
                    await cursor.execute(
                        "SELECT fence, owner_id, expires_at FROM acp_session_leases "
                        "WHERE session_id = %s FOR UPDATE",
                        (session_id,),
                    )
                    row = _row_dict(await cursor.fetchone())
                    if row is None:
                        return None
                    fence = int(row["fence"])
                    if row["owner_id"] != owner_id:
                        if int(row["expires_at"]) > active:
                            return None
                        fence += 1
                    lease = SessionLease(
                        session_id=session_id,
                        principal=principal,
                        deployment_id=deployment_id,
                        owner_id=owner_id,
                        fence=fence,
                        expires_at=expires_at,
                    )
                    await cursor.execute(
                        "UPDATE acp_session_leases SET principal = %s, deployment_id = %s, "
                        "owner_id = %s, fence = %s, expires_at = %s WHERE session_id = %s",
                        (
                            lease.principal,
                            lease.deployment_id,
                            lease.owner_id,
                            lease.fence,
                            lease.expires_at,
                            lease.session_id,
                        ),
                    )
                    return lease

    async def renew_lease(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        ttl_ms: int,
    ) -> SessionLease | None:
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    active = now_ms()
                    expires_at = active + ttl_ms
                    await cursor.execute(
                        "UPDATE acp_session_leases SET principal = %s, expires_at = %s "
                        "WHERE session_id = %s AND deployment_id = %s AND owner_id = %s "
                        "AND expires_at > %s RETURNING fence",
                        (principal, expires_at, session_id, deployment_id, owner_id, active),
                    )
                    row = _row_dict(await cursor.fetchone())
                    if row is None:
                        return None
                    return SessionLease(
                        session_id=session_id,
                        principal=principal,
                        deployment_id=deployment_id,
                        owner_id=owner_id,
                        fence=int(row["fence"]),
                        expires_at=expires_at,
                    )

    async def release_lease(
        self, principal: str, deployment_id: str, session_id: str, owner_id: str
    ) -> bool:
        del principal  # the owner id is the only credential the lease keeps
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                # Expire rather than delete so the fence never moves backwards.
                await cursor.execute(
                    "UPDATE acp_session_leases SET expires_at = 0 WHERE session_id = %s "
                    "AND deployment_id = %s AND owner_id = %s",
                    (session_id, deployment_id, owner_id),
                )
                deleted = cursor.rowcount
        return bool(deleted)

    async def get_lease(self, deployment_id: str, session_id: str) -> SessionLease | None:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM acp_session_leases WHERE session_id = %s AND deployment_id = %s",
                    (session_id, deployment_id),
                )
                row = await cursor.fetchone()
        return _lease_from_row(row) if row is not None else None

    # -- Durable per-session prompt queue ------------------------------------

    async def enqueue_prompt(self, prompt: QueuedPrompt) -> QueuedPrompt:
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    active = now_ms()
                    sequence = await self._next_sequence(cursor, prompt.session_id)
                    stored = replace(
                        prompt,
                        sequence=sequence,
                        created_at=prompt.created_at or active,
                        updated_at=prompt.updated_at or prompt.created_at or active,
                    )
                    await _insert_prompt(cursor, stored)
                    return stored

    async def claim_prompt(self, prompt_id: str, owner_id: str, fence: int) -> QueuedPrompt | None:
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM acp_prompt_queue WHERE prompt_id = %s AND state = %s "
                        "FOR UPDATE",
                        (prompt_id, PromptState.QUEUED.value),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    queued = _prompt_from_row(row)
                    active = now_ms()
                    attempt = queued.attempt + 1
                    await cursor.execute(
                        "UPDATE acp_prompt_queue SET state = %s, owner_id = %s, fence = %s, "
                        "attempt = %s, updated_at = %s WHERE prompt_id = %s",
                        (
                            PromptState.CLAIMED.value,
                            owner_id,
                            fence,
                            attempt,
                            active,
                            prompt_id,
                        ),
                    )
                    return replace(
                        queued,
                        state=PromptState.CLAIMED,
                        owner_id=owner_id,
                        fence=fence,
                        attempt=attempt,
                        updated_at=active,
                    )

    async def claim_next_prompt(
        self,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        fence: int,
        ttl_ms: int,
    ) -> QueuedPrompt | None:
        del ttl_ms  # the caller owns the lease; claiming does not extend it
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    # Never return an already-claimed row: an interrupted attempt must
                    # not be executed a second time.
                    await cursor.execute(
                        "SELECT * FROM acp_prompt_queue WHERE deployment_id = %s "
                        "AND session_id = %s AND state = %s ORDER BY sequence LIMIT 1 FOR UPDATE",
                        (deployment_id, session_id, PromptState.QUEUED.value),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    queued = _prompt_from_row(row)
                    active = now_ms()
                    attempt = queued.attempt + 1
                    await cursor.execute(
                        "UPDATE acp_prompt_queue SET state = %s, owner_id = %s, fence = %s, "
                        "attempt = %s, updated_at = %s WHERE prompt_id = %s",
                        (
                            PromptState.CLAIMED.value,
                            owner_id,
                            fence,
                            attempt,
                            active,
                            queued.prompt_id,
                        ),
                    )
                    return replace(
                        queued,
                        state=PromptState.CLAIMED,
                        owner_id=owner_id,
                        fence=fence,
                        attempt=attempt,
                        updated_at=active,
                    )

    async def finish_prompt(
        self,
        prompt_id: str,
        state: PromptState,
        *,
        owner_id: str,
        fence: int,
    ) -> bool:
        if state not in _TERMINAL_PROMPT_STATES:
            raise ValueError(f"{state.value!r} is not a terminal prompt state")
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "UPDATE acp_prompt_queue SET state = %s, updated_at = %s "
                    "WHERE prompt_id = %s AND state = %s AND owner_id = %s AND fence = %s",
                    (
                        state.value,
                        now_ms(),
                        prompt_id,
                        PromptState.CLAIMED.value,
                        owner_id,
                        fence,
                    ),
                )
                changed = cursor.rowcount
        return bool(changed)

    async def get_prompt(self, prompt_id: str) -> QueuedPrompt | None:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM acp_prompt_queue WHERE prompt_id = %s", (prompt_id,)
                )
                row = await cursor.fetchone()
        return _prompt_from_row(row) if row is not None else None

    async def list_prompts(
        self,
        deployment_id: str,
        session_id: str,
        *,
        states: Sequence[PromptState] | None = None,
        limit: int = 100,
    ) -> _PromptList:
        if limit < 1:
            raise ValueError("Prompt limit must be at least 1")
        from psycopg import sql

        clauses = [sql.SQL("deployment_id = %s"), sql.SQL("session_id = %s")]
        values: list[Any] = [deployment_id, session_id]
        if states:
            clauses.append(sql.SQL("state = ANY(%s)"))
            values.append([state.value for state in states])
        query = sql.SQL(
            "SELECT * FROM acp_prompt_queue WHERE {} ORDER BY sequence LIMIT %s"
        ).format(sql.SQL(" AND ").join(clauses))
        values.append(limit)
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(query, values)
                rows = await cursor.fetchall()
        return [_prompt_from_row(row) for row in rows]

    async def mark_uncertain_prompts(
        self,
        owner_id: str,
        *,
        deployment_id: str | None = None,
        session_id: str | None = None,
    ) -> _PromptList:
        from psycopg import sql

        clauses = [sql.SQL("owner_id = %s"), sql.SQL("state = %s")]
        values: list[Any] = [owner_id, PromptState.CLAIMED.value]
        if deployment_id is not None:
            clauses.append(sql.SQL("deployment_id = %s"))
            values.append(deployment_id)
        if session_id is not None:
            clauses.append(sql.SQL("session_id = %s"))
            values.append(session_id)
        query = sql.SQL(
            "UPDATE acp_prompt_queue SET state = %s, updated_at = %s WHERE {} RETURNING *"
        ).format(sql.SQL(" AND ").join(clauses))
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        query,
                        [PromptState.UNCERTAIN.value, now_ms(), *values],
                    )
                    rows = await cursor.fetchall()
        affected = [_prompt_from_row(row) for row in rows]
        affected.sort(key=lambda prompt: prompt.sequence)
        return affected

    async def retry_prompt(
        self,
        prompt_id: str,
        *,
        session_id: str,
        new_prompt_id: str,
        owner_id: str,
        fence: int,
    ) -> QueuedPrompt | None:
        del owner_id, fence  # the caller re-queues; the old claim stays as history
        async with self._lock:
            connection = self._require_connection()
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM acp_prompt_queue WHERE prompt_id = %s "
                        "AND session_id = %s FOR UPDATE",
                        (prompt_id, session_id),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    original = _prompt_from_row(row)
                    if original.state is not PromptState.UNCERTAIN:
                        return None
                    active = now_ms()
                    sequence = await self._next_sequence(cursor, original.session_id)
                    stored = replace(
                        original,
                        prompt_id=new_prompt_id,
                        sequence=sequence,
                        state=PromptState.QUEUED,
                        attempt=0,
                        parent_id=prompt_id,
                        owner_id=None,
                        fence=None,
                        created_at=active,
                        updated_at=active,
                    )
                    await _insert_prompt(cursor, stored)
                    return stored

    # -- Deployment revision metadata ----------------------------------------

    async def upsert_deployment(self, record: DeploymentRecord) -> None:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    _DEPLOYMENT_UPSERT,
                    (
                        record.agent_id,
                        record.version,
                        record.name,
                        record.description,
                        record.visible,
                        record.retained,
                        record.registered_at,
                    ),
                )

    async def list_deployments(self) -> _DeploymentList:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_DEPLOYMENT_COLUMNS} FROM acp_deployments "
                    "ORDER BY agent_id ASC, registered_at ASC, version ASC"
                )
                rows = await cursor.fetchall()
        return [_deployment_from_row(row) for row in rows]

    async def set_deployment_visible(self, agent_id: str, version: str, visible: bool) -> bool:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "UPDATE acp_deployments SET visible = %s WHERE agent_id = %s AND version = %s",
                    (visible, agent_id, version),
                )
                changed = cursor.rowcount
        return bool(changed)

    async def set_deployment_retained(self, agent_id: str, version: str, retained: bool) -> bool:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "UPDATE acp_deployments SET retained = %s WHERE agent_id = %s AND version = %s",
                    (retained, agent_id, version),
                )
                changed = cursor.rowcount
        return bool(changed)

    async def delete_deployment(self, agent_id: str, version: str) -> bool:
        async with self._lock:
            connection = self._require_connection()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM acp_deployments "
                    "WHERE agent_id = %s AND version = %s AND retained = FALSE",
                    (agent_id, version),
                )
                changed = cursor.rowcount
        return bool(changed)

    async def _next_sequence(self, cursor: AsyncCursor[PgRow], session_id: str) -> int:
        """Next per-session sequence number, serialized by a transaction-scoped lock."""
        await cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (session_id,))
        await cursor.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM acp_prompt_queue "
            "WHERE session_id = %s",
            (session_id,),
        )
        row = _row_dict(await cursor.fetchone())
        if row is None:
            return 1
        return int(row["sequence"]) + 1
