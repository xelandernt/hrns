"""SQLite persistence for ACP sessions.

The schema is deliberately small and fresh: one row per session holds the upstream
``StoredSession`` payload plus host-owned metadata. Cross-worker coordination adds two
side tables: ``acp_session_leases`` (fenced writer leases) and ``acp_prompt_queue`` (the
per-session ordered prompt queue). There is no turn table, transcript journal, or
revision log. An existing database is opened without being converted; missing tables and
columns are created in place.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from .acp_store import (
    SESSION_COLUMNS,
    DeploymentRecord,
    OperationKind,
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
    from pydantic_ai_harness.experimental.acp import StoredSession

__all__ = ["SQLiteSessionStore", "SessionStore", "SessionRecord", "default_store_path", "now_ms"]

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
    access TEXT NOT NULL DEFAULT 'private',
    owner TEXT,
    collaborators TEXT NOT NULL DEFAULT '[]',
    client_capabilities INTEGER,
    next_update_revision INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acp_sessions_scope
    ON acp_sessions(principal, deployment_id, last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_acp_sessions_deployment
    ON acp_sessions(deployment_id, last_active_at DESC);
CREATE TABLE IF NOT EXISTS acp_session_leases (
  session_id TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  deployment_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  fence INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS acp_prompt_queue (
  prompt_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  principal TEXT NOT NULL,
  deployment_id TEXT NOT NULL,
  credential_id TEXT,
  connection_id TEXT,
  sequence INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'prompt',
  payload TEXT NOT NULL,
  state TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  parent_id TEXT,
  owner_id TEXT,
  fence INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(session_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_acp_prompt_queue_order
    ON acp_prompt_queue(deployment_id, session_id, state, sequence);
CREATE TABLE IF NOT EXISTS acp_deployments (
    agent_id TEXT NOT NULL,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    visible INTEGER NOT NULL DEFAULT 1,
    retained INTEGER NOT NULL DEFAULT 1,
    registered_at INTEGER NOT NULL,
    PRIMARY KEY (agent_id, version)
);
"""

#: Columns added after the first released schema. Existing databases gain them in place.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("access", "TEXT NOT NULL DEFAULT 'private'"),
    ("owner", "TEXT"),
    ("collaborators", "TEXT NOT NULL DEFAULT '[]'"),
    ("client_capabilities", "INTEGER"),
    ("next_update_revision", "INTEGER NOT NULL DEFAULT 1"),
)

_ADDED_QUEUE_COLUMNS: tuple[tuple[str, str], ...] = (("kind", "TEXT NOT NULL DEFAULT 'prompt'"),)


def default_store_path() -> Path:
    """Host default store location: ``HRNS_STORE`` when set, else the user data dir."""
    explicit = os.environ.get("HRNS_STORE")
    if explicit:
        return Path(explicit).expanduser()
    import platformdirs

    return Path(platformdirs.user_data_dir("hrns", "hrns")) / "runtime.sqlite3"


_ResultT = TypeVar("_ResultT")

#: States that may only be reached through ``finish_prompt``.
_TERMINAL_PROMPT_STATES = frozenset(
    {
        PromptState.COMPLETED,
        PromptState.FAILED,
        PromptState.CANCELLED,
        PromptState.UNCERTAIN,
    }
)

_PROMPT_INSERT = """
INSERT INTO acp_prompt_queue (
    prompt_id, session_id, principal, deployment_id, credential_id, connection_id,
    sequence, kind, payload, state, attempt, parent_id, owner_id, fence, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_DEPLOYMENT_UPSERT = """
INSERT INTO acp_deployments (
    agent_id, version, name, description, visible, retained, registered_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(agent_id, version) DO UPDATE SET
    name = excluded.name,
    description = excluded.description,
    visible = excluded.visible,
    retained = excluded.retained,
    registered_at = excluded.registered_at
"""

_DEPLOYMENT_COLUMNS = "agent_id, version, name, description, visible, retained, registered_at"


@contextmanager
def _immediate(connection: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """Run a block in one ``BEGIN IMMEDIATE`` transaction, rolling back on failure."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _row_to_record(row: sqlite3.Row) -> SessionRecord:
    values = dict(row)
    return SessionRecord(
        session_id=values["session_id"],
        principal=values["principal"],
        deployment_id=values["deployment_id"],
        agent_id=values["agent_id"],
        definition_version=values["definition_version"],
        cwd=values["cwd"],
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


def _row_to_lease(row: sqlite3.Row) -> SessionLease:
    values = dict(row)
    return SessionLease(
        session_id=str(values["session_id"]),
        principal=str(values["principal"]),
        deployment_id=str(values["deployment_id"]),
        owner_id=str(values["owner_id"]),
        fence=int(values["fence"]),
        expires_at=int(values["expires_at"]),
    )


def _row_to_prompt(row: sqlite3.Row) -> QueuedPrompt:
    values = dict(row)
    return QueuedPrompt(
        prompt_id=str(values["prompt_id"]),
        session_id=str(values["session_id"]),
        principal=str(values["principal"]),
        deployment_id=str(values["deployment_id"]),
        credential_id=values["credential_id"],
        connection_id=values["connection_id"],
        sequence=int(values["sequence"]),
        kind=OperationKind(values["kind"]),
        payload=str(values["payload"]),
        state=PromptState(values["state"]),
        attempt=int(values["attempt"]),
        parent_id=values["parent_id"],
        owner_id=values["owner_id"],
        fence=None if values["fence"] is None else int(values["fence"]),
        created_at=int(values["created_at"]),
        updated_at=int(values["updated_at"]),
    )


def _row_to_deployment(row: sqlite3.Row) -> DeploymentRecord:
    values = dict(row)
    return DeploymentRecord(
        agent_id=str(values["agent_id"]),
        version=str(values["version"]),
        name=str(values["name"]),
        description=str(values["description"] or ""),
        visible=bool(values["visible"]),
        retained=bool(values["retained"]),
        registered_at=int(values["registered_at"]),
    )


def _next_sequence(connection: sqlite3.Connection, session_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM acp_prompt_queue WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["sequence"]) + 1


def _insert_prompt(connection: sqlite3.Connection, prompt: QueuedPrompt) -> None:
    connection.execute(
        _PROMPT_INSERT,
        (
            prompt.prompt_id,
            prompt.session_id,
            prompt.principal,
            prompt.deployment_id,
            prompt.credential_id,
            prompt.connection_id,
            prompt.sequence,
            prompt.kind.value,
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


class SQLiteSessionStore(SessionStore):
    """SQLite WAL store shared by connection-local scoped views."""

    def __init__(self, path: str | Path | None = None, *, busy_timeout: float = 10.0) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self._memory = str(self.path) == ":memory:"
        self._busy_timeout = busy_timeout
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            ":memory:" if self._memory else str(self.path),
            timeout=self._busy_timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        if not self._memory:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout * 1000)}")
        self._connection = connection
        return connection

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        connection = self._connect()
        connection.executescript(_SCHEMA)
        existing = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(acp_sessions)")
        }
        for name, declaration in _ADDED_COLUMNS:
            if name not in existing:
                connection.execute(f"ALTER TABLE acp_sessions ADD COLUMN {name} {declaration}")
        existing_queue = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(acp_prompt_queue)")
        }
        for name, declaration in _ADDED_QUEUE_COLUMNS:
            if name not in existing_queue:
                connection.execute(f"ALTER TABLE acp_prompt_queue ADD COLUMN {name} {declaration}")

    async def close(self) -> None:
        async with self._lock:
            connection = self._connection
            self._connection = None
            self._initialized = False
            if connection is not None:
                await asyncio.to_thread(connection.close)

    def _run(self, operation: Callable[..., _ResultT], *args: Any) -> _ResultT:
        if not self._initialized:
            raise RuntimeError("SessionStore.initialize() must run before use")
        return operation(*args)

    async def create(self, record: SessionRecord) -> None:
        record.created_at = record.created_at or now_ms()
        record.last_active_at = record.last_active_at or record.created_at

        def operation() -> None:
            connection = self._connect()
            existing = connection.execute(
                "SELECT principal, deployment_id FROM acp_sessions WHERE session_id = ?",
                (record.session_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["principal"] != record.principal
                    or existing["deployment_id"] != record.deployment_id
                ):
                    raise PermissionError("session id belongs to another scope")
                connection.execute(
                    "UPDATE acp_sessions SET cwd = ?, last_active_at = ? WHERE session_id = ?",
                    (record.cwd, record.last_active_at, record.session_id),
                )
                return
            connection.execute(
                """
                INSERT INTO acp_sessions (
                    session_id, principal, deployment_id, agent_id, definition_version,
                    cwd, title, effort, bindings, usage, messages, updates, model,
                    access, owner, collaborators, client_capabilities,
                    created_at, last_active_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', NULL, ?, ?, ?, ?, ?, ?)
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
                    (
                        None
                        if record.accepts_client_capabilities is None
                        else int(record.accepts_client_capabilities)
                    ),
                    record.created_at,
                    record.last_active_at,
                ),
            )

        async with self._lock:
            await asyncio.to_thread(self._run, operation)

    async def get(
        self, principal: str, deployment_id: str, session_id: str
    ) -> SessionRecord | None:
        async with self._lock:
            row = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        f"SELECT {SESSION_COLUMNS} FROM acp_sessions "
                        "WHERE session_id = ? AND principal = ? AND deployment_id = ?",
                        (session_id, principal, deployment_id),
                    )
                    .fetchone()
                ),
            )
        return _row_to_record(row) if row is not None else None

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
        clauses = ["principal = ?", "deployment_id = ?"]
        values: list[Any] = [principal, deployment_id]
        if cwd is not None:
            clauses.append("cwd = ?")
            values.append(cwd)
        if cursor is not None:
            last_active_at, session_id = decode_session_cursor(cursor)
            clauses.append("(last_active_at < ? OR (last_active_at = ? AND session_id > ?))")
            values.extend([last_active_at, last_active_at, session_id])
        query = (
            f"SELECT {SESSION_COLUMNS} FROM acp_sessions WHERE {' AND '.join(clauses)} "
            "ORDER BY last_active_at DESC, session_id ASC LIMIT ?"
        )
        async with self._lock:
            rows = await asyncio.to_thread(
                self._run,
                lambda: self._connect().execute(query, [*values, limit + 1]).fetchall(),
            )
        records = [_row_to_record(row) for row in rows]
        next_cursor = None
        if len(records) > limit:
            records = records[:limit]
            next_cursor = encode_session_cursor(records[-1])
        return SessionPage(records=records, next_cursor=next_cursor)

    async def delete(self, principal: str, deployment_id: str, session_id: str) -> bool:
        # The host authorizes the caller against the session policy before calling this.
        # The row's creator principal is not the caller for a shared session, so scope the
        # physical delete to the deployment only.
        del principal
        async with self._lock:
            changed = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "DELETE FROM acp_sessions WHERE session_id = ? AND deployment_id = ?",
                        (session_id, deployment_id),
                    )
                    .rowcount
                ),
            )
        return bool(changed)

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
        assignments: list[str] = []
        values: list[Any] = []
        if title is not None:
            assignments.append("title = ?")
            values.append(title)
        if effort is not None:
            assignments.append("effort = ?")
            values.append(effort)
        if bindings is not None:
            assignments.append("bindings = ?")
            values.append(serialize_bindings(bindings))
        if usage is not None:
            assignments.append("usage = ?")
            values.append(json.dumps(dict(usage), separators=(",", ":")))
        if touch:
            assignments.append("last_active_at = ?")
            values.append(now_ms())
        if not assignments:
            return
        del principal  # authorization happened against the session policy in the host
        values.extend([session_id, deployment_id])

        async with self._lock:
            await asyncio.to_thread(
                self._run,
                lambda: self._connect().execute(
                    f"UPDATE acp_sessions SET {', '.join(assignments)} "
                    "WHERE session_id = ? AND deployment_id = ?",
                    values,
                ),
            )

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
        assignments = ["access = ?"]
        values: list[Any] = [access.value]
        if owner is not None:
            assignments.append("owner = ?")
            values.append(owner)
        if collaborators is not None:
            assignments.append("collaborators = ?")
            values.append(serialize_collaborators(collaborators))
        values.extend([session_id, deployment_id, principal])

        async with self._lock:
            changed = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        f"UPDATE acp_sessions SET {', '.join(assignments)} "
                        "WHERE session_id = ? AND deployment_id = ? "
                        "AND COALESCE(owner, principal) = ?",
                        values,
                    )
                    .rowcount
                ),
            )
        return bool(changed)

    async def set_client_capabilities(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        accepts: bool | None,
    ) -> bool:
        async with self._lock:
            changed = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "UPDATE acp_sessions SET client_capabilities = ? "
                        "WHERE session_id = ? AND deployment_id = ? "
                        "AND COALESCE(owner, principal) = ?",
                        (
                            None if accepts is None else int(accepts),
                            session_id,
                            deployment_id,
                            principal,
                        ),
                    )
                    .rowcount
                ),
            )
        return bool(changed)

    async def get_visible(
        self, deployment_id: str, session_id: str, principal: str
    ) -> SessionRecord | None:
        query = (
            f"SELECT {SESSION_COLUMNS} FROM acp_sessions WHERE session_id = ? "
            "AND deployment_id = ? AND (COALESCE(owner, principal) = ? "
            "OR (access != 'private' AND EXISTS ("
            "SELECT 1 FROM json_each(acp_sessions.collaborators) "
            "WHERE json_each.value = ?)))"
        )
        async with self._lock:
            row = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(query, (session_id, deployment_id, principal, principal))
                    .fetchone()
                ),
            )
        return _row_to_record(row) if row is not None else None

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
        clauses = [
            "deployment_id = ?",
            "(COALESCE(owner, principal) = ? OR (access != 'private' AND EXISTS ("
            "SELECT 1 FROM json_each(acp_sessions.collaborators) "
            "WHERE json_each.value = ?)))",
        ]
        values: list[Any] = [deployment_id, principal, principal]
        if cwd is not None:
            clauses.append("cwd = ?")
            values.append(cwd)
        if cursor is not None:
            last_active_at, session_id = decode_session_cursor(cursor)
            clauses.append("(last_active_at < ? OR (last_active_at = ? AND session_id > ?))")
            values.extend([last_active_at, last_active_at, session_id])
        query = (
            f"SELECT {SESSION_COLUMNS} FROM acp_sessions WHERE {' AND '.join(clauses)} "
            "ORDER BY last_active_at DESC, session_id ASC LIMIT ?"
        )
        async with self._lock:
            rows = await asyncio.to_thread(
                self._run,
                lambda: self._connect().execute(query, [*values, limit + 1]).fetchall(),
            )
        records = [_row_to_record(row) for row in rows]
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
        active = now_ms()

        def operation() -> None:
            connection = self._connect()
            with _immediate(connection):
                row = connection.execute(
                    "SELECT deployment_id FROM acp_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise PermissionError("session was not created in this scope")
                if row["deployment_id"] != deployment_id:
                    raise PermissionError("session belongs to another deployment")
                if fence is not None:
                    lease = connection.execute(
                        "SELECT fence FROM acp_session_leases "
                        "WHERE session_id = ? AND deployment_id = ?",
                        (session_id, deployment_id),
                    ).fetchone()
                    if lease is None or int(lease["fence"]) != fence:
                        raise _session_conflict(deployment_id, session_id, fence)
                connection.execute(
                    "UPDATE acp_sessions SET messages = ?, updates = ?, model = ?, "
                    "last_active_at = ? WHERE session_id = ?",
                    (messages, updates, model, active, session_id),
                )

        async with self._lock:
            await asyncio.to_thread(self._run, operation)

    async def load_scoped(
        self, principal: str, deployment_id: str, session_id: str
    ) -> StoredSession | None:
        # Read authorization is the session policy's job; the physical scope is the
        # deployment so a shared session is readable by every admitted principal.
        del principal
        async with self._lock:
            row = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "SELECT messages, updates, model FROM acp_sessions "
                        "WHERE session_id = ? AND deployment_id = ?",
                        (session_id, deployment_id),
                    )
                    .fetchone()
                ),
            )
        if row is None:
            return None
        return decode_session(row["messages"], row["updates"], row["model"])

    async def reserve_update_revisions(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        count: int,
        *,
        fence: int | None = None,
    ) -> int:
        del principal
        if count < 1:
            raise ValueError("update revision reservation count must be positive")

        def operation() -> int:
            connection = self._connect()
            with _immediate(connection):
                row = connection.execute(
                    "SELECT deployment_id, next_update_revision FROM acp_sessions "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise PermissionError("session was not created in this scope")
                if row["deployment_id"] != deployment_id:
                    raise PermissionError("session belongs to another deployment")
                if fence is not None:
                    lease = connection.execute(
                        "SELECT fence FROM acp_session_leases "
                        "WHERE session_id = ? AND deployment_id = ?",
                        (session_id, deployment_id),
                    ).fetchone()
                    if lease is None or int(lease["fence"]) != fence:
                        raise _session_conflict(deployment_id, session_id, fence)
                first = int(row["next_update_revision"])
                connection.execute(
                    "UPDATE acp_sessions SET next_update_revision = ? WHERE session_id = ?",
                    (first + count, session_id),
                )
                return first

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

    # -- Cross-worker session leases -----------------------------------------

    async def acquire_lease(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        ttl_ms: int,
    ) -> SessionLease | None:
        def operation() -> SessionLease | None:
            connection = self._connect()
            with _immediate(connection):
                active = now_ms()
                expires_at = active + ttl_ms
                row = connection.execute(
                    "SELECT fence, owner_id, expires_at FROM acp_session_leases "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    lease = SessionLease(
                        session_id=session_id,
                        principal=principal,
                        deployment_id=deployment_id,
                        owner_id=owner_id,
                        fence=1,
                        expires_at=expires_at,
                    )
                    connection.execute(
                        "INSERT INTO acp_session_leases "
                        "(session_id, principal, deployment_id, owner_id, fence, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            lease.session_id,
                            lease.principal,
                            lease.deployment_id,
                            lease.owner_id,
                            lease.fence,
                            lease.expires_at,
                        ),
                    )
                    return lease
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
                connection.execute(
                    "UPDATE acp_session_leases SET principal = ?, deployment_id = ?, "
                    "owner_id = ?, fence = ?, expires_at = ? WHERE session_id = ?",
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

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

    async def renew_lease(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        ttl_ms: int,
    ) -> SessionLease | None:
        def operation() -> SessionLease | None:
            connection = self._connect()
            with _immediate(connection):
                active = now_ms()
                expires_at = active + ttl_ms
                changed = connection.execute(
                    "UPDATE acp_session_leases SET principal = ?, expires_at = ? "
                    "WHERE session_id = ? AND deployment_id = ? AND owner_id = ? "
                    "AND expires_at > ?",
                    (principal, expires_at, session_id, deployment_id, owner_id, active),
                ).rowcount
                if not changed:
                    return None
                row = connection.execute(
                    "SELECT fence FROM acp_session_leases WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
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

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

    async def release_lease(
        self, principal: str, deployment_id: str, session_id: str, owner_id: str
    ) -> bool:
        del principal  # the owner id is the only credential the lease keeps

        def operation() -> bool:
            connection = self._connect()
            with _immediate(connection):
                # Expire rather than delete: the fence must never move backwards, or a
                # released-and-reacquired session would accept the old actor's save.
                return bool(
                    connection.execute(
                        "UPDATE acp_session_leases SET expires_at = 0 WHERE session_id = ? "
                        "AND deployment_id = ? AND owner_id = ?",
                        (session_id, deployment_id, owner_id),
                    ).rowcount
                )

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

    async def get_lease(self, deployment_id: str, session_id: str) -> SessionLease | None:
        async with self._lock:
            row = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "SELECT * FROM acp_session_leases "
                        "WHERE session_id = ? AND deployment_id = ?",
                        (session_id, deployment_id),
                    )
                    .fetchone()
                ),
            )
        return _row_to_lease(row) if row is not None else None

    # -- Durable per-session prompt queue ------------------------------------

    async def enqueue_prompt(self, prompt: QueuedPrompt) -> QueuedPrompt:
        def operation() -> QueuedPrompt:
            connection = self._connect()
            with _immediate(connection):
                active = now_ms()
                stored = replace(
                    prompt,
                    sequence=_next_sequence(connection, prompt.session_id),
                    created_at=prompt.created_at or active,
                    updated_at=prompt.updated_at or prompt.created_at or active,
                )
                _insert_prompt(connection, stored)
                return stored

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

    async def claim_prompt(self, prompt_id: str, owner_id: str, fence: int) -> QueuedPrompt | None:
        def operation() -> QueuedPrompt | None:
            connection = self._connect()
            with _immediate(connection):
                row = connection.execute(
                    "SELECT * FROM acp_prompt_queue WHERE prompt_id = ? AND state = ?",
                    (prompt_id, PromptState.QUEUED.value),
                ).fetchone()
                if row is None:
                    return None
                active = now_ms()
                attempt = int(row["attempt"]) + 1
                connection.execute(
                    "UPDATE acp_prompt_queue SET state = ?, owner_id = ?, fence = ?, "
                    "attempt = ?, updated_at = ? WHERE prompt_id = ?",
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
                    _row_to_prompt(row),
                    state=PromptState.CLAIMED,
                    owner_id=owner_id,
                    fence=fence,
                    attempt=attempt,
                    updated_at=active,
                )

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

    async def claim_next_prompt(
        self,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        fence: int,
        ttl_ms: int,
    ) -> QueuedPrompt | None:
        del ttl_ms  # the caller owns the lease; claiming does not extend it

        def operation() -> QueuedPrompt | None:
            connection = self._connect()
            with _immediate(connection):
                # The earliest non-terminal operation is authoritative. A stale claim
                # blocks later work until recovery marks it uncertain; skipping it
                # would silently overtake an interrupted turn.
                row = connection.execute(
                    "SELECT * FROM acp_prompt_queue WHERE deployment_id = ? "
                    "AND session_id = ? AND state IN (?, ?) ORDER BY sequence LIMIT 1",
                    (
                        deployment_id,
                        session_id,
                        PromptState.QUEUED.value,
                        PromptState.CLAIMED.value,
                    ),
                ).fetchone()
                if row is None or row["state"] != PromptState.QUEUED.value:
                    return None
                active = now_ms()
                attempt = int(row["attempt"]) + 1
                connection.execute(
                    "UPDATE acp_prompt_queue SET state = ?, owner_id = ?, fence = ?, "
                    "attempt = ?, updated_at = ? WHERE prompt_id = ?",
                    (
                        PromptState.CLAIMED.value,
                        owner_id,
                        fence,
                        attempt,
                        active,
                        row["prompt_id"],
                    ),
                )
                return replace(
                    _row_to_prompt(row),
                    state=PromptState.CLAIMED,
                    owner_id=owner_id,
                    fence=fence,
                    attempt=attempt,
                    updated_at=active,
                )

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

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

        def operation() -> bool:
            connection = self._connect()
            with _immediate(connection):
                return bool(
                    connection.execute(
                        "UPDATE acp_prompt_queue SET state = ?, updated_at = ? "
                        "WHERE prompt_id = ? AND state = ? AND owner_id = ? AND fence = ?",
                        (
                            state.value,
                            now_ms(),
                            prompt_id,
                            PromptState.CLAIMED.value,
                            owner_id,
                            fence,
                        ),
                    ).rowcount
                )

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

    async def get_prompt(self, prompt_id: str) -> QueuedPrompt | None:
        async with self._lock:
            row = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute("SELECT * FROM acp_prompt_queue WHERE prompt_id = ?", (prompt_id,))
                    .fetchone()
                ),
            )
        return _row_to_prompt(row) if row is not None else None

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
        clauses = ["deployment_id = ?", "session_id = ?"]
        values: list[Any] = [deployment_id, session_id]
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            values.extend(state.value for state in states)
        query = (
            f"SELECT * FROM acp_prompt_queue WHERE {' AND '.join(clauses)} "
            "ORDER BY sequence LIMIT ?"
        )
        async with self._lock:
            rows = await asyncio.to_thread(
                self._run,
                lambda: self._connect().execute(query, [*values, limit]).fetchall(),
            )
        return [_row_to_prompt(row) for row in rows]

    async def mark_uncertain_prompts(
        self,
        owner_id: str,
        *,
        deployment_id: str | None = None,
        session_id: str | None = None,
    ) -> _PromptList:
        clauses = ["owner_id = ?", "state = ?"]
        values: list[Any] = [owner_id, PromptState.CLAIMED.value]
        if deployment_id is not None:
            clauses.append("deployment_id = ?")
            values.append(deployment_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)
        where = " AND ".join(clauses)

        def operation() -> list[QueuedPrompt]:
            connection = self._connect()
            with _immediate(connection):
                rows = connection.execute(
                    f"SELECT * FROM acp_prompt_queue WHERE {where} ORDER BY sequence", values
                ).fetchall()
                if not rows:
                    return []
                active = now_ms()
                connection.execute(
                    f"UPDATE acp_prompt_queue SET state = ?, updated_at = ? WHERE {where}",
                    [PromptState.UNCERTAIN.value, active, *values],
                )
                return [
                    replace(
                        _row_to_prompt(row),
                        state=PromptState.UNCERTAIN,
                        updated_at=active,
                    )
                    for row in rows
                ]

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

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

        def operation() -> QueuedPrompt | None:
            connection = self._connect()
            with _immediate(connection):
                row = connection.execute(
                    "SELECT * FROM acp_prompt_queue WHERE prompt_id = ? AND session_id = ?",
                    (prompt_id, session_id),
                ).fetchone()
                if row is None:
                    return None
                original = _row_to_prompt(row)
                if original.state is not PromptState.UNCERTAIN:
                    return None
                active = now_ms()
                stored = replace(
                    original,
                    prompt_id=new_prompt_id,
                    sequence=_next_sequence(connection, original.session_id),
                    state=PromptState.QUEUED,
                    attempt=0,
                    parent_id=prompt_id,
                    owner_id=None,
                    fence=None,
                    created_at=active,
                    updated_at=active,
                )
                _insert_prompt(connection, stored)
                return stored

        async with self._lock:
            return await asyncio.to_thread(self._run, operation)

    # -- Deployment revision metadata ----------------------------------------

    async def upsert_deployment(self, record: DeploymentRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._run,
                lambda: self._connect().execute(
                    _DEPLOYMENT_UPSERT,
                    (
                        record.agent_id,
                        record.version,
                        record.name,
                        record.description,
                        int(record.visible),
                        int(record.retained),
                        record.registered_at,
                    ),
                ),
            )

    async def list_deployments(self) -> _DeploymentList:
        query = (
            f"SELECT {_DEPLOYMENT_COLUMNS} FROM acp_deployments "
            "ORDER BY agent_id ASC, registered_at ASC, version ASC"
        )
        async with self._lock:
            rows = await asyncio.to_thread(
                self._run,
                lambda: self._connect().execute(query).fetchall(),
            )
        return [_row_to_deployment(row) for row in rows]

    async def set_deployment_visible(self, agent_id: str, version: str, visible: bool) -> bool:
        async with self._lock:
            changed = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "UPDATE acp_deployments SET visible = ? WHERE agent_id = ? AND version = ?",
                        (int(visible), agent_id, version),
                    )
                    .rowcount
                ),
            )
        return bool(changed)

    async def set_deployment_retained(self, agent_id: str, version: str, retained: bool) -> bool:
        async with self._lock:
            changed = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "UPDATE acp_deployments SET retained = ? "
                        "WHERE agent_id = ? AND version = ?",
                        (int(retained), agent_id, version),
                    )
                    .rowcount
                ),
            )
        return bool(changed)

    async def delete_deployment(self, agent_id: str, version: str) -> bool:
        async with self._lock:
            changed = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "DELETE FROM acp_deployments "
                        "WHERE agent_id = ? AND version = ? AND retained = 0",
                        (agent_id, version),
                    )
                    .rowcount
                ),
            )
        return bool(changed)
