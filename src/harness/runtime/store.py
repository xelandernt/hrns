"""Durable session storage (SQLite in WAL mode).

The runtime host owns canonical session state. This store persists session ownership and
pins, selected configuration revisions, committed transaction/retry records, turn outcomes,
native model message history, and a renderable transcript. Short transactions are shared by
connection workers; WAL mode keeps readers unblocked while a turn journals.

Nothing live is ever persisted: no agents, capability objects, clients, or callables. A
session is reconstructed from its pinned definition and these records.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from harness.protocol import HarnessError

from .policy import payload_hash_matches

__all__ = [
    "SessionStore",
    "SessionRecord",
    "SessionSummary",
    "StoredRevision",
    "StoredTurn",
    "TransactionClaim",
    "default_store_path",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    title TEXT,
    cwd TEXT,
    workspace_managed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL,
    closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS session_control (
    session_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    owner_token TEXT NOT NULL,
    owner_pid INTEGER,
    acquired_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS acp_metadata (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS configuration_revisions (
    session_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    bindings TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    source TEXT NOT NULL,
    request_id TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, revision)
);

CREATE TABLE IF NOT EXISTS configuration_transactions (
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result_snapshot TEXT,
    revision INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, request_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS turns (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    active_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    output TEXT,
    error TEXT,
    usage TEXT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    PRIMARY KEY (session_id, turn_id),
    UNIQUE (session_id, turn_index),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    turn_id TEXT,
    message TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workspace_records (
    session_id TEXT PRIMARY KEY,
    record TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plugin_state (
    session_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, binding_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transcript (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    turn_id TEXT,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_principal ON sessions(principal, created_at);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript(session_id, seq);
"""


def default_store_path() -> Path:
    """Host default store location: ``HARNESS_STORE`` when set, else the user data dir."""
    explicit = os.environ.get("HARNESS_STORE")
    if explicit:
        return Path(explicit).expanduser()
    import platformdirs

    return Path(platformdirs.user_data_dir("harness", "harness")) / "runtime.sqlite3"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class SessionRecord:
    session_id: str
    principal: str
    deployment_id: str
    agent_id: str
    definition_version: str
    title: str | None = None
    cwd: str | None = None
    workspace_managed: bool = False
    status: str = "open"


@dataclass
class SessionSummary:
    session_id: str
    principal: str
    deployment_id: str
    agent_id: str
    definition_version: str
    title: str | None
    status: str
    created_at: int
    last_active_at: int
    cwd: str | None = None
    workspace_managed: bool = False
    transcript: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StoredRevision:
    session_id: str
    revision: int
    bindings: dict[str, dict[str, Any]]
    policy_revision: int
    source: str
    applied: bool = False


@dataclass
class TransactionClaim:
    status: Literal["claimed", "committed"]
    snapshot: dict[str, Any] | None = None
    revision: int | None = None


@dataclass
class StoredTurn:
    session_id: str
    turn_id: str
    turn_index: int
    active_revision: int
    status: str
    prompt: Any
    output: Any = None
    error: Any = None
    usage: Any = None
    started_at: int = 0
    finished_at: int | None = None


class SessionStore:
    """SQLite-backed durable store with async methods.

    All database work runs in a worker thread; an instance-level async lock serializes this
    process's writes so claimed transactions and revision numbers stay atomic. Cross-process
    atomicity comes from SQLite transactions and WAL.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        busy_timeout: float = 10.0,
    ) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self._memory = str(self.path) == ":memory:"
        self._busy_timeout = busy_timeout
        self._connection: Any = None
        self._lock = asyncio.Lock()
        self._initialized = False
        self.control_stale_after: float | None = None
        """Optional bounded reclaim age for ownership rows without PID information."""

    # --- plumbing -----------------------------------------------------

    def _connect(self) -> Any:
        if self._connection is not None:
            return self._connection
        import sqlite3

        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.path),
            timeout=self._busy_timeout,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout * 1000)}")
        if not self._memory:
            # Two workers can open the same new database concurrently. Switching to WAL
            # briefly needs an exclusive lock; the connection busy handler is not reliably
            # invoked for this PRAGMA, so retry the bounded lock window explicitly.
            for attempt in range(6):
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 5:
                        raise
                    time.sleep(0.02 * (attempt + 1))
            connection.execute("PRAGMA synchronous = NORMAL")
        self._connection = connection
        return connection

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        connection = self._connect()
        connection.executescript(_SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        if "cwd" not in columns:  # migrate stores created before working-directory retention
            connection.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
        if "workspace_managed" not in columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN workspace_managed INTEGER NOT NULL DEFAULT 0"
            )
        control_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(session_control)")
        }
        if control_columns and "owner_pid" not in control_columns:
            connection.execute("ALTER TABLE session_control ADD COLUMN owner_pid INTEGER")
        connection.commit()

    async def _run(self, fn: Any, *args: Any) -> Any:
        await self.initialize()
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None
                self._initialized = False

    # --- sessions -----------------------------------------------------

    async def create_session(
        self,
        record: SessionRecord,
        *,
        initial_bindings: dict[str, dict[str, Any]],
        policy_revision: int,
    ) -> StoredRevision:
        return await self._run(self._create_session_sync, record, initial_bindings, policy_revision)

    def _create_session_sync(
        self,
        record: SessionRecord,
        initial_bindings: dict[str, dict[str, Any]],
        policy_revision: int,
    ) -> StoredRevision:
        connection = self._connect()
        timestamp = now_ms()
        with connection:
            existing = connection.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?", (record.session_id,)
            ).fetchone()
            if existing is not None:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Session id {record.session_id!r} already exists.",
                    details={"session_id": record.session_id},
                )
            connection.execute(
                "INSERT INTO sessions (session_id, principal, deployment_id, agent_id, "
                "definition_version, title, cwd, workspace_managed, status, created_at, "
                "last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (
                    record.session_id,
                    record.principal,
                    record.deployment_id,
                    record.agent_id,
                    record.definition_version,
                    record.title,
                    record.cwd,
                    1 if record.workspace_managed else 0,
                    timestamp,
                    timestamp,
                ),
            )
            payload = json.dumps(initial_bindings, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "INSERT INTO configuration_revisions (session_id, revision, bindings, policy_revision, "
                "source, request_id, applied, created_at) VALUES (?, 1, ?, ?, 'defaults', NULL, 0, ?)",
                (record.session_id, payload, policy_revision, timestamp),
            )
        return StoredRevision(
            session_id=record.session_id,
            revision=1,
            bindings=initial_bindings,
            policy_revision=policy_revision,
            source="defaults",
        )

    async def get_session(self, session_id: str) -> SessionSummary | None:
        return await self._run(self._get_session_sync, session_id)

    def _get_session_sync(self, session_id: str) -> SessionSummary | None:
        row = (
            self._connect()
            .execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status != 'deleted'",
                (session_id,),
            )
            .fetchone()
        )
        return _summary(row) if row is not None else None

    async def set_session_title(self, session_id: str, title: str) -> None:
        await self._run(self._set_session_title_sync, session_id, title)

    def _set_session_title_sync(self, session_id: str, title: str) -> None:
        connection = self._connect()
        with connection:
            connection.execute(
                "UPDATE sessions SET title = ?, last_active_at = ? WHERE session_id = ?",
                (title, now_ms(), session_id),
            )

    async def list_sessions(
        self,
        principal: str | None = None,
        *,
        deployment_id: str | None = None,
        limit: int | None = 100,
    ) -> list[SessionSummary]:
        return await self._run(self._list_sessions_sync, principal, deployment_id, limit)

    def _list_sessions_sync(
        self, principal: str | None, deployment_id: str | None, limit: int | None
    ) -> list[SessionSummary]:
        clauses = ["status != 'deleted'"]
        params: list[Any] = []
        if principal is not None:
            clauses.append("principal = ?")
            params.append(principal)
        if deployment_id is not None:
            clauses.append("deployment_id = ?")
            params.append(deployment_id)
        sql = (
            "SELECT * FROM sessions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY last_active_at DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._connect().execute(sql, params).fetchall()
        return [_summary(row) for row in rows]

    # --- host-facing record aliases ----------------------------------

    async def load_record(self, session_id: str) -> SessionSummary | None:
        """Host adapter record: session summary plus the replayable transcript."""
        return await self._run(self._load_record_sync, session_id)

    def _load_record_sync(self, session_id: str) -> SessionSummary | None:
        row = (
            self._connect()
            .execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status != 'deleted'",
                (session_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        summary = _summary(row)
        summary.transcript = self._load_transcript_sync(session_id, None, 10_000)
        return summary

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
        await self._run(self._touch_session_sync, session_id)

    def _touch_session_sync(self, session_id: str) -> None:
        connection = self._connect()
        with connection:
            connection.execute(
                "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                (now_ms(), session_id),
            )

    async def close_session(self, session_id: str) -> None:
        await self._run(self._close_session_sync, session_id)

    def _close_session_sync(self, session_id: str) -> None:
        connection = self._connect()
        with connection:
            connection.execute(
                "UPDATE sessions SET status = 'closed', closed_at = ?, last_active_at = ? "
                "WHERE session_id = ? AND status = 'open'",
                (now_ms(), now_ms(), session_id),
            )
            connection.execute("DELETE FROM session_control WHERE session_id = ?", (session_id,))

    async def delete_session(self, session_id: str) -> None:
        await self._run(self._delete_session_sync, session_id)

    def _delete_session_sync(self, session_id: str) -> None:
        connection = self._connect()
        with connection:
            connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM transcript WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
            connection.execute(
                "DELETE FROM configuration_transactions WHERE session_id = ?", (session_id,)
            )
            connection.execute(
                "DELETE FROM configuration_revisions WHERE session_id = ?", (session_id,)
            )
            connection.execute("DELETE FROM workspace_records WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM plugin_state WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM session_control WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    async def get_acp_metadata(self, session_id: str) -> dict[str, Any] | None:
        """Load the ACP working directory and selected model/effort."""

        def operation() -> dict[str, Any] | None:
            row = (
                self._connect()
                .execute("SELECT data FROM acp_metadata WHERE session_id = ?", (session_id,))
                .fetchone()
            )
            return json.loads(row["data"]) if row is not None else None

        return await self._run(operation)

    async def put_acp_metadata(self, session_id: str, data: dict[str, Any]) -> None:
        def operation() -> None:
            connection = self._connect()
            with connection:
                connection.execute(
                    "INSERT INTO acp_metadata (session_id, data) VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET data = excluded.data",
                    (session_id, json.dumps(data)),
                )

        await self._run(operation)

    async def delete_acp_metadata(self, session_id: str) -> None:
        def operation() -> None:
            connection = self._connect()
            with connection:
                connection.execute("DELETE FROM acp_metadata WHERE session_id = ?", (session_id,))

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
        """Claim exclusive control, taking over provably dead local owners.

        A live claim from another process/worker is never stolen. A claim whose owner PID
        no longer exists (crash/restart) is replaced; ``stale_after`` (or the store's
        configured ``control_stale_after``) additionally allows reclaiming rows without
        PID information after a bounded age.
        """
        return await self._run(
            self._acquire_control_sync,
            session_id,
            principal,
            owner_token,
            stale_after if stale_after is not None else self.control_stale_after,
        )

    def _acquire_control_sync(
        self,
        session_id: str,
        principal: str,
        owner_token: str,
        stale_after: float | None,
    ) -> bool:
        import os

        connection = self._connect()
        timestamp = now_ms()
        with connection:
            row = connection.execute(
                "SELECT owner_token, owner_pid, acquired_at FROM session_control WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None:
                if row["owner_token"] == owner_token:
                    return True
                owner_pid = row["owner_pid"]
                if isinstance(owner_pid, int) and owner_pid > 0 and not _pid_alive(owner_pid):
                    connection.execute(
                        "DELETE FROM session_control WHERE session_id = ?", (session_id,)
                    )
                elif (
                    stale_after is not None
                    and owner_pid is None
                    and timestamp - int(row["acquired_at"]) > stale_after * 1000
                ):
                    connection.execute(
                        "DELETE FROM session_control WHERE session_id = ?", (session_id,)
                    )
                else:
                    return False
            connection.execute(
                "INSERT INTO session_control (session_id, principal, owner_token, owner_pid, acquired_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, principal, owner_token, os.getpid(), timestamp),
            )
        return True

    async def release_control(self, session_id: str, owner_token: str) -> None:
        await self._run(self._release_control_sync, session_id, owner_token)

    def _release_control_sync(self, session_id: str, owner_token: str) -> None:
        connection = self._connect()
        with connection:
            connection.execute(
                "DELETE FROM session_control WHERE session_id = ? AND owner_token = ?",
                (session_id, owner_token),
            )

    # --- configuration revisions -------------------------------------

    @staticmethod
    def _require_session_row(connection: Any, session_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Session {session_id!r} does not exist in this store.",
                details={"session_id": session_id},
            )

    async def append_revision(
        self,
        session_id: str,
        *,
        bindings: dict[str, dict[str, Any]],
        policy_revision: int,
        source: str,
        request_id: str | None = None,
    ) -> StoredRevision:
        return await self._run(
            self._append_revision_sync, session_id, bindings, policy_revision, source, request_id
        )

    def _append_revision_sync(
        self,
        session_id: str,
        bindings: dict[str, dict[str, Any]],
        policy_revision: int,
        source: str,
        request_id: str | None,
    ) -> StoredRevision:
        connection = self._connect()
        payload = json.dumps(bindings, sort_keys=True, separators=(",", ":"))
        try:
            with connection:
                self._require_session_row(connection, session_id)
                row = connection.execute(
                    "SELECT MAX(revision) AS revision FROM configuration_revisions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                revision = int(row["revision"] or 0) + 1
                connection.execute(
                    "INSERT INTO configuration_revisions (session_id, revision, bindings, policy_revision, "
                    "source, request_id, applied, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (session_id, revision, payload, policy_revision, source, request_id, now_ms()),
                )
                connection.execute(
                    "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                    (now_ms(), session_id),
                )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                "REVISION_CONFLICT",
                "A concurrent write allocated the next configuration revision; retry the change.",
                details={"session_id": session_id},
            ) from exc
        return StoredRevision(
            session_id=session_id,
            revision=revision,
            bindings=bindings,
            policy_revision=policy_revision,
            source=source,
        )

    async def latest_revision(self, session_id: str) -> StoredRevision | None:
        return await self._run(self._latest_revision_sync, session_id)

    def _latest_revision_sync(self, session_id: str) -> StoredRevision | None:
        row = (
            self._connect()
            .execute(
                "SELECT * FROM configuration_revisions WHERE session_id = ? ORDER BY revision DESC LIMIT 1",
                (session_id,),
            )
            .fetchone()
        )
        return _revision(row) if row is not None else None

    async def get_revision(self, session_id: str, revision: int) -> StoredRevision | None:
        return await self._run(self._get_revision_sync, session_id, revision)

    def _get_revision_sync(self, session_id: str, revision: int) -> StoredRevision | None:
        row = (
            self._connect()
            .execute(
                "SELECT * FROM configuration_revisions WHERE session_id = ? AND revision = ?",
                (session_id, revision),
            )
            .fetchone()
        )
        return _revision(row) if row is not None else None

    async def mark_revision_applied(
        self,
        session_id: str,
        revision: int,
        *,
        applied: bool = True,
    ) -> None:
        await self._run(self._mark_revision_applied_sync, session_id, revision, applied)

    def _mark_revision_applied_sync(self, session_id: str, revision: int, applied: bool) -> None:
        connection = self._connect()
        with connection:
            connection.execute(
                "UPDATE configuration_revisions SET applied = ? WHERE session_id = ? AND revision <= ?",
                (1 if applied else 0, session_id, revision),
            )

    # --- configuration transactions / retries ------------------------

    async def claim_transaction(
        self,
        session_id: str,
        request_id: str,
        payload_hash: str,
    ) -> TransactionClaim:
        return await self._run(self._claim_transaction_sync, session_id, request_id, payload_hash)

    def _claim_transaction_sync(
        self, session_id: str, request_id: str, payload_hash: str
    ) -> TransactionClaim:
        connection = self._connect()
        timestamp = now_ms()
        try:
            with connection:
                self._require_session_row(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM configuration_transactions "
                    "WHERE session_id = ? AND request_id = ?",
                    (session_id, request_id),
                ).fetchone()
                if row is not None:
                    if not payload_hash_matches(row["payload_hash"], payload_hash):
                        raise HarnessError(
                            "INVALID_CONFIGURATION",
                            f"Request id {request_id!r} was already used with a different payload.",
                            details={"request_id": request_id, "session_id": session_id},
                        )
                    if row["status"] == "committed":
                        snapshot = (
                            json.loads(row["result_snapshot"]) if row["result_snapshot"] else None
                        )
                        return TransactionClaim(
                            status="committed",
                            snapshot=snapshot,
                            revision=row["revision"],
                        )
                    if row["status"] == "in_progress":
                        raise HarnessError(
                            "REVISION_CONFLICT",
                            "The same configuration request is currently in progress; retry shortly.",
                            details={"request_id": request_id, "session_id": session_id},
                        )
                    # A failed attempt may be retried with the same payload.
                    connection.execute(
                        "UPDATE configuration_transactions SET status = 'in_progress', "
                        "updated_at = ? WHERE session_id = ? AND request_id = ?",
                        (timestamp, session_id, request_id),
                    )
                    return TransactionClaim(status="claimed")
                connection.execute(
                    "INSERT INTO configuration_transactions "
                    "(session_id, request_id, payload_hash, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'in_progress', ?, ?)",
                    (session_id, request_id, payload_hash, timestamp, timestamp),
                )
                return TransactionClaim(status="claimed")
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                "REVISION_CONFLICT",
                "The same configuration request was claimed concurrently; retry shortly.",
                details={"request_id": request_id, "session_id": session_id},
            ) from exc

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
        """Commit a claimed configuration transaction.

        When ``create_revision`` is true, a new selected revision is inserted and
        ``snapshot_builder(revision)`` produces the stored result snapshot inside the same
        transaction. When false, the request was an idempotent no-op: no revision is
        created and the current revision's snapshot is recorded.
        """
        return await self._run(
            self._commit_transaction_sync,
            session_id,
            request_id,
            bindings,
            policy_revision,
            snapshot_builder,
            create_revision,
        )

    def _commit_transaction_sync(
        self,
        session_id: str,
        request_id: str,
        bindings: dict[str, dict[str, Any]] | None,
        policy_revision: int,
        snapshot_builder: Any,
        create_revision: bool,
    ) -> StoredRevision:
        connection = self._connect()
        timestamp = now_ms()
        try:
            with connection:
                self._require_session_row(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM configuration_transactions "
                    "WHERE session_id = ? AND request_id = ?",
                    (session_id, request_id),
                ).fetchone()
                if row is None:
                    raise HarnessError(
                        "INVALID_CONFIGURATION",
                        "Configuration transaction was not claimed before commit.",
                        details={"session_id": session_id, "request_id": request_id},
                    )
                if row["status"] == "committed":
                    stored = self._get_revision_sync(session_id, int(row["revision"]))
                    if stored is not None:
                        return stored
                latest = connection.execute(
                    "SELECT MAX(revision) AS revision FROM configuration_revisions "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                current_revision = int(latest["revision"] or 0)
                if create_revision:
                    revision = current_revision + 1
                    payload = json.dumps(bindings or {}, sort_keys=True, separators=(",", ":"))
                    connection.execute(
                        "INSERT INTO configuration_revisions "
                        "(session_id, revision, bindings, policy_revision, source, request_id, "
                        "applied, created_at) VALUES (?, ?, ?, ?, 'override', ?, 0, ?)",
                        (
                            session_id,
                            revision,
                            payload,
                            policy_revision,
                            request_id,
                            timestamp,
                        ),
                    )
                else:
                    revision = current_revision
                snapshot = snapshot_builder(revision) if callable(snapshot_builder) else {}
                connection.execute(
                    "UPDATE configuration_transactions SET status = 'committed', "
                    "result_snapshot = ?, revision = ?, updated_at = ? "
                    "WHERE session_id = ? AND request_id = ?",
                    (
                        json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                        revision,
                        timestamp,
                        session_id,
                        request_id,
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                    (timestamp, session_id),
                )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                "REVISION_CONFLICT",
                "A concurrent write allocated the next configuration revision; retry the change.",
                details={"session_id": session_id, "request_id": request_id},
            ) from exc
        stored = self._get_revision_sync(session_id, revision)
        if stored is None:  # pragma: no cover - a session always has revision 1
            stored = StoredRevision(
                session_id=session_id,
                revision=revision,
                bindings=dict(bindings or {}),
                policy_revision=policy_revision,
                source="override",
            )
        return stored

    async def fail_transaction(self, session_id: str, request_id: str) -> None:
        await self._run(self._fail_transaction_sync, session_id, request_id)

    def _fail_transaction_sync(self, session_id: str, request_id: str) -> None:
        connection = self._connect()
        with connection:
            # Never downgrade an already committed request: a client whose commit response
            # was lost must still get the original snapshot on retry.
            connection.execute(
                "UPDATE configuration_transactions SET status = 'failed', updated_at = ? "
                "WHERE session_id = ? AND request_id = ? AND status != 'committed'",
                (now_ms(), session_id, request_id),
            )

    async def fail_in_progress_transactions(self, session_id: str) -> int:
        """Mark abandoned in-progress claims failed after a worker crash.

        Called only after a new worker acquired exclusive session control, so no other
        process can still be committing them. Completed claims are never changed.
        """
        return await self._run(self._fail_in_progress_transactions_sync, session_id)

    def _fail_in_progress_transactions_sync(self, session_id: str) -> int:
        connection = self._connect()
        with connection:
            cursor = connection.execute(
                "UPDATE configuration_transactions SET status = 'failed', updated_at = ? "
                "WHERE session_id = ? AND status = 'in_progress'",
                (now_ms(), session_id),
            )
            return int(cursor.rowcount or 0)

    # --- turns --------------------------------------------------------

    async def create_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        active_revision: int,
        prompt: Any,
    ) -> StoredTurn:
        return await self._run(self._create_turn_sync, session_id, turn_id, active_revision, prompt)

    def _create_turn_sync(
        self, session_id: str, turn_id: str, active_revision: int, prompt: Any
    ) -> StoredTurn:
        connection = self._connect()
        timestamp = now_ms()
        with connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(turn_index), 0) AS turn_index FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turn_index = int(row["turn_index"]) + 1
            connection.execute(
                "INSERT INTO turns (session_id, turn_id, turn_index, active_revision, status, prompt, "
                "started_at) VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (
                    session_id,
                    turn_id,
                    turn_index,
                    active_revision,
                    json.dumps(prompt, sort_keys=True),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
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
        await self._run(
            self._finalize_turn_sync,
            session_id,
            turn_id,
            status,
            output,
            error,
            usage,
            list(messages or []),
        )

    def _finalize_turn_sync(
        self,
        session_id: str,
        turn_id: str,
        status: str,
        output: Any,
        error: Any,
        usage: Any,
        messages: list[Any],
    ) -> None:
        connection = self._connect()
        timestamp = now_ms()
        with connection:
            connection.execute(
                "UPDATE turns SET status = ?, output = ?, error = ?, usage = ?, finished_at = ? "
                "WHERE session_id = ? AND turn_id = ?",
                (
                    status,
                    json.dumps(output, sort_keys=True) if output is not None else None,
                    json.dumps(error, sort_keys=True) if error is not None else None,
                    json.dumps(usage, sort_keys=True) if usage is not None else None,
                    timestamp,
                    session_id,
                    turn_id,
                ),
            )
            if messages:
                row = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                seq = int(row["seq"])
                for message in messages:
                    seq += 1
                    connection.execute(
                        "INSERT INTO messages (session_id, seq, turn_id, message, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (session_id, seq, turn_id, json.dumps(message), timestamp),
                    )
            connection.execute(
                "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                (timestamp, session_id),
            )

    async def mark_turn_interrupted(self, session_id: str) -> list[str]:
        """Mark turns left ``running`` by a crash as interrupted; returns their ids."""
        return await self._run(self._mark_turn_interrupted_sync, session_id)

    def _mark_turn_interrupted_sync(self, session_id: str) -> list[str]:
        connection = self._connect()
        with connection:
            rows = connection.execute(
                "SELECT turn_id FROM turns WHERE session_id = ? AND status = 'running'",
                (session_id,),
            ).fetchall()
            ids = [row["turn_id"] for row in rows]
            connection.execute(
                "UPDATE turns SET status = 'interrupted', finished_at = ? "
                "WHERE session_id = ? AND status = 'running'",
                (now_ms(), session_id),
            )
        return ids

    async def get_turn(self, session_id: str, turn_id: str) -> StoredTurn | None:
        return await self._run(self._get_turn_sync, session_id, turn_id)

    def _get_turn_sync(self, session_id: str, turn_id: str) -> StoredTurn | None:
        row = (
            self._connect()
            .execute(
                "SELECT * FROM turns WHERE session_id = ? AND turn_id = ?", (session_id, turn_id)
            )
            .fetchone()
        )
        return _turn(row) if row is not None else None

    async def list_turns(self, session_id: str) -> list[StoredTurn]:
        return await self._run(self._list_turns_sync, session_id)

    def _list_turns_sync(self, session_id: str) -> list[StoredTurn]:
        rows = (
            self._connect()
            .execute("SELECT * FROM turns WHERE session_id = ? ORDER BY turn_index", (session_id,))
            .fetchall()
        )
        return [_turn(row) for row in rows]

    # --- history ------------------------------------------------------

    async def append_messages(
        self, session_id: str, turn_id: str | None, messages: list[Any]
    ) -> None:
        await self._run(self._append_messages_sync, session_id, turn_id, list(messages))

    def _append_messages_sync(
        self, session_id: str, turn_id: str | None, messages: list[Any]
    ) -> None:
        if not messages:
            return
        connection = self._connect()
        timestamp = now_ms()
        with connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["seq"])
            for message in messages:
                seq += 1
                connection.execute(
                    "INSERT INTO messages (session_id, seq, turn_id, message, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, seq, turn_id, json.dumps(message), timestamp),
                )

    async def load_messages_json(self, session_id: str) -> str:
        return await self._run(self._load_messages_json_sync, session_id)

    def _load_messages_json_sync(self, session_id: str) -> str:
        rows = (
            self._connect()
            .execute(
                "SELECT message FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            )
            .fetchall()
        )
        return "[" + ",".join(row["message"] for row in rows) + "]"

    async def message_count(self, session_id: str) -> int:
        return await self._run(self._message_count_sync, session_id)

    def _message_count_sync(self, session_id: str) -> int:
        row = (
            self._connect()
            .execute("SELECT COUNT(*) AS count FROM messages WHERE session_id = ?", (session_id,))
            .fetchone()
        )
        return int(row["count"])

    # --- workspace identities ----------------------------------------

    async def get_workspace_record(self, session_id: str) -> Any | None:
        """Load a session's durable workspace identity, if one is stored."""
        return await self._run(self._get_workspace_record_sync, session_id)

    def _get_workspace_record_sync(self, session_id: str) -> Any | None:
        from .workspaces import WorkspaceRecord

        row = (
            self._connect()
            .execute("SELECT record FROM workspace_records WHERE session_id = ?", (session_id,))
            .fetchone()
        )
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

    async def save_workspace_record(self, record: Any) -> None:
        """Persist one workspace record (upsert)."""
        await self._run(self._save_workspace_record_sync, record)

    def _save_workspace_record_sync(self, record: Any) -> None:
        payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
        connection = self._connect()
        with connection:
            connection.execute(
                "INSERT INTO workspace_records (session_id, record, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET record = excluded.record, "
                "updated_at = excluded.updated_at",
                (record.session_id, payload, now_ms()),
            )

    async def delete_workspace_record(self, session_id: str) -> None:
        """Remove a session's workspace identity after its environment was deleted."""
        await self._run(self._delete_workspace_record_sync, session_id)

    def _delete_workspace_record_sync(self, session_id: str) -> None:
        connection = self._connect()
        with connection:
            connection.execute("DELETE FROM workspace_records WHERE session_id = ?", (session_id,))

    async def list_workspace_records(self) -> list[Any]:
        """All retained workspace identities (for TTL cleanup)."""
        return await self._run(self._list_workspace_records_sync)

    def _list_workspace_records_sync(self) -> list[Any]:
        from .workspaces import WorkspaceRecord

        rows = (
            self._connect()
            .execute("SELECT record FROM workspace_records ORDER BY session_id")
            .fetchall()
        )
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
        """Load retained state for a binding, never as an authorization decision."""
        return await self._run(self._get_plugin_state_sync, session_id, binding_id, schema_version)

    def _get_plugin_state_sync(
        self, session_id: str, binding_id: str, schema_version: int | None
    ) -> dict[str, Any] | None:
        row = (
            self._connect()
            .execute(
                "SELECT schema_version, state FROM plugin_state "
                "WHERE session_id = ? AND binding_id = ?",
                (session_id, binding_id),
            )
            .fetchone()
        )
        if row is None:
            return None
        if schema_version is not None and int(row["schema_version"]) != schema_version:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Stored state for binding {binding_id!r} uses schema "
                f"{row['schema_version']}, not the requested {schema_version}.",
                details={
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
                details={"binding_id": binding_id},
            ) from exc
        if not isinstance(state, dict):
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Stored state for binding {binding_id!r} is not an object.",
                details={"binding_id": binding_id},
            )
        return state

    async def set_plugin_state(
        self, session_id: str, binding_id: str, *, schema_version: int, state: Mapping[str, Any]
    ) -> None:
        await self._run(self._set_plugin_state_sync, session_id, binding_id, schema_version, state)

    def _set_plugin_state_sync(
        self, session_id: str, binding_id: str, schema_version: int, state: Mapping[str, Any]
    ) -> None:
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version < 1
        ):
            raise HarnessError(
                "INVALID_CONFIGURATION", "schema_version must be a positive integer."
            )
        try:
            payload = json.dumps(
                dict(state), sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "Capability state must be a JSON-serializable object.",
                details={"binding_id": binding_id},
            ) from exc
        connection = self._connect()
        with connection:
            row = connection.execute(
                "SELECT schema_version FROM plugin_state WHERE session_id = ? AND binding_id = ?",
                (session_id, binding_id),
            ).fetchone()
            if row is not None and int(row["schema_version"]) != schema_version:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Binding {binding_id!r} state uses schema {row['schema_version']}; "
                    "a schema change requires a new binding identity or session.",
                    details={
                        "binding_id": binding_id,
                        "stored_schema_version": int(row["schema_version"]),
                        "schema_version": schema_version,
                    },
                )
            session = connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise HarnessError(
                    "SESSION_CLOSED",
                    f"Session {session_id!r} does not exist.",
                    details={"session_id": session_id},
                )
            connection.execute(
                "INSERT INTO plugin_state "
                "(session_id, binding_id, schema_version, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, binding_id) DO UPDATE SET "
                "state = excluded.state, updated_at = excluded.updated_at",
                (session_id, binding_id, schema_version, payload, now_ms()),
            )

    async def delete_plugin_state(self, session_id: str, binding_id: str) -> None:
        await self._run(self._delete_plugin_state_sync, session_id, binding_id)

    def _delete_plugin_state_sync(self, session_id: str, binding_id: str) -> None:
        connection = self._connect()
        with connection:
            connection.execute(
                "DELETE FROM plugin_state WHERE session_id = ? AND binding_id = ?",
                (session_id, binding_id),
            )

    # --- transcript ---------------------------------------------------

    async def append_transcript(
        self,
        session_id: str,
        turn_id: str | None,
        kind: str,
        data: dict[str, Any],
    ) -> int:
        return await self._run(self._append_transcript_sync, session_id, turn_id, kind, data)

    def _append_transcript_sync(
        self, session_id: str, turn_id: str | None, kind: str, data: dict[str, Any]
    ) -> int:
        connection = self._connect()
        timestamp = now_ms()
        with connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM transcript WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["seq"]) + 1
            connection.execute(
                "INSERT INTO transcript (session_id, seq, turn_id, kind, data, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, seq, turn_id, kind, json.dumps(data), timestamp),
            )
        return seq

    async def append_transcript_text(
        self,
        session_id: str,
        seq: int,
        appended: str,
        turn_id: str | None,
    ) -> None:
        await self._run(self._append_transcript_text_sync, session_id, seq, appended, turn_id)

    def _append_transcript_text_sync(
        self, session_id: str, seq: int, appended: str, turn_id: str | None
    ) -> None:
        connection = self._connect()
        with connection:
            row = connection.execute(
                "SELECT data FROM transcript WHERE session_id = ? AND seq = ?", (session_id, seq)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO transcript (session_id, seq, turn_id, kind, data, created_at) "
                    "VALUES (?, ?, ?, 'text_delta', ?, ?)",
                    (session_id, seq, turn_id, json.dumps({"text": appended}), now_ms()),
                )
                return
            data = json.loads(row["data"])
            data["text"] = data.get("text", "") + appended
            connection.execute(
                "UPDATE transcript SET data = ? WHERE session_id = ? AND seq = ?",
                (json.dumps(data), session_id, seq),
            )

    async def load_transcript(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._run(self._load_transcript_sync, session_id, turn_id, limit)

    def _load_transcript_sync(
        self, session_id: str, turn_id: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        suffix = "" if limit is None else " LIMIT ?"
        if turn_id is None:
            rows = connection.execute(
                "SELECT * FROM transcript WHERE session_id = ? ORDER BY seq" + suffix,
                (session_id, limit) if limit is not None else (session_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM transcript WHERE session_id = ? AND turn_id = ? "
                "ORDER BY seq" + suffix,
                (session_id, turn_id, limit) if limit is not None else (session_id, turn_id),
            ).fetchall()
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


def _summary(row: Any) -> SessionSummary:
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
        cwd=row["cwd"] if "cwd" in row.keys() else None,
        workspace_managed=bool(row["workspace_managed"])
        if "workspace_managed" in row.keys()
        else False,
    )


def _revision(row: Any) -> StoredRevision:
    return StoredRevision(
        session_id=row["session_id"],
        revision=row["revision"],
        bindings=json.loads(row["bindings"]),
        policy_revision=row["policy_revision"],
        source=row["source"],
        applied=bool(row["applied"]),
    )


def _turn(row: Any) -> StoredTurn:
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


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):  # pragma: no cover - other process's uid barrier
        return True
    return True
