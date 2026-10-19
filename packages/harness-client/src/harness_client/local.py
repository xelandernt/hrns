"""Local runtime launch recipes; this module never imports the AI runtime."""

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from harness_config import LocalSettings

from .errors import HarnessClientError
from .launch import ConnectionTarget, local_cwd
from .profiles import Profile

_CATALOG_LOCK = threading.Lock()
_CATALOG: dict[str, tuple[str, ...]] | None = None

_LOCAL_KEYRING_SERVICE = "harness.local"


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


def _load_catalog() -> dict[str, tuple[str, ...]]:
    executable = shutil.which("harness-local")
    if executable is None:
        return {}
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
        return {}
    entries = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}
    catalog: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        models = entry.get("models")
        if not isinstance(name, str) or not name or not isinstance(models, list):
            continue
        catalog[name] = tuple(str(model) for model in models if isinstance(model, str))
    return catalog


def local_model_catalog() -> dict[str, tuple[str, ...]]:
    """Providers and model ids advertised by the installed Pydantic AI runtime.

    The client never imports Pydantic AI; ``harness-local --catalog`` reads the
    pinned runtime's registry and returns JSON. An unavailable runtime produces an
    empty catalog so the dialog can still fall back to manual provider/model input.
    """
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    with _CATALOG_LOCK:
        if _CATALOG is None:
            _CATALOG = _load_catalog()
        return _CATALOG


def local_target(
    config: Path | None = None,
    cwd: str | None = None,
    *,
    api_key_ref: str | None = None,
) -> ConnectionTarget:
    try:
        settings = LocalSettings(**({"config_file": config} if config is not None else {}))
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
        raise HarnessClientError(str(exc)) from exc
    if shutil.which("harness-local") is None:
        raise HarnessClientError(
            "Local mode requires harness-runtime in PATH. Install harness-runtime[openai] "
            "for DeepSeek and OpenAI-compatible models."
        )
    path = settings.config_file.expanduser().absolute() if settings.config_file else None
    env = {name: f"env:{name}" for name in os.environ}
    if api_key_ref is not None:
        env["HARNESS_LOCAL_API_KEY"] = api_key_ref
    profile = Profile(
        name=f"{settings.provider}:{settings.model}",
        command="harness-local",
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
