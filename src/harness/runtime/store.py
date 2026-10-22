"""SQLite persistence for ACP sessions.

The schema is deliberately small and fresh: one row per session holds the upstream
``StoredSession`` payload plus host-owned metadata. There is no turn table, transcript
journal, revision log, or workspace lease record. An existing database is opened without
being converted; only the ``acp_sessions`` table is created or used.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    created_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acp_sessions_scope
    ON acp_sessions(principal, deployment_id, last_active_at DESC);
"""


def default_store_path() -> Path:
    """Host default store location: ``HARNESS_STORE`` when set, else the user data dir."""
    explicit = os.environ.get("HARNESS_STORE")
    if explicit:
        return Path(explicit).expanduser()
    import platformdirs

    return Path(platformdirs.user_data_dir("harness", "harness")) / "runtime.sqlite3"


def _row_to_record(row: Any) -> SessionRecord:
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
        created_at=int(values["created_at"]),
        last_active_at=int(values["last_active_at"]),
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

    async def close(self) -> None:
        async with self._lock:
            connection = self._connection
            self._connection = None
            self._initialized = False
            if connection is not None:
                await asyncio.to_thread(connection.close)

    def _run(self, operation: Any, *args: Any) -> Any:
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
                    created_at, last_active_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', NULL, ?, ?)
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
                        "SELECT * FROM acp_sessions WHERE session_id = ? AND principal = ? "
                        "AND deployment_id = ?",
                        (session_id, principal, deployment_id),
                    )
                    .fetchone()
                ),
            )
        return _row_to_record(row) if row is not None else None

    async def list(self, principal: str, deployment_id: str) -> list[SessionRecord]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "SELECT * FROM acp_sessions WHERE principal = ? AND deployment_id = ? "
                        "ORDER BY last_active_at DESC, session_id ASC",
                        (principal, deployment_id),
                    )
                    .fetchall()
                ),
            )
        return [_row_to_record(row) for row in rows]

    async def delete(self, principal: str, deployment_id: str, session_id: str) -> bool:
        async with self._lock:
            changed = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "DELETE FROM acp_sessions WHERE session_id = ? AND principal = ? "
                        "AND deployment_id = ?",
                        (session_id, principal, deployment_id),
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
        values.extend([session_id, principal, deployment_id])

        async with self._lock:
            await asyncio.to_thread(
                self._run,
                lambda: self._connect().execute(
                    f"UPDATE acp_sessions SET {', '.join(assignments)} "
                    "WHERE session_id = ? AND principal = ? AND deployment_id = ?",
                    values,
                ),
            )

    async def save_scoped(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        session: "StoredSession",
    ) -> None:
        messages, updates, model = encode_session(session)
        active = now_ms()

        def operation() -> None:
            connection = self._connect()
            row = connection.execute(
                "SELECT principal, deployment_id FROM acp_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("session was not created in this scope")
            if row["principal"] != principal or row["deployment_id"] != deployment_id:
                raise PermissionError("session belongs to another scope")
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
        async with self._lock:
            row = await asyncio.to_thread(
                self._run,
                lambda: (
                    self._connect()
                    .execute(
                        "SELECT messages, updates, model FROM acp_sessions "
                        "WHERE session_id = ? AND principal = ? AND deployment_id = ?",
                        (session_id, principal, deployment_id),
                    )
                    .fetchone()
                ),
            )
        if row is None:
            return None
        return decode_session(row["messages"], row["updates"], row["model"])
