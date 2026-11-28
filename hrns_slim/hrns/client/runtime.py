"""Built-in agent launch recipes; this module never imports the AI runtime."""

import importlib.util
import os
import sys
from pathlib import Path

from hrns.shared.config import HrnsConfig, Profile
from hrns.shared.config.credentials import stored_api_key_ref

from ..shared.errors import HrnsClientError
from .launch import ConnectionTarget, local_cwd

__all__ = [
    "agent_command",
    "agent_target",
]


def agent_command() -> list[str]:
    """Command that runs the built-in ACP agent in a separate process."""
    return [sys.executable, "-m", "hrns.server.runtime"]


def _load_agent(config: Path | None) -> HrnsConfig:
    try:
        return HrnsConfig(config_file=config) if config is not None else HrnsConfig()
    except (ValueError, OSError) as exc:
        raise HrnsClientError(str(exc)) from exc


def _agent_profile(settings: HrnsConfig, cwd: str | None) -> Profile:
    if importlib.util.find_spec("pydantic_ai") is None:
        raise HrnsClientError(
            "The built-in agent needs the server extra. Install `hrns-slim[server]`, "
            "or connect to a remote agent."
        )
    # Pass through a selected configuration so the client and runtime use the same file.
    command = agent_command()
    args = [*command[1:]]
    if settings.config_file is not None:
        args.extend(["--config", str(settings.config_file.expanduser().absolute())])
    return Profile(
        name=f"{settings.provider}:{settings.model}",
        command=command[0],
        args=args,
        cwd=cwd or "",
        # Unlike generic ACP agents, the built-in agent needs the user's provider
        # credentials/settings (AWS, Google, proxies, etc.). Keep references here so
        # secret values never enter profile display/storage.
        env={name: f"env:{name}" for name in os.environ},
    )


def _target(
    settings: HrnsConfig, cwd: str | None, *, local_setup: bool = False
) -> ConnectionTarget:
    profile = _agent_profile(settings, cwd)
    if local_setup:
        profile.name = "Set up a model"
        profile.args.append("--setup")
    return ConnectionTarget(
        name=profile.name,
        kind="local",
        profile=profile,
        cwd=local_cwd(profile),
    )


def agent_target(
    config: Path | None = None,
    cwd: str | None = None,
    *,
    api_key_ref: str | None = None,
    allow_setup: bool = False,
) -> ConnectionTarget:
    settings = _load_agent(config)
    try:
        settings.require_model()
        entry = settings.require_entry()
    except ValueError as exc:
        if allow_setup:
            return _target(settings, cwd, local_setup=True)
        raise HrnsClientError(str(exc)) from exc
    env_has_key = bool(
        settings.api_key is not None or (entry.api_key_env and os.environ.get(entry.api_key_env))
    )
    if api_key_ref is None and not env_has_key:
        api_key_ref = stored_api_key_ref(settings.provider, entry.api_key_env)
    if api_key_ref is None:
        try:
            settings.resolved_api_key()
        except ValueError as exc:
            if allow_setup:
                return _target(settings, cwd, local_setup=True)
            raise HrnsClientError(str(exc)) from exc
    target = _target(settings, cwd)
    if api_key_ref is not None:
        target.profile.env["HRNS_API_KEY"] = api_key_ref
    return target
