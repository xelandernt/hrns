"""``hrns clean``: delete local session history and, on request, the local config.

Only local, hrns-owned files are removed. Session history is the default target; the
provider configuration is deleted only when it is selected interactively or with
``--config``. Stored credentials follow the configuration only when asked for, and
sessions hosted on a remote server are never touched.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import HrnsClientError
from .headless import EXIT_OK

__all__ = [
    "ConfigArtifact",
    "SessionArtifact",
    "clean",
    "config_artifacts",
    "session_artifacts",
]


@dataclass(frozen=True, slots=True)
class SessionArtifact:
    """One local SQLite database that stores ACP sessions."""

    label: str
    path: Path


@dataclass(frozen=True, slots=True)
class ConfigArtifact:
    """One local provider configuration file and the entries it declares."""

    label: str
    path: Path
    #: ``(entry name, api_key_env)`` for every ``[providers.<name>]`` in the file.
    entries: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _client_store_path() -> Path:
    """The local developer agent's session database."""
    try:
        from hrns.config import LocalSettings

        return Path(LocalSettings().store).expanduser()
    except Exception:
        # A broken local config must not block cleanup; fall back to the default.
        from platformdirs import user_data_path

        return user_data_path("hrns") / "local-sessions.db"


def _runtime_store_path() -> Path | None:
    """The host runtime's default session database, or ``None`` for a remote store."""
    explicit = os.environ.get("HRNS_STORE")
    if explicit:
        if urlsplit(explicit).scheme in {"postgres", "postgresql"}:
            # Dropping a PostgreSQL database is never an implicit local cleanup.
            return None
        return Path(explicit).expanduser()
    from platformdirs import user_data_dir

    return Path(user_data_dir("hrns", "hrns")) / "runtime.sqlite3"


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


def _config_candidates() -> list[tuple[str, Path]]:
    """The project file and the user file, in the order the runtime reads them."""
    from platformdirs import user_config_path

    return [
        ("project config", Path.cwd() / "hrns.local.toml"),
        ("user config", user_config_path("hrns") / "local.toml"),
    ]


def _declared_entries(path: Path) -> tuple[tuple[str, str], ...]:
    """Provider entries in a config file, read without validating the hrns schema.

    A file written for an older hrns must still be deletable, so this reads the TOML
    directly instead of through ``LocalSettings``.
    """
    try:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return ()
    entries: list[tuple[str, str]] = []
    for name, entry in providers.items():
        if not isinstance(name, str):
            continue
        api_key_env = entry.get("api_key_env") if isinstance(entry, dict) else None
        entries.append((name, api_key_env if isinstance(api_key_env, str) else ""))
    return tuple(entries)


def config_artifacts() -> list[ConfigArtifact]:
    """Existing local provider configuration files, deduplicated by resolved path."""
    artifacts: list[ConfigArtifact] = []
    seen: set[Path] = set()
    for label, path in _config_candidates():
        resolved = path.expanduser().resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        artifacts.append(ConfigArtifact(label, resolved, _declared_entries(resolved)))
    return artifacts


def _stored_accounts(artifact: ConfigArtifact) -> list[str]:
    """Keyring accounts of this file's entries that currently hold a secret."""
    from hrns.config.credentials import keyring_account, stored_secret

    accounts: list[str] = []
    for name, api_key_env in artifact.entries:
        account = keyring_account(name, api_key_env)
        if account in accounts:
            continue
        if stored_secret(account) is not None:
            accounts.append(account)
    return accounts


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


def _describe_config(artifact: ConfigArtifact) -> str:
    count = len(artifact.entries)
    entries = f"{count} provider{'s' if count != 1 else ''}" if count else "no providers"
    return f"  {artifact.label:<18} {_display_path(artifact.path)}  ({entries})"


def _confirm(question: str) -> bool:
    try:
        answer = input(question)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _remove(paths: list[Path]) -> tuple[int, list[str]]:
    """Delete files, returning how many went and one message per failure."""
    removed = 0
    failures: list[str] = []
    for path in paths:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f"{_display_path(path)}: {exc}")
    return removed, failures


def clean(
    *,
    assume_yes: bool = False,
    dry_run: bool = False,
    config: bool = False,
    keys: bool = False,
) -> int:
    """Delete local session history and, when selected, the local provider config."""
    sessions = session_artifacts()
    configs = config_artifacts()
    if not sessions and not configs:
        print("Nothing to clean: no local session history or provider config found.")
        return EXIT_OK

    if sessions:
        print("This permanently deletes local session history:")
        for artifact in sessions:
            print(_describe(artifact))
    if configs:
        if sessions:
            print()
        print("Local provider configuration:")
        for artifact in configs:
            print(_describe_config(artifact))
    if dry_run:
        print("\nDry run: nothing was deleted.")
        return EXIT_OK

    interactive = sys.stdin.isatty() and not assume_yes
    remove_sessions = bool(sessions)
    remove_config = bool(configs) and config
    if interactive and sessions:
        remove_sessions = _confirm("\nDelete these sessions? [y/N] ")
    if interactive and configs and not config:
        remove_config = _confirm("Delete the provider configuration? [y/N] ")
    if not interactive and sessions and not assume_yes:
        raise HrnsClientError(
            "Refusing to delete local session history without confirmation in a "
            "non-interactive shell. Re-run with `--yes`, and add `--config` to delete "
            "the provider configuration too."
        )
    if not interactive and not sessions and configs and not config and not keys:
        raise HrnsClientError(
            "Refusing to delete the provider configuration in a non-interactive shell. "
            "Re-run with `--config`."
        )

    accounts: list[str] = []
    if remove_config:
        for artifact in configs:
            accounts.extend(
                account for account in _stored_accounts(artifact) if account not in accounts
            )
    elif keys:
        print("Keys are deleted with the provider configuration; add --config to include it.")
    remove_keys = False
    if accounts:
        if keys:
            remove_keys = True
        elif interactive:
            remove_keys = _confirm(
                f"Also delete {len(accounts)} stored key"
                f"{'s' if len(accounts) != 1 else ''} from the OS keyring? [y/N] "
            )

    if not remove_sessions and not remove_config:
        print("Cancelled.")
        return EXIT_OK

    removed = 0
    failures: list[str] = []
    if remove_sessions:
        for artifact in sessions:
            count, problems = _remove(_files_for(artifact))
            removed += count
            failures.extend(problems)
        print(f"Removed local session history ({removed} file{'s' if removed != 1 else ''}).")
    if remove_config:
        deleted = 0
        for artifact in configs:
            count, problems = _remove([artifact.path])
            deleted += count
            failures.extend(problems)
        print(
            f"Removed local provider configuration ({deleted} file{'s' if deleted != 1 else ''})."
        )
    forgotten = 0
    if remove_keys:
        from hrns.config.credentials import delete_secret, stored_secret

        for account in accounts:
            delete_secret(account)
            if stored_secret(account) is None:
                forgotten += 1
        print(f"Deleted {forgotten} stored key{'s' if forgotten != 1 else ''} from the keyring.")
    elif accounts:
        print(
            f"Kept {len(accounts)} stored key{'s' if len(accounts) != 1 else ''} in the "
            "keyring; re-run with `--keys` to delete them."
        )

    for failure in failures:
        print(f"hrns: error: could not delete {failure}", file=sys.stderr)
    if failures:
        raise HrnsClientError(
            f"Deleted {removed} file{'s' if removed != 1 else ''}, "
            f"but {len(failures)} could not be removed."
        )
    return EXIT_OK
