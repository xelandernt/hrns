"""Local agent configuration shared by the independent client and runtime."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomli_w
from platformdirs import user_config_path, user_data_path
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource

from .mcp import McpSettings

#: Unified pydantic-ai thinking levels accepted by ``ModelSettings.thinking``.
THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

#: TUI/ACP spelling for "do not set thinking; use the provider default".
EFFORT_DEFAULT = "default"

#: TUI/ACP spelling for ``thinking=False`` (disable reasoning).
EFFORT_OFF = "off"


def default_local_config_path() -> Path:
    project = Path.cwd() / "harness.local.toml"
    return project if project.is_file() else user_config_path("harness") / "local.toml"


class CapabilitiesSettings(BaseModel):
    """Which interactive capabilities the local definition binds."""

    model_config = ConfigDict(extra="forbid")

    ask_user: bool = Field(
        default=True,
        description=(
            "Bind the ask_user capability so the agent can ask structured questions. "
            "Turn it off for headless runs, where nobody is there to answer."
        ),
    )


class LocalSettings(BaseSettings):
    """Precedence: explicit values > HARNESS_LOCAL_* > TOML > defaults.

    Provider credentials normally use the provider's own environment variables.
    ``api_key_env`` selects another variable; ``api_key`` is never saved by the UI.
    """

    model_config = SettingsConfigDict(
        env_prefix="HARNESS_LOCAL_",
        extra="forbid",
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    config_file: Path | None = Field(default_factory=default_local_config_path, exclude=True)
    provider: str = Field(default="deepseek", min_length=1)
    model: str = ""
    base_url: str = ""
    api_key_env: str = Field(default="", pattern=r"^([A-Za-z_][A-Za-z0-9_]*)?$")
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    provider_options: dict[str, Any] = Field(default_factory=dict)
    model_settings: dict[str, Any] = Field(default_factory=dict)
    capabilities: CapabilitiesSettings = Field(default_factory=CapabilitiesSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    store: Path = Field(default_factory=lambda: user_data_path("harness") / "local-sessions.db")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if value:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("base_url must be an http(s) URL")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("base_url must not contain credentials, query, or fragment")
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        if (
            "config_file" in init_settings.init_kwargs
            and init_settings.init_kwargs["config_file"] is None
        ):
            return init_settings, env_settings
        environment = env_settings()
        path = init_settings.init_kwargs.get("config_file") or environment.get("config_file")
        path = Path(path).expanduser() if path else default_local_config_path()
        # A selected file is a contract: typos must not silently use defaults.
        if (
            "config_file" in init_settings.init_kwargs or "config_file" in environment
        ) and not path.is_file():
            raise ValueError(f"Local configuration file does not exist: {path}")
        toml = TomlConfigSettingsSource(settings_cls, path)
        if "api_key" in toml():
            raise ValueError("Use api_key_env in TOML or HARNESS_LOCAL_API_KEY in the environment.")
        if "config_file" in toml():
            raise ValueError(
                "Select the config file with --local-config or HARNESS_LOCAL_CONFIG_FILE."
            )
        return init_settings, env_settings, toml

    def require_model(self) -> None:
        if not self.model:
            raise ValueError(
                "Configure a local model in the UI or set model in harness.local.toml."
            )

    def resolved_api_key(self) -> str | None:
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if not value:
                raise ValueError(f"API key environment variable {self.api_key_env} is not set.")
            return value
        return None

    def save(self, path: Path | None = None, *, exclude_defaults: bool = False) -> Path:
        """Atomically save non-secret settings; do not change the existing file on failure."""
        target = (path or self.config_file or default_local_config_path()).expanduser().absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json", exclude_defaults=exclude_defaults)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(tomli_w.dumps(payload).encode())
                stream.close()
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return target


def thinking_from_settings(settings: LocalSettings) -> str | None:
    """The configured effort as a TUI/ACP value, or ``None`` for the default."""
    value = settings.model_settings.get("thinking")
    if value is False:
        return EFFORT_OFF
    if isinstance(value, str) and value in THINKING_LEVELS:
        return value
    # ``True`` and an unset value both mean "provider default".
    return None


def with_thinking(settings: LocalSettings, effort: str | None) -> LocalSettings:
    """Return a copy whose ``model_settings.thinking`` reflects ``effort``.

    Only the ``thinking`` key is touched so other model settings survive.
    """
    model_settings = dict(settings.model_settings)
    if effort in (None, "", EFFORT_DEFAULT):
        model_settings.pop("thinking", None)
    elif effort == EFFORT_OFF:
        model_settings["thinking"] = False
    elif effort in THINKING_LEVELS:
        model_settings["thinking"] = effort
    else:
        raise ValueError(f"Unknown reasoning effort: {effort!r}")
    return settings.model_copy(update={"model_settings": model_settings})
