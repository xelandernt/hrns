"""Account logins for local providers, driven through the runtime's device flow.

The client never imports Pydantic AI: it starts ``hrns-local --login PROVIDER`` and
renders the JSON events that process prints.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hrns.config import LocalSettings, keyring_account

from .errors import HrnsClientError
from .local import local_executable

__all__ = [
    "DeviceAuthorization",
    "LoginResult",
    "LoginTarget",
    "login",
    "login_target",
]


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    """The code a user types while the runtime polls for approval."""

    user_code: str
    verification_uri: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class LoginTarget:
    """Which provider to sign in to, where its credential belongs, and as whom."""

    provider: str
    account: str
    #: False when the configured provider is a different one.
    configured: bool = True
    #: GitHub OAuth app to authenticate with; empty uses the provider's default.
    client_id: str = ""


@dataclass(frozen=True, slots=True)
class LoginResult:
    """A completed login, already stored by the runtime."""

    provider: str
    account: str
    reference: str
    models: tuple[str, ...] = ()


def login_target(provider: str | None = None, config: Path | None = None) -> LoginTarget:
    """Resolve the provider to sign in to and the keyring account that holds it.

    ``provider`` may be a configured entry name (``openai-work``) or a bare Pydantic AI
    provider id (``github-copilot``) when no entry uses that name yet.
    """
    try:
        settings = LocalSettings(config_file=config) if config is not None else LocalSettings()
    except (ValueError, OSError) as exc:
        raise HrnsClientError(str(exc)) from exc
    selected = (provider or settings.provider).strip()
    if not selected:
        raise HrnsClientError(
            "Name the provider to sign in to, for example `hrns login github-copilot`."
        )
    entry = settings.entry(selected)
    api_key_env = entry.api_key_env if entry is not None else ""
    provider_id = settings.provider_id(selected)
    client_id = ""
    if provider_id == "github-copilot":
        client_id = (entry.copilot_client_id if entry is not None else "") or ""
    return LoginTarget(
        provider=selected,
        account=keyring_account(selected, api_key_env),
        configured=selected == settings.provider,
        client_id=client_id,
    )


def _text(event: dict[str, object], key: str) -> str:
    value = event.get(key)
    return value if isinstance(value, str) else ""


def _count(event: dict[str, object], key: str) -> int:
    value = event.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _names(event: dict[str, object], key: str) -> tuple[str, ...]:
    value = event.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _authorized(target: LoginTarget, event: dict[str, object]) -> LoginResult:
    return LoginResult(
        provider=_text(event, "provider") or target.provider,
        account=_text(event, "account") or target.account,
        reference=_text(event, "reference"),
        models=_names(event, "models"),
    )


async def login(
    target: LoginTarget,
    *,
    on_authorization: Callable[[DeviceAuthorization], None],
) -> LoginResult:
    """Run the runtime's device flow until the token is stored.

    ``on_authorization`` runs once the code is available, so a terminal can show it
    before the caller starts waiting. Cancelling the awaited task stops the runtime.
    """
    executable = local_executable()
    if executable is None:
        raise HrnsClientError(
            "Account login requires the hrns-local command. Reinstall hrns so its "
            "console scripts are on PATH."
        )
    client_id = ["--client-id", target.client_id] if target.client_id else []
    process = await asyncio.create_subprocess_exec(
        executable,
        "--login",
        target.provider,
        "--account",
        target.account,
        *client_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:  # pragma: no cover - PIPEs were requested
        process.kill()
        await process.wait()
        raise HrnsClientError("Could not read the login process output.")
    authorized: LoginResult | None = None
    try:
        while line := await stdout.readline():
            try:
                raw: object = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            event: dict[str, object] = {str(key): value for key, value in raw.items()}
            kind = _text(event, "event")
            if kind == "device":
                on_authorization(
                    DeviceAuthorization(
                        user_code=_text(event, "userCode"),
                        verification_uri=_text(event, "verificationUri"),
                        expires_in=_count(event, "expiresIn"),
                    )
                )
            elif kind == "authorized":
                authorized = _authorized(target, event)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    if authorized is None:
        detail = (await stderr.read()).decode(errors="replace").strip()
        raise HrnsClientError(
            detail or f"{target.provider} login did not complete. Run `hrns login` again."
        )
    return authorized
