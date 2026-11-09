"""Built-in agent launch recipes; this module never imports the AI runtime."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from hrns.shared.config import HrnsConfig, Profile, config_files
from hrns.shared.config.credentials import store_api_key, stored_api_key_ref

from ..shared.errors import HrnsClientError
from .launch import ConnectionTarget, local_cwd

__all__ = [
    "AgentCatalog",
    "agent_command",
    "agent_credential_available",
    "agent_detail",
    "agent_login_providers",
    "agent_model_catalog",
    "agent_target",
    "agent_thinking_support",
    "store_api_key",
    "stored_api_key_ref",
]


@dataclass(frozen=True, slots=True)
class AgentCatalog:
    """Provider/model ids plus the reasoning support the runtime advertised."""

    models: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: provider -> model -> ``"supported"``/``"always"``
    thinking: dict[str, dict[str, str]] = field(default_factory=dict)
    #: providers that authenticate through an account login (OAuth device flow)
    logins: frozenset[str] = frozenset()


_CATALOG_LOCK = threading.Lock()
_CATALOG: AgentCatalog | None = None


def agent_command() -> list[str]:
    """Command that runs the built-in ACP agent in a separate process."""
    return [sys.executable, "-m", "hrns.server.runtime"]


def _load_catalog() -> AgentCatalog:
    command = agent_command()
    try:
        completed = subprocess.run(
            [*command, "--catalog"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return AgentCatalog()
    entries = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return AgentCatalog()
    models_by_provider: dict[str, tuple[str, ...]] = {}
    thinking_by_provider: dict[str, dict[str, str]] = {}
    login_providers: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        models = entry.get("models")
        if not isinstance(name, str) or not name or not isinstance(models, list):
            continue
        models_by_provider[name] = tuple(model for model in models if isinstance(model, str))
        if entry.get("login") is True:
            login_providers.add(name)
        thinking = entry.get("thinking")
        if isinstance(thinking, dict):
            thinking_by_provider[name] = {
                model: level
                for model, level in thinking.items()
                if isinstance(model, str) and level in ("supported", "always")
            }
    return AgentCatalog(
        models=models_by_provider,
        thinking=thinking_by_provider,
        logins=frozenset(login_providers),
    )


def _catalog() -> AgentCatalog:
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    with _CATALOG_LOCK:
        if _CATALOG is None:
            _CATALOG = _load_catalog()
        return _CATALOG


def agent_model_catalog() -> dict[str, tuple[str, ...]]:
    """Providers and model ids advertised by the installed Pydantic AI runtime.

    The client never imports Pydantic AI; ``hrns agent --catalog`` reads the
    pinned runtime's registry and returns JSON. An unavailable runtime produces an
    empty catalog so the dialog can still fall back to manual provider/model input.
    """
    return _catalog().models


def agent_login_providers() -> frozenset[str]:
    """Providers the runtime can authenticate with an account login."""
    return _catalog().logins


def agent_thinking_support(provider: str, model: str) -> str | None:
    """``"supported"``, ``"always"``, ``"custom"``, or ``None``.

    ``provider`` is the Pydantic AI provider id, not an entry name: two entries of the
    same type share a catalog line. ``None`` means the model is known and its profile
    says it cannot reason. A custom id outside the catalog returns ``"custom"`` so the
    user can opt in rather than being blocked by a registry that does not know it.
    """
    catalog = _catalog()
    support = catalog.thinking.get(provider, {}).get(model)
    if support is not None:
        return support
    if model in catalog.models.get(provider, ()):
        return None
    return "custom"


def agent_credential_available(provider: str, api_key_env: str = "") -> bool:
    """Whether a provider entry can authenticate right now.

    An entry passes when its environment variable is set or the keyring holds a secret
    for its account; account logins and pasted keys both land in the keyring.
    """
    if api_key_env and os.environ.get(api_key_env):
        return True
    return stored_api_key_ref(provider, api_key_env) is not None


def _load_agent(config: Path | None) -> HrnsConfig:
    try:
        settings = HrnsConfig(config_file=config) if config is not None else HrnsConfig()
        settings.require_model()
        settings.require_entry()
    except (ValueError, OSError) as exc:
        raise HrnsClientError(str(exc)) from exc
    return settings


def _agent_profile(settings: HrnsConfig, cwd: str | None) -> Profile:
    if importlib.util.find_spec("pydantic_ai") is None:
        raise HrnsClientError(
            "The built-in agent needs the server extra. Install `hrns[server]`, "
            "or connect to a remote agent."
        )
    # The runtime reads the same document this client just read. An explicitly selected
    # file is passed through; otherwise the runtime discovers the user and project
    # documents itself, so a merged view stays merged instead of being flattened.
    command = agent_command()
    args = [*command[1:]]
    if settings.config_file is not None:
        args.extend(["--config", str(settings.config_file.expanduser().absolute())])
    elif not config_files():
        args.append("--env-only")
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


def _target(settings: HrnsConfig, cwd: str | None) -> ConnectionTarget:
    profile = _agent_profile(settings, cwd)
    return ConnectionTarget(
        name=profile.name,
        kind="local",
        profile=profile,
        cwd=local_cwd(profile),
    )


def agent_detail(config: Path | None = None, cwd: str | None = None) -> str:
    """The picker's description of the built-in agent, without reading the keyring."""
    return _target(_load_agent(config), cwd).location_text()


def agent_target(
    config: Path | None = None,
    cwd: str | None = None,
    *,
    api_key_ref: str | None = None,
) -> ConnectionTarget:
    settings = _load_agent(config)
    entry = settings.require_entry()
    env_has_key = bool(
        settings.api_key is not None or (entry.api_key_env and os.environ.get(entry.api_key_env))
    )
    if api_key_ref is None and not env_has_key:
        api_key_ref = stored_api_key_ref(settings.provider, entry.api_key_env)
    if api_key_ref is None:
        try:
            settings.resolved_api_key()
        except ValueError as exc:
            raise HrnsClientError(str(exc)) from exc
    target = _target(settings, cwd)
    if api_key_ref is not None:
        target.profile.env["HRNS_API_KEY"] = api_key_ref
    return target
