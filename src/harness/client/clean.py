"""``hrns clean``: delete the local session history written by Harness.

Only local, Harness-owned SQLite files are removed. Saved profiles, local model
settings, and sessions hosted on a remote server are never touched.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import HarnessClientError
from .headless import EXIT_OK

__all__ = ["SessionArtifact", "clean", "session_artifacts"]


@dataclass(frozen=True, slots=True)
class SessionArtifact:
    """One local SQLite database that stores ACP sessions."""

    label: str
    path: Path


def _client_store_path() -> Path:
    """The local developer agent's session database."""
    try:
        from harness.config import LocalSettings

        return Path(LocalSettings().store).expanduser()
    except Exception:
        # A broken local config must not block cleanup; fall back to the default.
        from platformdirs import user_data_path

        return user_data_path("harness") / "local-sessions.db"


def _runtime_store_path() -> Path | None:
    """The host runtime's default session database, or ``None`` for a remote store."""
    explicit = os.environ.get("HARNESS_STORE")
    if explicit:
        if urlsplit(explicit).scheme in {"postgres", "postgresql"}:
            # Dropping a PostgreSQL database is never an implicit local cleanup.
            return None
        return Path(explicit).expanduser()
    from platformdirs import user_data_dir

    return Path(user_data_dir("harness", "harness")) / "runtime.sqlite3"


def session_artifacts() -> list[SessionArtifact]:
    """Existing local session databases, deduplicated by resolved path."""
    candidates: list[tuple[str, Path]] = [("local sessions", _client_store_path())]
    runtime_path = _runtime_store_path()
    if runtime_path is not None:
        candidates.append(("runtime sessions", runtime_path))
    artifacts: list[SessionArtifact] = []
    seen: set[Path] = set()
    for label, path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        artifacts.append(SessionArtifact(label, resolved))
    return artifacts


def _sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _session_count(path: Path) -> int | None:
    """Rows in ``acp_sessions``, or ``None`` when the file is not readable as one."""
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT COUNT(*) FROM acp_sessions").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _files_for(artifact: SessionArtifact) -> list[Path]:
    return [artifact.path, *_sidecars(artifact.path)]


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _describe(artifact: SessionArtifact) -> str:
    files = [path for path in _files_for(artifact) if path.exists()]
    size = sum(path.stat().st_size for path in files)
    count = _session_count(artifact.path)
    sessions = f"{count} session{'s' if count != 1 else ''}" if count is not None else "sessions"
    return (
        f"  {artifact.label:<18} {_display_path(artifact.path)}  ({sessions}, {_human_size(size)})"
    )


def _confirm(question: str) -> bool:
    try:
        answer = input(question)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def clean(*, assume_yes: bool = False, dry_run: bool = False) -> int:
    """Delete local session databases after confirmation; return an exit code."""
    artifacts = session_artifacts()
    if not artifacts:
        print("Nothing to clean: no local session history found.")
        return EXIT_OK

    print("This permanently deletes local session history:")
    for artifact in artifacts:
        print(_describe(artifact))
    if dry_run:
        print("\nDry run: nothing was deleted.")
        return EXIT_OK

    if not assume_yes:
        if not sys.stdin.isatty():
            raise HarnessClientError(
                "Refusing to delete local session history without confirmation in a "
                "non-interactive shell. Re-run with `--yes`."
            )
        if not _confirm("\nDelete these sessions? [y/N] "):
            print("Cancelled.")
            return EXIT_OK

    removed = 0
    failures: list[str] = []
    for artifact in artifacts:
        for path in _files_for(artifact):
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                failures.append(f"{_display_path(path)}: {exc}")

    for failure in failures:
        print(f"harness: error: could not delete {failure}", file=sys.stderr)
    if failures:
        raise HarnessClientError(
            f"Deleted {removed} file{'s' if removed != 1 else ''}, "
            f"but {len(failures)} could not be removed."
        )
    print(f"Removed local session history ({removed} files).")
    return EXIT_OK
