"""Connection profiles: TOML storage, secret references, CLI overrides.

Profiles live in the platform user config directory; an optional project
file (``hrns.toml``) contributes more. Secrets are *references*—never
inline values:

* ``env:NAME`` resolves from the process environment.
* ``keyring:service[/username]`` resolves through the OS keyring.

Anything else is a literal value, which keeps third-party agent commands
portable (``PATH`` is usually not a secret).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from .constants import (
    CONFIG_DIR_NAME,
    CONFIG_ENV_OVERRIDE,
    CONFIG_FILE_NAME,
    PROJECT_CONFIG_NAME,
)
from .errors import SecretMissing


@dataclass(slots=True)
class Profile:
    """Instructions for launching or connecting to an ACP agent."""

    name: str
    transport: str = "stdio"  # "stdio" | "server"
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)  # env name -> secret ref or literal
    url: str = ""  # https(s) base url for "server" profiles
    agent: str = ""  # default agent id/name for "server" profiles
    token: str = ""  # secret reference for bearer token
    remote_cwd: str = ""  # bridge rewrite target
    auth_method: str = ""  # agent-advertised auth method id to try automatically
    default_for_server: bool = False
    notes: str = ""

    @property
    def is_remote(self) -> bool:
        return self.transport == "server"

    def describe_location(self) -> str:
        if self.is_remote:
            target = self.agent or "(agent unspecified)"
            return f"{self.url or self.name} · {target}"
        command = " ".join([self.command, *self.args]) if self.command else "(command unspecified)"
        where = f" in {self.cwd}" if self.cwd else ""
        return f"{command}{where}"

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {"transport": self.transport}
        if self.command:
            data["command"] = self.command
        if self.args:
            data["args"] = list(self.args)
        if self.cwd:
            data["cwd"] = self.cwd
        if self.env:
            data["env"] = dict(self.env)
        if self.url:
            data["url"] = self.url
        if self.agent:
            data["agent"] = self.agent
        if self.token:
            data["token"] = self.token
        if self.remote_cwd:
            data["remoteCwd"] = self.remote_cwd
        if self.auth_method:
            data["authMethod"] = self.auth_method
        if self.default_for_server:
            data["default"] = True
        if self.notes:
            data["notes"] = self.notes
        return data

    @classmethod
    def from_wire(cls, name: str, data: dict[str, Any]) -> Profile:
        known = {
            "transport",
            "command",
            "args",
            "cwd",
            "env",
            "url",
            "agent",
            "token",
            "remoteCwd",
            "authMethod",
            "default",
            "notes",
        }
        unknown = set(data) - known - {"name"}
        if unknown:
            raise ValueError(f"Profile '{name}' has unknown keys: {', '.join(sorted(unknown))}")
        transport = str(data.get("transport", "stdio"))
        if transport not in {"stdio", "server"}:
            raise ValueError(f"Profile '{name}': transport must be 'stdio' or 'server'")
        args = [str(item) for item in data.get("args", [])]
        env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
        return cls(
            name=name,
            transport=transport,
            command=str(data.get("command", "")),
            args=args,
            cwd=str(data.get("cwd", "")),
            env=env,
            url=str(data.get("url", "")),
            agent=str(data.get("agent", "")),
            token=str(data.get("token", "")),
            remote_cwd=str(data.get("remoteCwd", "")),
            auth_method=str(data.get("authMethod", "")),
            default_for_server=bool(data.get("default", False)),
            notes=str(data.get("notes", "")),
        )


def toml_from_profiles(profiles: dict[str, Profile], tui: dict[str, Any] | None = None) -> str:
    """Render profiles (and optional ``tui`` section) as TOML text.

    ``tomli_w`` owns quoting and escaping, so names such as ``a"b`` or ``café`` round-trip
    exactly instead of being silently rewritten or producing an invalid document.
    """
    for name in profiles:
        if not name:
            raise ValueError("Profile name cannot be empty")
    document: dict[str, Any] = {
        "profile": {name: profile.to_wire() for name, profile in profiles.items()}
    }
    if tui:
        document["tui"] = tui
    return tomli_w.dumps(document)


def profiles_from_toml(text: str) -> tuple[dict[str, Profile], dict[str, Any]]:
    """Parse a profiles TOML document. Unknown top keys are preserved for round-trip."""
    data = tomllib.loads(text)
    profiles: dict[str, Profile] = {}
    section = data.get("profile") or data.get("profiles") or {}
    if not isinstance(section, dict):
        raise ValueError("The 'profile' section must be a table")
    for name, raw in section.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Profile '{name}' must be a table")
        profiles[name] = Profile.from_wire(str(name), raw)
    rest = {k: v for k, v in data.items() if k not in {"profile", "profiles"}}
    return profiles, rest


class Profiles:
    """Load, save and merge profile documents."""

    def __init__(
        self,
        profiles: dict[str, Profile],
        tui: dict[str, Any] | None = None,
        sources: list[Path] | None = None,
    ):
        self.profiles = profiles
        self.tui = tui or {}
        self.sources: list[Path] = sources or []

    def get(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.profiles)) or "(none saved)"
            raise KeyError(f"No profile named '{name}'. Saved profiles: {available}") from exc

    @property
    def names(self) -> list[str]:
        return list(self.profiles)

    def default_profile(self) -> Profile | None:
        for profile in self.profiles.values():
            if profile.default_for_server:
                return profile
        if len(self.profiles) == 1:
            return next(iter(self.profiles.values()))
        return None

    def save(self, path: Path | None = None) -> Path | None:
        """Persist *profiles and tui settings* atomically to ``path``."""
        target = path
        if target is None:
            if not self.sources:
                return None
            target = self.sources[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        text = toml_from_profiles(self.profiles, self.tui)
        # Refuse to replace the user's file with a document we cannot read back.
        tomllib.loads(text)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return target

    def save_tui(self, path: Path | None = None) -> Path:
        """Persist global TUI preferences without copying project profiles."""
        configured = os.environ.get(CONFIG_ENV_OVERRIDE)
        target = path or (Path(configured) if configured else default_profiles_path())
        target = target.expanduser().absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        document: dict[str, Any]
        if target.is_file():
            document = tomllib.loads(target.read_text(encoding="utf-8"))
        else:
            document = {}
        document["tui"] = dict(self.tui)
        text = tomli_w.dumps(document)
        tomllib.loads(text)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return target

    @classmethod
    def load(
        cls,
        *,
        extra_files: list[Path] | None = None,
        include_defaults: bool = True,
    ) -> Profiles:
        """Load the user config file plus any explicitly provided project files."""
        sources: list[Path] = []
        profile_map: dict[str, Profile] = {}
        tui: dict[str, Any] = {}

        user_file = default_profiles_path()
        candidates: list[Path] = []
        env_override = os.environ.get(CONFIG_ENV_OVERRIDE)
        if env_override:
            candidates.append(Path(env_override))
        elif user_file.exists():
            candidates.append(user_file)
        if extra_files:
            candidates.extend(path for path in extra_files if path.exists())
        elif include_defaults:
            project_file = Path.cwd() / PROJECT_CONFIG_NAME
            if project_file != user_file and project_file.exists():
                candidates.append(project_file)

        for path in candidates:
            try:
                profiles, rest = profiles_from_toml(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            profile_map.update(profiles)
            tui.update(rest.get("tui") or {})
            sources.append(path)
            others = {k: v for k, v in rest.items() if k != "tui"}
            if others:
                tui["ignored"] = others

        return Profiles(profile_map, tui, sources)

    def upsert(self, profile: Profile, *, set_default: bool = False) -> None:
        """Add or replace a profile; optionally flag it as the default."""
        if set_default:
            for existing in self.profiles.values():
                existing.default_for_server = False
            profile.default_for_server = True
        self.profiles[profile.name] = profile
        return None


def default_profiles_path() -> Path:
    """User config directory path for profile storage."""
    try:
        from platformdirs import user_config_path

        return user_config_path(CONFIG_DIR_NAME) / CONFIG_FILE_NAME
    except Exception:  # pragma: no cover - platformdirs is a hard dependency
        home = Path.home() / ".config"
        return home / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def secret_value(reference: str, *, env: dict[str, str] | None = None) -> str | None:
    """Resolve ``env:`` and ``keyring:`` references; literals pass through unchanged."""
    if not reference:
        return None
    if reference.startswith("env:"):
        name = reference[4:]
        if not name:
            raise SecretMissing(reference, "no environment variable named")
        value = (env if env is not None else os.environ).get(name)
        if value is None:
            raise SecretMissing(reference, f"environment variable '{name}' is not set")
        return value
    if reference.startswith("keyring:"):
        remainder = reference[len("keyring:") :]
        if not remainder:
            raise SecretMissing(reference, "no service named")
        service, _, username = remainder.partition("/")
        if username == "":
            username = "default"
        try:
            import keyring
        except Exception as exc:  # pragma: no cover - keyring is generally available
            raise SecretMissing(reference, f"keyring backend unavailable ({exc})") from exc
        try:
            value = keyring.get_password(service, username)
        except Exception as exc:
            raise SecretMissing(reference, f"keyring lookup failed ({exc})") from exc
        if value is None:
            raise SecretMissing(reference, f"keyring has no entry '{service}' for '{username}'")
        return value
    return reference
