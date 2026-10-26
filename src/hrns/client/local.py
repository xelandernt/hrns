"""Local runtime launch recipes; this module never imports the AI runtime."""

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from hrns.config import LocalSettings

from .errors import HrnsClientError
from .launch import ConnectionTarget, local_cwd
from .profiles import Profile


@dataclass(frozen=True, slots=True)
class LocalCatalog:
    """Provider/model ids plus the reasoning support the runtime advertised."""

    models: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: provider -> model -> ``"supported"``/``"always"``
    thinking: dict[str, dict[str, str]] = field(default_factory=dict)


_CATALOG_LOCK = threading.Lock()
_CATALOG: LocalCatalog | None = None

_LOCAL_KEYRING_SERVICE = "hrns.local"


def _keyring_account(provider: str, api_key_env: str) -> str:
    return api_key_env or provider or "default"


def stored_api_key_ref(provider: str, api_key_env: str) -> str | None:
    """Keyring reference for a previously saved local provider key."""
    account = _keyring_account(provider, api_key_env)
    try:
        import keyring

        if keyring.get_password(_LOCAL_KEYRING_SERVICE, account) is not None:
            return f"keyring:{_LOCAL_KEYRING_SERVICE}/{account}"
    except Exception:
        return None
    return None


def store_api_key(provider: str, api_key_env: str, api_key: str) -> str | None:
    """Save a pasted key in the OS keyring; return a reference for the child env."""
    account = _keyring_account(provider, api_key_env)
    try:
        import keyring

        keyring.set_password(_LOCAL_KEYRING_SERVICE, account, api_key)
        return f"keyring:{_LOCAL_KEYRING_SERVICE}/{account}"
    except Exception:
        return None


def _sibling_script(name: str) -> str | None:
    """A console script installed next to this interpreter (venv, pipx, uv tool)."""
    candidate = Path(sys.executable).with_name(name)
    return str(candidate) if candidate.is_file() else None


def _load_catalog() -> LocalCatalog:
    executable = shutil.which("hrns-local") or _sibling_script("hrns-local")
    if executable is None:
        return LocalCatalog()
    try:
        completed = subprocess.run(
            [executable, "--catalog"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return LocalCatalog()
    entries = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return LocalCatalog()
    models_by_provider: dict[str, tuple[str, ...]] = {}
    thinking_by_provider: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        models = entry.get("models")
        if not isinstance(name, str) or not name or not isinstance(models, list):
            continue
        models_by_provider[name] = tuple(model for model in models if isinstance(model, str))
        thinking = entry.get("thinking")
        if isinstance(thinking, dict):
            thinking_by_provider[name] = {
                model: level
                for model, level in thinking.items()
                if isinstance(model, str) and level in ("supported", "always")
            }
    return LocalCatalog(models=models_by_provider, thinking=thinking_by_provider)


def _catalog() -> LocalCatalog:
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    with _CATALOG_LOCK:
        if _CATALOG is None:
            _CATALOG = _load_catalog()
        return _CATALOG


def local_model_catalog() -> dict[str, tuple[str, ...]]:
    """Providers and model ids advertised by the installed Pydantic AI runtime.

    The client never imports Pydantic AI; ``hrns-local --catalog`` reads the
    pinned runtime's registry and returns JSON. An unavailable runtime produces an
    empty catalog so the dialog can still fall back to manual provider/model input.
    """
    return _catalog().models


def local_thinking_support(provider: str, model: str) -> str | None:
    """``"supported"``, ``"always"``, ``"custom"``, or ``None``.

    ``None`` means the model is known and its profile says it cannot reason.
    A custom id outside the catalog returns ``"custom"`` so the user can opt in
    rather than being blocked by a registry that does not know the alias.
    """
    catalog = _catalog()
    support = catalog.thinking.get(provider, {}).get(model)
    if support is not None:
        return support
    if model in catalog.models.get(provider, ()):
        return None
    return "custom"


def local_target(
    config: Path | None = None,
    cwd: str | None = None,
    *,
    api_key_ref: str | None = None,
) -> ConnectionTarget:
    try:
        settings = LocalSettings(config_file=config) if config is not None else LocalSettings()
        settings.require_model()
        env_has_key = bool(
            settings.api_key is not None
            or (settings.api_key_env and os.environ.get(settings.api_key_env))
        )
        if api_key_ref is None and not env_has_key:
            api_key_ref = stored_api_key_ref(settings.provider, settings.api_key_env)
        if api_key_ref is None:
            settings.resolved_api_key()
    except (ValueError, OSError) as exc:
        raise HrnsClientError(str(exc)) from exc
    if shutil.which("hrns-local") is None:
        raise HrnsClientError(
            "Local mode requires hrns-local in PATH. Install 'hrns[runtime,openai]' "
            "for DeepSeek and OpenAI-compatible models."
        )
    path = settings.config_file.expanduser().absolute() if settings.config_file else None
    env = {name: f"env:{name}" for name in os.environ}
    if api_key_ref is not None:
        env["HRNS_LOCAL_API_KEY"] = api_key_ref
    profile = Profile(
        name=f"{settings.provider}:{settings.model}",
        command="hrns-local",
        args=["--config", str(path)] if path and path.is_file() else ["--env-only"],
        cwd=cwd or "",
        # Unlike generic ACP agents, the explicitly selected local runtime needs the
        # user's provider credentials/settings (AWS, Google, proxies, etc.). Keep
        # references here so secret values never enter profile display/storage.
        env=env,
    )
    return ConnectionTarget(
        name=profile.name,
        kind="local",
        profile=profile,
        agent=None,
        cwd=local_cwd(profile),
    )
