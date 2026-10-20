"""Optional PostgreSQL-backed durable session storage.

This is a server-side alternative to :class:`harness.runtime.store.SessionStore`
(SQLite WAL) behind the same asynchronous interface.  It persists exactly the same
records: session ownership/pins, selected configuration revisions and retry records,
turn outcomes, native model message history, and the renderable transcript.

Design notes
------------
* ``psycopg`` 3 is an optional dependency (``harness[postgres]``).  Importing
  this module never imports SQLite or requires PostgreSQL; the driver is checked when a
  store instance is constructed.
* One async connection per store instance is reused and guarded by an in-process lock.
  Cross-process (multi-worker) correctness is provided by short PostgreSQL
  transactions and row locks, not by the in-process lock alone.
* Every write that allocates a revision, turn index, message sequence, or transcript
  sequence first takes a ``FOR UPDATE`` lock on the owning ``harness_sessions`` row.
  This makes those allocations monotonic per session even when several worker
  processes share one database.
* Configuration transaction claims use the ``(session_id, request_id)`` primary key
  with ``INSERT ... ON CONFLICT DO NOTHING``.  A racing retry observes the existing
  claim and gets either the committed snapshot (identical payload) or a conflict;
  it never allocates a second revision.
* All table names are ``harness_``-prefixed so a shared database cannot silently
  collide with an unrelated application's ``sessions`` table.  A dedicated database
  (or at least a dedicated schema/search_path) is still recommended.

Nothing live is ever persisted: no agents, capability objects, clients, or callables.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

from harness.protocol import HarnessError

from .policy import payload_hash_matches
from .store import (
    SessionRecord,
    SessionStore,
    SessionSummary,
    StoredRevision,
    StoredTurn,
    TransactionClaim,
    now_ms,
)

try:  # pragma: no cover - exercised explicitly by the optional-import test
    import psycopg
    from psycopg import errors as _pg_errors
    from psycopg.rows import dict_row as _dict_row
except ImportError as _import_error:  # pragma: no cover - depends on installation
    psycopg = None  # type: ignore[assignment]
    _pg_errors = None  # type: ignore[assignment]
    _dict_row = None  # type: ignore[assignment]
    _PSYCOPG_IMPORT_ERROR: ImportError | None = _import_error
else:
    _PSYCOPG_IMPORT_ERROR = None

__all__ = [
    "PSYCOPG_AVAILABLE",
    "PostgresSessionStore",
    "SCHEMA_VERSION",
    "is_postgres_dsn",
]

PSYCOPG_AVAILABLE = psycopg is not None

_MISSING_DRIVER = (
    "PostgreSQL session storage requires the optional psycopg 3 driver. "
    "Install it with `pip install 'harness[postgres]'` "
    "(or `uv sync --extra postgres`)."
)

_T = TypeVar("_T")

# Serialize schema creation across processes.  The value is a stable, arbitrary
# signed 64-bit key ("HARNPG" in ASCII); PostgreSQL advisory locks are per database.
_MIGRATION_LOCK_KEY = 0x4841524E50470001

_MAX_TRANSACTION_ATTEMPTS = 3

def _process_host_identity() -> str:
    """Identity for PID-liveness comparisons (host + boot + PID namespace when known).

    Two workers on the same machine outside a container share the identity.  Containers
    with separate PID namespaces (even when configured with the same hostname) do not,
    so their PIDs are never judged by the wrong namespace.
    """

    parts = [socket.gethostname() or "unknown-host"]
    for source in ("/proc/sys/kernel/random/boot_id", "/proc/self/ns/pid"):
        try:
            if source.endswith("/ns/pid"):
                value = os.readlink(source)
            else:
                with open(source, encoding="ascii") as handle:
                    value = handle.read().strip()
        except OSError:
            continue
        if value:
            parts.append(value)
    return "|".join(parts)


#: Host identity stored with ownership claims.  PID liveness is only meaningful in the
#: same host/PID namespace; a remote worker is never judged dead from a local PID.
_PROCESS_HOST = _process_host_identity()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):  # other user's process: assume alive, fail safe
        return True
    return True


_SCHEMA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS harness_store_schema (
    version INTEGER PRIMARY KEY,
    applied_at BIGINT NOT NULL
)
"""

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS harness_sessions (
                session_id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                deployment_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                definition_version TEXT NOT NULL,
                title TEXT,
                cwd TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at BIGINT NOT NULL,
                last_active_at BIGINT NOT NULL,
                closed_at BIGINT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_session_control (
                session_id TEXT PRIMARY KEY
                    REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
                principal TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                acquired_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_configuration_revisions (
                session_id TEXT NOT NULL
                    REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                bindings TEXT NOT NULL,
                policy_revision INTEGER NOT NULL,
                source TEXT NOT NULL,
                request_id TEXT,
                applied BOOLEAN NOT NULL DEFAULT FALSE,
                created_at BIGINT NOT NULL,
                PRIMARY KEY (session_id, revision)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_configuration_transactions (
                session_id TEXT NOT NULL
                    REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
                request_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                result_snapshot TEXT,
                revision INTEGER,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL,
                PRIMARY KEY (session_id, request_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_turns (
                session_id TEXT NOT NULL
                    REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                active_revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                prompt TEXT NOT NULL,
                output TEXT,
                error TEXT,
                usage TEXT,
                started_at BIGINT NOT NULL,
                finished_at BIGINT,
                PRIMARY KEY (session_id, turn_id),
                UNIQUE (session_id, turn_index)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_messages (
                session_id TEXT NOT NULL
                    REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                turn_id TEXT,
                message TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                PRIMARY KEY (session_id, seq)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS harness_transcript (
                session_id TEXT NOT NULL
                    REFERENCES harness_sessions(session_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                turn_id TEXT,
                kind TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                PRIMARY KEY (session_id, seq)
            )
            """,
            "CREATE INDEX IF NOT EXISTS harness_sessions_principal_idx "
            "ON harness_sessions(principal, created_at)",
            "CREATE INDEX IF NOT EXISTS harness_turns_session_idx "
            "ON harness_turns(session_id, turn_index)",
            "CREATE INDEX IF NOT EXISTS harness_messages_session_idx "
            "ON harness_messages(session_id, seq)",
            "CREATE INDEX IF NOT EXISTS harness_transcript_session_idx "
            "ON harness_transcript(session_id, seq)",
        ),
    ),
    (
        2,
        (
            # Added when ownership learned to distinguish dead local owners from live
            # remote workers.  The columns are nullable for rows created by schema v1.
            "ALTER TABLE harness_session_control "
            "ADD COLUMN IF NOT EXISTS owner_pid INTEGER",
            "ALTER TABLE harness_session_control "
            "ADD COLUMN IF NOT EXISTS owner_host TEXT",
        ),
    ),
    (
        3,
        (
            # Durable workspace identities.  No foreign key, matching the SQLite store:
            # delete_session removes this row explicitly so a record can be enumerated
            # for TTL cleanup even if its session row is already gone.
            """
            CREATE TABLE IF NOT EXISTS harness_workspace_records (
                session_id TEXT PRIMARY KEY,
                record TEXT NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """,
        ),
    ),
    (
        4,
        (
            # Distinguishes a host-managed workspace binding from a plain cwd.
            "ALTER TABLE harness_sessions "
            "ADD COLUMN IF NOT EXISTS workspace_managed BOOLEAN NOT NULL DEFAULT FALSE",
        ),
    ),
    (
        5,
        (
            # Explicitly persisted capability state. A toggle never deletes it;
            # session deletion removes it explicitly alongside the conversation.
            """
            CREATE TABLE IF NOT EXISTS harness_plugin_state (
                session_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                state TEXT NOT NULL,
                updated_at BIGINT NOT NULL,
                PRIMARY KEY (session_id, binding_id)
            )
            """,
        ),
    ),
)

SCHEMA_VERSION = _MIGRATIONS[-1][0]


def is_postgres_dsn(value: str | None) -> bool:
    """Return True when *value* looks like a PostgreSQL URI DSN.

    This is a convenience for runtime selectors that must distinguish
    ``HARNESS_STORE=postgresql://...`` from a SQLite filesystem path.  libpq keyword
    DSNs (``host=... dbname=...``) are also accepted by the store constructor even
    though this helper intentionally only recognises URI prefixes.
    """

    if not value:
        return False
    return value.strip().lower().startswith(("postgres://", "postgresql://"))


def _compact_json(value: Any) -> str:
    """Canonical JSON used for bindings and committed snapshots (as in the SQLite store)."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sorted_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _optional_sorted_json(value: Any) -> str | None:
    return None if value is None else _sorted_json(value)


def _optional_json(text: str | None) -> Any:
    return json.loads(text) if text else None


def _summary_from_row(row: Any) -> SessionSummary:
    return SessionSummary(
        session_id=row["session_id"],
        principal=row["principal"],
        deployment_id=row["deployment_id"],
        agent_id=row["agent_id"],
        definition_version=row["definition_version"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        last_active_at=row["last_active_at"],
        cwd=row["cwd"],
        workspace_managed=bool(row["workspace_managed"]),
    )


def _revision_from_row(row: Any) -> StoredRevision:
    return StoredRevision(
        session_id=row["session_id"],
        revision=row["revision"],
        bindings=json.loads(row["bindings"]),
        policy_revision=row["policy_revision"],
        source=row["source"],
        applied=bool(row["applied"]),
    )


def _turn_from_row(row: Any) -> StoredTurn:
    return StoredTurn(
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        turn_index=row["turn_index"],
        active_revision=row["active_revision"],
        status=row["status"],
        prompt=json.loads(row["prompt"]) if row["prompt"] else None,
        output=json.loads(row["output"]) if row["output"] else None,
        error=json.loads(row["error"]) if row["error"] else None,
        usage=json.loads(row["usage"]) if row["usage"] else None,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class PostgresSessionStore(SessionStore):
    """PostgreSQL implementation of the durable :class:`SessionStore` contract.

    Parameters
    ----------
    dsn:
        A PostgreSQL URI (``postgresql://user:pass@host/db``) or a libpq keyword
        DSN.  When omitted, ``HARNESS_STORE`` is consulted.  Connections are lazy:
        construction never contacts the server.
    connect_timeout:
        libpq connection timeout in seconds (default 10).  Lock waits use
        PostgreSQL's normal behaviour; configure ``lock_timeout``/``statement_timeout``
        through the DSN or ``**connect_kwargs`` if a bound is required.
    application_name:
        Default ``application_name`` reported by ``pg_stat_activity``.
    **connect_kwargs:
        Additional libpq connection parameters (for example ``sslmode``).

    The object subclasses :class:`SessionStore` deliberately: the session manager
    distinguishes stores from filesystem paths with ``isinstance``, and passing an
    externally owned store must not cause the manager to create a SQLite file.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connect_timeout: float | None = 10.0,
        application_name: str = "harness-runtime",
        **connect_kwargs: Any,
    ) -> None:
        if psycopg is None or _pg_errors is None or _dict_row is None:
            raise ImportError(_MISSING_DRIVER) from _PSYCOPG_IMPORT_ERROR
        resolved = dsn if dsn is not None else os.environ.get("HARNESS_STORE")
        if resolved is None or not resolved.strip():
            raise ValueError(
                "PostgresSessionStore requires a PostgreSQL DSN (pass dsn=... or set "
                "HARNESS_STORE to a postgresql:// URI)."
            )
        resolved = resolved.strip()
        if not is_postgres_dsn(resolved) and "=" not in resolved:
            raise ValueError(
                "PostgresSessionStore expects a PostgreSQL URI (postgresql://...) or a "
                "libpq keyword DSN (host=... dbname=...), not a filesystem path."
            )
        self._dsn = resolved
        self._connect_timeout = connect_timeout
        self._application_name = application_name
        self._connect_kwargs = dict(connect_kwargs)
        self._connection: Any = None
        self._lock = asyncio.Lock()
        self._initialized = False
        self.control_stale_after: float | None = None
        """Optional bounded reclaim age for ownership rows without PID information."""

    # --- plumbing -----------------------------------------------------

    def _connect(self) -> Any:
        """The SQLite plumbing is intentionally unavailable on this store."""

        raise RuntimeError(
            "PostgresSessionStore does not use the SQLite connection layer; "
            "call the public async store methods instead."
        )

    async def _ensure_connected_locked(self) -> Any:
        connection = self._connection
        if connection is not None and not connection.closed and not connection.broken:
            return connection
        if connection is not None:
            try:
                await connection.close()
            except Exception:  # pragma: no cover - best effort on a broken connection
                pass
        self._connection = None
        self._initialized = False
        kwargs = dict(self._connect_kwargs)
        # The store depends on mapping rows and explicit transaction control.
        kwargs.pop("row_factory", None)
        kwargs.pop("autocommit", None)
        kwargs.setdefault("application_name", self._application_name)
        if self._connect_timeout is not None:
            kwargs.setdefault("connect_timeout", max(1, int(round(self._connect_timeout))))
        connection = await psycopg.AsyncConnection.connect(
            self._dsn,
            autocommit=False,
            row_factory=_dict_row,
            **kwargs,
        )
        self._connection = connection
        return connection

    async def initialize(self) -> None:
        """Connect and apply pending schema migrations idempotently."""

        async with self._lock:
            connection = await self._ensure_connected_locked()
            if self._initialized:
                return
            async with connection.transaction():
                await self._migrate(connection)
            self._initialized = True

    async def _migrate(self, connection: Any) -> None:
        """Apply schema migrations while holding a cross-process advisory lock."""

        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        await connection.execute(_SCHEMA_TABLE_SQL)
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM harness_store_schema"
        )
        row = await cursor.fetchone()
        current = int(row["version"]) if row is not None else 0
        if current > SCHEMA_VERSION:
            raise HarnessError(
                "DEPLOYMENT_UNAVAILABLE",
                "The PostgreSQL session database schema is newer than this runtime supports.",
                details={"database_version": current, "supported_version": SCHEMA_VERSION},
            )
        for version, statements in _MIGRATIONS:
            if version <= current:
                continue
            for statement in statements:
                await connection.execute(statement)
            await connection.execute(
                "INSERT INTO harness_store_schema (version, applied_at) VALUES (%s, %s)",
                (version, now_ms()),
            )

    async def _run(self, operation: Callable[[Any], Awaitable[_T]]) -> _T:
        """Run *operation* in a transaction, serializing this process's connection use.

        Deadlocks and serialization failures are safe to retry because the whole
        operation is rolled back before the attempt is repeated.  Unique violations are
        deliberately not retried here; callers translate the ones with defined
        semantics (session creation and transaction claims handle their own races).
        """

        async with self._lock:
            connection = await self._ensure_connected_locked()
            if not self._initialized:
                async with connection.transaction():
                    await self._migrate(connection)
                self._initialized = True
            for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
                try:
                    async with connection.transaction():
                        return await operation(connection)
                except (_pg_errors.DeadlockDetected, _pg_errors.SerializationFailure):
                    if attempt + 1 >= _MAX_TRANSACTION_ATTEMPTS:
                        raise
                    await asyncio.sleep(0.025 * (2**attempt))
            raise AssertionError("unreachable transaction retry state")  # pragma: no cover

    async def close(self) -> None:
        """Close this store's connection; a later operation reconnects automatically."""

        async with self._lock:
            connection = self._connection
            self._connection = None
            self._initialized = False
            if connection is not None:
                try:
                    await connection.close()
                except Exception:  # pragma: no cover - close is best effort
                    pass

    # --- sessions -----------------------------------------------------

    async def create_session(
        self,
        record: SessionRecord,
        *,
        initial_bindings: dict[str, dict[str, Any]],
        policy_revision: int,
    ) -> StoredRevision:
        async def operation(connection: Any) -> StoredRevision:
            timestamp = now_ms()
            await connection.execute(
                """
                INSERT INTO harness_sessions (
                    session_id, principal, deployment_id, agent_id,
                    definition_version, title, cwd, workspace_managed, status,
                    created_at, last_active_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s)
                """,
                (
                    record.session_id,
                    record.principal,
                    record.deployment_id,
                    record.agent_id,
                    record.definition_version,
                    record.title,
                    record.cwd,
                    bool(record.workspace_managed),
                    timestamp,
                    timestamp,
                ),
            )
            await connection.execute(
                """
                INSERT INTO harness_configuration_revisions (
                    session_id, revision, bindings, policy_revision, source,
                    request_id, applied, created_at
                ) VALUES (%s, 1, %s, %s, 'defaults', NULL, FALSE, %s)
                """,
                (
                    record.session_id,
                    _compact_json(initial_bindings),
                    policy_revision,
                    timestamp,
                ),
            )
            return StoredRevision(
                session_id=record.session_id,
                revision=1,
                bindings=initial_bindings,
                policy_revision=policy_revision,
                source="defaults",
            )

        try:
            return await self._run(operation)
        except _pg_errors.UniqueViolation as exc:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Session id {record.session_id!r} already exists.",
                details={"session_id": record.session_id},
            ) from exc

    async def get_session(self, session_id: str) -> SessionSummary | None:
        async def operation(connection: Any) -> SessionSummary | None:
            cursor = await connection.execute(
                "SELECT * FROM harness_sessions "
                "WHERE session_id = %s AND status != 'deleted'",
                (session_id,),
            )
            row = await cursor.fetchone()
            return _summary_from_row(row) if row is not None else None

        return await self._run(operation)

    async def list_sessions(
        self,
        principal: str | None = None,
        *,
        deployment_id: str | None = None,
        limit: int | None = 100,
    ) -> list[SessionSummary]:
        async def operation(connection: Any) -> list[SessionSummary]:
            clauses = ["status != 'deleted'"]
            params: list[Any] = []
            if principal is not None:
                clauses.append("principal = %s")
                params.append(principal)
            if deployment_id is not None:
                clauses.append("deployment_id = %s")
                params.append(deployment_id)
            sql = (
                "SELECT * FROM harness_sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY last_active_at DESC"
            )
            if limit is not None and int(limit) >= 0:
                sql += " LIMIT %s"
                params.append(int(limit))
            cursor = await connection.execute(sql, tuple(params))
            rows = await cursor.fetchall()
            return [_summary_from_row(row) for row in rows]

        return await self._run(operation)

    # --- host-facing record aliases ----------------------------------

    async def load_record(self, session_id: str) -> SessionSummary | None:
        """Host adapter record: session summary plus the replayable transcript."""

        async def operation(connection: Any) -> SessionSummary | None:
            cursor = await connection.execute(
                "SELECT * FROM harness_sessions "
                "WHERE session_id = %s AND status != 'deleted'",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            summary = _summary_from_row(row)
            summary.transcript = await self._load_transcript_rows(
                connection, session_id, None, 10_000
            )
            return summary

        return await self._run(operation)

    async def list_records(
        self,
        *,
        principal: str | None = None,
        deployment_id: str | None = None,
    ) -> list[SessionSummary]:
        """Host adapter record listing (no transcript payloads)."""

        return await self.list_sessions(principal, deployment_id=deployment_id)

    async def delete_record(self, session_id: str) -> bool:
        """Host adapter deletion: remove conversation, owned state, and retry records."""

        record = await self.get_session(session_id)
        if record is None:
            return False
        await self.delete_session(session_id)
        return True

    async def touch_session(self, session_id: str) -> None:
        async def operation(connection: Any) -> None:
            await connection.execute(
                "UPDATE harness_sessions SET last_active_at = %s WHERE session_id = %s",
                (now_ms(), session_id),
            )

        await self._run(operation)

    async def close_session(self, session_id: str) -> None:
        async def operation(connection: Any) -> None:
            timestamp = now_ms()
            await connection.execute(
                "UPDATE harness_sessions SET status = 'closed', closed_at = %s, "
                "last_active_at = %s WHERE session_id = %s AND status = 'open'",
                (timestamp, timestamp, session_id),
            )
            await connection.execute(
                "DELETE FROM harness_session_control WHERE session_id = %s",
                (session_id,),
            )

        await self._run(operation)

    async def delete_session(self, session_id: str) -> None:
        async def operation(connection: Any) -> None:
            # Workspace identities deliberately have no foreign key (matching SQLite),
            # so remove them explicitly.  Other child tables use ON DELETE CASCADE.
            await connection.execute(
                "DELETE FROM harness_workspace_records WHERE session_id = %s",
                (session_id,),
            )
            await connection.execute(
                "DELETE FROM harness_plugin_state WHERE session_id = %s",
                (session_id,),
            )
            await connection.execute(
                "DELETE FROM harness_sessions WHERE session_id = %s",
                (session_id,),
            )

        await self._run(operation)

    # --- ownership ----------------------------------------------------

    async def acquire_control(
        self,
        session_id: str,
        principal: str,
        owner_token: str,
        *,
        stale_after: float | None = None,
    ) -> bool:
        """Claim exclusive control, reclaiming provably dead local owners.

        A live local claim is never stolen, and a claim from another host is never
        judged dead by a local PID check.  ``stale_after`` (or the store's configured
        ``control_stale_after``) can reclaim a claim that has no PID information (for
        example one written by schema v1) after that many seconds; claims with a PID use
        process liveness instead.
        """

        effective_stale_after = (
            stale_after if stale_after is not None else self.control_stale_after
        )

        async def operation(connection: Any) -> bool:
            if not await self._lock_session(connection, session_id):
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Session {session_id!r} does not exist.",
                    details={"session_id": session_id},
                )
            timestamp = now_ms()
            cursor = await connection.execute(
                "SELECT owner_token, owner_pid, owner_host, acquired_at "
                "FROM harness_session_control WHERE session_id = %s FOR UPDATE",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is not None:
                if row["owner_token"] == owner_token:
                    return True
                owner_pid = row["owner_pid"]
                owner_host = row["owner_host"]
                locally_dead = (
                    isinstance(owner_pid, int)
                    and owner_pid > 0
                    and owner_host == _PROCESS_HOST
                    and not _pid_alive(owner_pid)
                )
                stale_without_pid = (
                    effective_stale_after is not None
                    and owner_pid is None
                    and timestamp - int(row["acquired_at"]) > effective_stale_after * 1000
                )
                if locally_dead or stale_without_pid:
                    await connection.execute(
                        "DELETE FROM harness_session_control WHERE session_id = %s",
                        (session_id,),
                    )
                else:
                    return False

            cursor = await connection.execute(
                """
                INSERT INTO harness_session_control (
                    session_id, principal, owner_token, owner_pid, owner_host, acquired_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO NOTHING
                RETURNING owner_token
                """,
                (
                    session_id,
                    principal,
                    owner_token,
                    os.getpid(),
                    _PROCESS_HOST,
                    timestamp,
                ),
            )
            inserted = await cursor.fetchone()
            if inserted is not None:
                return True
            # A writer outside this store could have raced the insert.  Re-read rather
            # than claiming a token that is not actually ours.
            cursor = await connection.execute(
                "SELECT owner_token FROM harness_session_control WHERE session_id = %s",
                (session_id,),
            )
            current = await cursor.fetchone()
            return current is not None and current["owner_token"] == owner_token

        return await self._run(operation)

    async def release_control(self, session_id: str, owner_token: str) -> None:
        async def operation(connection: Any) -> None:
            if not await self._lock_session(connection, session_id):
                return
            await connection.execute(
                "DELETE FROM harness_session_control "
                "WHERE session_id = %s AND owner_token = %s",
                (session_id, owner_token),
            )

        await self._run(operation)

    # --- configuration revisions -------------------------------------

    async def append_revision(
        self,
        session_id: str,
        *,
        bindings: dict[str, dict[str, Any]],
        policy_revision: int,
        source: str,
        request_id: str | None = None,
    ) -> StoredRevision:
        async def operation(connection: Any) -> StoredRevision:
            await self._require_session(connection, session_id)
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision "
                "FROM harness_configuration_revisions WHERE session_id = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
            revision = int(row["revision"] if row is not None else 0) + 1
            timestamp = now_ms()
            await connection.execute(
                """
                INSERT INTO harness_configuration_revisions (
                    session_id, revision, bindings, policy_revision, source,
                    request_id, applied, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
                """,
                (
                    session_id,
                    revision,
                    _compact_json(bindings),
                    policy_revision,
                    source,
                    request_id,
                    timestamp,
                ),
            )
            await connection.execute(
                "UPDATE harness_sessions SET last_active_at = %s WHERE session_id = %s",
                (timestamp, session_id),
            )
            return StoredRevision(
                session_id=session_id,
                revision=revision,
                bindings=bindings,
                policy_revision=policy_revision,
                source=source,
            )

        return await self._run(operation)

    async def latest_revision(self, session_id: str) -> StoredRevision | None:
        async def operation(connection: Any) -> StoredRevision | None:
            cursor = await connection.execute(
                "SELECT * FROM harness_configuration_revisions "
                "WHERE session_id = %s ORDER BY revision DESC LIMIT 1",
                (session_id,),
            )
            row = await cursor.fetchone()
            return _revision_from_row(row) if row is not None else None

        return await self._run(operation)

    async def get_revision(self, session_id: str, revision: int) -> StoredRevision | None:
        async def operation(connection: Any) -> StoredRevision | None:
            return await self._select_revision(connection, session_id, revision)

        return await self._run(operation)

    async def mark_revision_applied(
        self,
        session_id: str,
        revision: int,
        *,
        applied: bool = True,
    ) -> None:
        async def operation(connection: Any) -> None:
            if not await self._lock_session(connection, session_id):
                return
            await connection.execute(
                "UPDATE harness_configuration_revisions SET applied = %s "
                "WHERE session_id = %s AND revision <= %s",
                (bool(applied), session_id, revision),
            )

        await self._run(operation)

    # --- configuration transactions / retries ------------------------

    async def claim_transaction(
        self,
        session_id: str,
        request_id: str,
        payload_hash: str,
    ) -> TransactionClaim:
        async def operation(connection: Any) -> TransactionClaim:
            await self._require_session(connection, session_id)
            timestamp = now_ms()
            cursor = await connection.execute(
                """
                INSERT INTO harness_configuration_transactions (
                    session_id, request_id, payload_hash, status, created_at, updated_at
                ) VALUES (%s, %s, %s, 'in_progress', %s, %s)
                ON CONFLICT (session_id, request_id) DO NOTHING
                RETURNING request_id
                """,
                (session_id, request_id, payload_hash, timestamp, timestamp),
            )
            if await cursor.fetchone() is not None:
                return TransactionClaim(status="claimed")

            cursor = await connection.execute(
                "SELECT * FROM harness_configuration_transactions "
                "WHERE session_id = %s AND request_id = %s FOR UPDATE",
                (session_id, request_id),
            )
            row = await cursor.fetchone()
            if row is None:  # pragma: no cover - protected by the unique key
                raise HarnessError(
                    "REVISION_CONFLICT",
                    "The configuration request disappeared while claiming it; retry shortly.",
                    details={"request_id": request_id, "session_id": session_id},
                )
            if not payload_hash_matches(row["payload_hash"], payload_hash):
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Request id {request_id!r} was already used with a different payload.",
                    details={"request_id": request_id, "session_id": session_id},
                )
            if row["status"] == "committed":
                snapshot = _optional_json(row["result_snapshot"])
                return TransactionClaim(
                    status="committed", snapshot=snapshot, revision=row["revision"]
                )
            if row["status"] == "in_progress":
                raise HarnessError(
                    "REVISION_CONFLICT",
                    "The same configuration request is currently in progress; retry shortly.",
                    details={"request_id": request_id, "session_id": session_id},
                )
            # A failed attempt may be retried with the same payload.
            await connection.execute(
                "UPDATE harness_configuration_transactions "
                "SET status = 'in_progress', updated_at = %s "
                "WHERE session_id = %s AND request_id = %s",
                (timestamp, session_id, request_id),
            )
            return TransactionClaim(status="claimed")

        return await self._run(operation)

    async def commit_transaction(
        self,
        session_id: str,
        request_id: str,
        *,
        bindings: dict[str, dict[str, Any]] | None,
        policy_revision: int,
        snapshot_builder: Any = None,
        create_revision: bool = True,
    ) -> StoredRevision:
        async def operation(connection: Any) -> StoredRevision:
            await self._require_session(connection, session_id)
            timestamp = now_ms()
            cursor = await connection.execute(
                "SELECT * FROM harness_configuration_transactions "
                "WHERE session_id = %s AND request_id = %s FOR UPDATE",
                (session_id, request_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    "Configuration transaction was not claimed before commit.",
                    details={"session_id": session_id, "request_id": request_id},
                )
            if row["status"] == "committed" and row["revision"] is not None:
                stored = await self._select_revision(
                    connection, session_id, int(row["revision"])
                )
                if stored is not None:
                    return stored

            cursor = await connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision "
                "FROM harness_configuration_revisions WHERE session_id = %s",
                (session_id,),
            )
            latest = await cursor.fetchone()
            current_revision = int(latest["revision"] if latest is not None else 0)
            if create_revision:
                revision = current_revision + 1
                await connection.execute(
                    """
                    INSERT INTO harness_configuration_revisions (
                        session_id, revision, bindings, policy_revision, source,
                        request_id, applied, created_at
                    ) VALUES (%s, %s, %s, %s, 'override', %s, FALSE, %s)
                    """,
                    (
                        session_id,
                        revision,
                        _compact_json(bindings or {}),
                        policy_revision,
                        request_id,
                        timestamp,
                    ),
                )
            else:
                revision = current_revision

            snapshot = snapshot_builder(revision) if callable(snapshot_builder) else {}
            await connection.execute(
                "UPDATE harness_configuration_transactions "
                "SET status = 'committed', result_snapshot = %s, revision = %s, "
                "updated_at = %s WHERE session_id = %s AND request_id = %s",
                (
                    _compact_json(snapshot),
                    revision,
                    timestamp,
                    session_id,
                    request_id,
                ),
            )
            await connection.execute(
                "UPDATE harness_sessions SET last_active_at = %s WHERE session_id = %s",
                (timestamp, session_id),
            )
            stored = await self._select_revision(connection, session_id, revision)
            if stored is not None:
                return stored
            # Defensive only: every committed session has at least revision 1.
            return StoredRevision(  # pragma: no cover - schema invariant
                session_id=session_id,
                revision=revision,
                bindings=dict(bindings or {}),
                policy_revision=policy_revision,
                source="override",
            )

        return await self._run(operation)

    async def fail_transaction(self, session_id: str, request_id: str) -> None:
        async def operation(connection: Any) -> None:
            if not await self._lock_session(connection, session_id):
                return
            # Never downgrade an already committed request: a client whose commit
            # response was lost must still get the original snapshot on retry.
            await connection.execute(
                "UPDATE harness_configuration_transactions "
                "SET status = 'failed', updated_at = %s "
                "WHERE session_id = %s AND request_id = %s AND status != 'committed'",
                (now_ms(), session_id, request_id),
            )

        await self._run(operation)

    async def fail_in_progress_transactions(self, session_id: str) -> int:
        """Mark abandoned in-progress claims failed after a worker crash."""

        async def operation(connection: Any) -> int:
            if not await self._lock_session(connection, session_id):
                return 0
            cursor = await connection.execute(
                "UPDATE harness_configuration_transactions "
                "SET status = 'failed', updated_at = %s "
                "WHERE session_id = %s AND status = 'in_progress'",
                (now_ms(), session_id),
            )
            return int(cursor.rowcount or 0)

        return await self._run(operation)

    # --- turns --------------------------------------------------------

    async def create_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        active_revision: int,
        prompt: Any,
    ) -> StoredTurn:
        async def operation(connection: Any) -> StoredTurn:
            await self._require_session(connection, session_id)
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(turn_index), 0) AS turn_index "
                "FROM harness_turns WHERE session_id = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
            turn_index = int(row["turn_index"] if row is not None else 0) + 1
            timestamp = now_ms()
            await connection.execute(
                """
                INSERT INTO harness_turns (
                    session_id, turn_id, turn_index, active_revision, status,
                    prompt, started_at
                ) VALUES (%s, %s, %s, %s, 'running', %s, %s)
                """,
                (
                    session_id,
                    turn_id,
                    turn_index,
                    active_revision,
                    _sorted_json(prompt),
                    timestamp,
                ),
            )
            await connection.execute(
                "UPDATE harness_sessions SET last_active_at = %s WHERE session_id = %s",
                (timestamp, session_id),
            )
            return StoredTurn(
                session_id=session_id,
                turn_id=turn_id,
                turn_index=turn_index,
                active_revision=active_revision,
                status="running",
                prompt=prompt,
                started_at=timestamp,
            )

        return await self._run(operation)

    async def finalize_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        status: str,
        output: Any = None,
        error: Any = None,
        usage: Any = None,
        messages: list[Any] | None = None,
    ) -> None:
        async def operation(connection: Any) -> None:
            await self._require_session(connection, session_id)
            timestamp = now_ms()
            await connection.execute(
                "UPDATE harness_turns SET status = %s, output = %s, error = %s, "
                "usage = %s, finished_at = %s WHERE session_id = %s AND turn_id = %s",
                (
                    status,
                    _optional_sorted_json(output),
                    _optional_sorted_json(error),
                    _optional_sorted_json(usage),
                    timestamp,
                    session_id,
                    turn_id,
                ),
            )
            history = list(messages or [])
            if history:
                await self._insert_messages(
                    connection, session_id, turn_id, history, timestamp
                )
            await connection.execute(
                "UPDATE harness_sessions SET last_active_at = %s WHERE session_id = %s",
                (timestamp, session_id),
            )

        await self._run(operation)

    async def mark_turn_interrupted(self, session_id: str) -> list[str]:
        """Mark turns left ``running`` by a crash as interrupted; returns their ids."""

        async def operation(connection: Any) -> list[str]:
            if not await self._lock_session(connection, session_id):
                return []
            cursor = await connection.execute(
                "SELECT turn_id FROM harness_turns "
                "WHERE session_id = %s AND status = 'running' "
                "ORDER BY turn_index FOR UPDATE",
                (session_id,),
            )
            rows = await cursor.fetchall()
            turn_ids = [row["turn_id"] for row in rows]
            if turn_ids:
                await connection.execute(
                    "UPDATE harness_turns SET status = 'interrupted', finished_at = %s "
                    "WHERE session_id = %s AND status = 'running'",
                    (now_ms(), session_id),
                )
            return turn_ids

        return await self._run(operation)

    async def get_turn(self, session_id: str, turn_id: str) -> StoredTurn | None:
        async def operation(connection: Any) -> StoredTurn | None:
            cursor = await connection.execute(
                "SELECT * FROM harness_turns WHERE session_id = %s AND turn_id = %s",
                (session_id, turn_id),
            )
            row = await cursor.fetchone()
            return _turn_from_row(row) if row is not None else None

        return await self._run(operation)

    async def list_turns(self, session_id: str) -> list[StoredTurn]:
        async def operation(connection: Any) -> list[StoredTurn]:
            cursor = await connection.execute(
                "SELECT * FROM harness_turns WHERE session_id = %s ORDER BY turn_index",
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [_turn_from_row(row) for row in rows]

        return await self._run(operation)

    # --- history ------------------------------------------------------

    async def append_messages(
        self, session_id: str, turn_id: str | None, messages: list[Any]
    ) -> None:
        if not messages:
            await self.initialize()
            return

        async def operation(connection: Any) -> None:
            await self._require_session(connection, session_id)
            await self._insert_messages(
                connection, session_id, turn_id, list(messages), now_ms()
            )

        await self._run(operation)

    async def _insert_messages(
        self,
        connection: Any,
        session_id: str,
        turn_id: str | None,
        messages: list[Any],
        timestamp: int,
    ) -> None:
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM harness_messages WHERE session_id = %s",
            (session_id,),
        )
        row = await cursor.fetchone()
        seq = int(row["seq"] if row is not None else 0)
        for message in messages:
            seq += 1
            await connection.execute(
                "INSERT INTO harness_messages "
                "(session_id, seq, turn_id, message, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (session_id, seq, turn_id, json.dumps(message), timestamp),
            )

    async def load_messages_json(self, session_id: str) -> str:
        async def operation(connection: Any) -> str:
            cursor = await connection.execute(
                "SELECT message FROM harness_messages WHERE session_id = %s ORDER BY seq",
                (session_id,),
            )
            rows = await cursor.fetchall()
            return "[" + ",".join(row["message"] for row in rows) + "]"

        return await self._run(operation)

    async def message_count(self, session_id: str) -> int:
        async def operation(connection: Any) -> int:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM harness_messages WHERE session_id = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
            return int(row["count"]) if row is not None else 0

        return await self._run(operation)

    # --- workspace identities ----------------------------------------

    async def get_workspace_record(self, session_id: str) -> Any | None:
        """Load a session's durable workspace identity, if one is stored."""

        async def operation(connection: Any) -> Any | None:
            from .workspaces import WorkspaceRecord

            cursor = await connection.execute(
                "SELECT record FROM harness_workspace_records WHERE session_id = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            try:
                return WorkspaceRecord.from_dict(json.loads(row["record"]))
            except Exception as exc:
                raise HarnessError(
                    "WORKSPACE_RECORD_FAILED",
                    f"Stored workspace identity for session {session_id!r} is unreadable.",
                    {"session_id": session_id},
                ) from exc

        return await self._run(operation)

    async def save_workspace_record(self, record: Any) -> None:
        """Persist one workspace record (upsert)."""

        payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))

        async def operation(connection: Any) -> None:
            await connection.execute(
                """
                INSERT INTO harness_workspace_records (session_id, record, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE
                SET record = EXCLUDED.record, updated_at = EXCLUDED.updated_at
                """,
                (record.session_id, payload, now_ms()),
            )

        await self._run(operation)

    async def delete_workspace_record(self, session_id: str) -> None:
        """Remove a session's workspace identity after its environment was deleted."""

        async def operation(connection: Any) -> None:
            await connection.execute(
                "DELETE FROM harness_workspace_records WHERE session_id = %s",
                (session_id,),
            )

        await self._run(operation)

    async def list_workspace_records(self) -> list[Any]:
        """All retained workspace identities (for TTL cleanup)."""

        async def operation(connection: Any) -> list[Any]:
            from .workspaces import WorkspaceRecord

            cursor = await connection.execute(
                "SELECT record FROM harness_workspace_records ORDER BY session_id"
            )
            rows = await cursor.fetchall()
            records: list[WorkspaceRecord] = []
            for row in rows:
                try:
                    records.append(WorkspaceRecord.from_dict(json.loads(row["record"])))
                except Exception as exc:
                    raise HarnessError(
                        "WORKSPACE_RECORD_FAILED",
                        "A stored workspace identity is unreadable; cleanup made no changes.",
                        {},
                    ) from exc
            return records

        return await self._run(operation)

    def workspace_record_callbacks(self) -> Any:
        """Adapter for :class:`harness.runtime.workspaces.WorkspaceManager`."""

        from .workspaces import WorkspaceRecordCallbacks

        return WorkspaceRecordCallbacks(
            get=self.get_workspace_record,
            save=self.save_workspace_record,
            delete=self.delete_workspace_record,
            records=self.list_workspace_records,
        )

    # --- explicitly persisted capability state ------------------------

    async def get_plugin_state(
        self, session_id: str, binding_id: str, *, schema_version: int | None = None
    ) -> dict[str, Any] | None:
        async def operation(connection: Any) -> dict[str, Any] | None:
            cursor = await connection.execute(
                "SELECT schema_version, state FROM harness_plugin_state "
                "WHERE session_id = %s AND binding_id = %s",
                (session_id, binding_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            if schema_version is not None and int(row["schema_version"]) != schema_version:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Stored state for binding {binding_id!r} uses schema "
                    f"{row['schema_version']}, not the requested {schema_version}.",
                    {
                        "binding_id": binding_id,
                        "stored_schema_version": int(row["schema_version"]),
                        "schema_version": schema_version,
                    },
                )
            try:
                state = json.loads(row["state"])
            except ValueError as exc:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Stored state for binding {binding_id!r} is unreadable.",
                    {"binding_id": binding_id},
                ) from exc
            if not isinstance(state, dict):
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Stored state for binding {binding_id!r} is not an object.",
                    {"binding_id": binding_id},
                )
            return state

        return await self._run(operation)

    async def set_plugin_state(
        self, session_id: str, binding_id: str, *, schema_version: int, state: Mapping[str, Any]
    ) -> None:
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
            raise HarnessError("INVALID_CONFIGURATION", "schema_version must be a positive integer.")
        try:
            payload = json.dumps(
                dict(state), sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "Capability state must be a JSON-serializable object.",
                {"binding_id": binding_id},
            ) from exc

        async def operation(connection: Any) -> None:
            await self._require_session(connection, session_id)
            cursor = await connection.execute(
                "SELECT schema_version FROM harness_plugin_state "
                "WHERE session_id = %s AND binding_id = %s",
                (session_id, binding_id),
            )
            row = await cursor.fetchone()
            if row is not None and int(row["schema_version"]) != schema_version:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Binding {binding_id!r} state uses schema {row['schema_version']}; "
                    "a schema change requires a new binding identity or session.",
                    {
                        "binding_id": binding_id,
                        "stored_schema_version": int(row["schema_version"]),
                        "schema_version": schema_version,
                    },
                )
            await connection.execute(
                """
                INSERT INTO harness_plugin_state
                    (session_id, binding_id, schema_version, state, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (session_id, binding_id) DO UPDATE
                SET state = EXCLUDED.state, updated_at = EXCLUDED.updated_at
                """,
                (session_id, binding_id, schema_version, payload, now_ms()),
            )

        await self._run(operation)

    async def delete_plugin_state(self, session_id: str, binding_id: str) -> None:
        async def operation(connection: Any) -> None:
            await connection.execute(
                "DELETE FROM harness_plugin_state WHERE session_id = %s AND binding_id = %s",
                (session_id, binding_id),
            )

        await self._run(operation)

    # --- transcript ---------------------------------------------------

    async def append_transcript(
        self,
        session_id: str,
        turn_id: str | None,
        kind: str,
        data: dict[str, Any],
    ) -> int:
        async def operation(connection: Any) -> int:
            await self._require_session(connection, session_id)
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM harness_transcript "
                "WHERE session_id = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
            seq = int(row["seq"] if row is not None else 0) + 1
            await connection.execute(
                "INSERT INTO harness_transcript "
                "(session_id, seq, turn_id, kind, data, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (session_id, seq, turn_id, kind, json.dumps(data), now_ms()),
            )
            return seq

        return await self._run(operation)

    async def append_transcript_text(
        self,
        session_id: str,
        seq: int,
        appended: str,
        turn_id: str | None,
    ) -> None:
        async def operation(connection: Any) -> None:
            await self._require_session(connection, session_id)
            cursor = await connection.execute(
                "SELECT data FROM harness_transcript "
                "WHERE session_id = %s AND seq = %s FOR UPDATE",
                (session_id, seq),
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.execute(
                    "INSERT INTO harness_transcript "
                    "(session_id, seq, turn_id, kind, data, created_at) "
                    "VALUES (%s, %s, %s, 'text_delta', %s, %s)",
                    (
                        session_id,
                        seq,
                        turn_id,
                        json.dumps({"text": appended}),
                        now_ms(),
                    ),
                )
                return
            data = json.loads(row["data"])
            data["text"] = data.get("text", "") + appended
            await connection.execute(
                "UPDATE harness_transcript SET data = %s "
                "WHERE session_id = %s AND seq = %s",
                (json.dumps(data), session_id, seq),
            )

        await self._run(operation)

    async def load_transcript(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        async def operation(connection: Any) -> list[dict[str, Any]]:
            return await self._load_transcript_rows(connection, session_id, turn_id, limit)

        return await self._run(operation)

    # --- internal SQL helpers ----------------------------------------

    async def _load_transcript_rows(
        self,
        connection: Any,
        session_id: str,
        turn_id: str | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM harness_transcript WHERE session_id = %s"
        params: list[Any] = [session_id]
        if turn_id is not None:
            sql += " AND turn_id = %s"
            params.append(turn_id)
        sql += " ORDER BY seq"
        if limit is not None and int(limit) >= 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        cursor = await connection.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        return [
            {
                "seq": row["seq"],
                "turn_id": row["turn_id"],
                "kind": row["kind"],
                "data": json.loads(row["data"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def _lock_session(self, connection: Any, session_id: str) -> bool:
        """Lock the session row to serialize revision/turn/sequence allocation."""

        cursor = await connection.execute(
            "SELECT session_id FROM harness_sessions WHERE session_id = %s FOR UPDATE",
            (session_id,),
        )
        return await cursor.fetchone() is not None

    async def _require_session(self, connection: Any, session_id: str) -> None:
        if not await self._lock_session(connection, session_id):
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Session {session_id!r} does not exist.",
                details={"session_id": session_id},
            )

    async def _select_revision(
        self, connection: Any, session_id: str, revision: int
    ) -> StoredRevision | None:
        cursor = await connection.execute(
            "SELECT * FROM harness_configuration_revisions "
            "WHERE session_id = %s AND revision = %s",
            (session_id, revision),
        )
        row = await cursor.fetchone()
        return _revision_from_row(row) if row is not None else None
