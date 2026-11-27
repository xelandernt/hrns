"""SQLite persistence for the built-in stdio agent."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from .acp.store import (
    _PAGE_SIZE,
    SessionAccess,
    SessionStoreError,
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
    def _connect(self) -> Generator[sqlite3.Connection]:
        try:
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
        except (OSError, sqlite3.Error) as exc:
            detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            raise SessionStoreError(
                f"Could not access the local session database ({detail})."
            ) from exc

    @contextmanager
    def _model_preferences(self) -> Generator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS startup_models (
                    config_path TEXT PRIMARY KEY,
                    model TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS provider_models (
                    config_path TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    PRIMARY KEY (config_path, provider)
                )
            """)
            yield db

    def startup_model(self, config_path: Path) -> str | None:
        """Read the last explicitly selected model for this configuration."""
        with self._model_preferences() as db:
            row = db.execute(
                "SELECT model FROM startup_models WHERE config_path = ?", (str(config_path),)
            ).fetchone()
        return row[0] if row is not None else None

    async def remember_startup_model(self, config_path: Path, model: str) -> None:
        await asyncio.to_thread(self._write_startup_model, config_path, model)

    def provider_models(self, config_path: Path) -> dict[str, str]:
        with self._model_preferences() as db:
            return dict(
                db.execute(
                    "SELECT provider, model FROM provider_models WHERE config_path = ?",
                    (str(config_path),),
                )
            )

    def _write_startup_model(self, config_path: Path, model: str) -> None:
        with self._model_preferences() as db:
            db.execute(
                """
                INSERT INTO startup_models (config_path, model) VALUES (?, ?)
                ON CONFLICT (config_path) DO UPDATE SET model = excluded.model
                """,
                (str(config_path), model),
            )
            db.execute(
                """
                INSERT INTO provider_models (config_path, provider, model) VALUES (?, ?, ?)
                ON CONFLICT (config_path, provider) DO UPDATE SET model = excluded.model
                """,
                (str(config_path), model.partition(":")[0], model),
            )

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
                cursor = db.execute(
                    "UPDATE acp_sessions SET title = coalesce(?, title), updated = ?, state = ? WHERE id = ?",
                    (title, updated, payload, session_id),
                )
                if cursor.rowcount == 0:
                    raise SessionStoreError(
                        f"Session {session_id!r} no longer exists in the store."
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
