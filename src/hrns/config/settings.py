"""Local agent configuration shared by the independent client and runtime.

Providers are named entries: ``[providers.<name>]`` holds how to reach one provider, and
``provider``/``model`` at the top level say which entry and model are active. Two entries
may use the same Pydantic AI provider id, so ``openai-work`` and ``openai-home`` can hold
different endpoints and keys.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import tomli_w
from platformdirs import user_config_path, user_data_path
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource

from .mcp import McpSettings

#: Unified pydantic-ai thinking levels accepted by ``ModelSettings.thinking``.
THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

#: The literal type of those levels, so settings can be assigned without narrowing.
EffortLevel = Literal["minimal", "low", "medium", "high", "xhigh"]

#: TUI/ACP spelling for "do not set thinking; use the provider default".
EFFORT_DEFAULT = "default"

#: TUI/ACP spelling for ``thinking=False`` (disable reasoning).
EFFORT_OFF = "off"

#: Provider ids passed to Pydantic AI; ``gateway/`` names carry a slash.
PROVIDER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"

#: Entry names prefix a ``name:model`` session id, so no colon and no slash.
PROVIDER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class ProviderNameError(ValueError):
    """A provider entry name that cannot prefix a session model id."""


def default_local_config_path() -> Path:
    project = Path.cwd() / "hrns.local.toml"
    return project if project.is_file() else user_config_path("hrns") / "local.toml"


def check_provider_name(name: str) -> str:
    """Return a usable entry name, or raise with the reason it is not one."""
    import re

    value = name.strip()
    if not value:
        raise ProviderNameError("A provider name is required.")
    if not re.fullmatch(PROVIDER_NAME_PATTERN, value):
        raise ProviderNameError(
            "A provider name may only contain letters, digits, dots, hyphens and "
            "underscores, and must start with a letter or digit."
        )
    return value


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


class ProviderEntry(BaseModel):
    """One configured local provider: how to reach it and which models it offers."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(
        default="",
        description=(
            "Pydantic AI provider id. Empty reuses the entry name, so an entry called "
            "deepseek reaches the deepseek provider."
        ),
    )
    base_url: str = ""
    api_key_env: str = Field(default="", pattern=r"^([A-Za-z_][A-Za-z0-9_]*)?$")
    copilot_client_id: str = Field(
        default="",
        pattern=r"^([A-Za-z0-9._-]+)?$",
        description=(
            "GitHub OAuth app used for the Copilot device login. Empty means GitHub "
            "Copilot's public client, which is the only kind GitHub grants the current "
            "model catalog to."
        ),
    )
    custom_models: list[str] = Field(
        default_factory=list,
        description="Model ids to offer besides the catalog's, for this entry only.",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra keyword arguments passed to the provider's constructor.",
    )

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        import re

        value = value.strip()
        if value and not re.fullmatch(PROVIDER_ID_PATTERN, value):
            raise ValueError("A provider id may only contain letters, digits and . _ / -")
        return value

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

    @field_validator("custom_models")
    @classmethod
    def validate_custom_models(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            model = item.strip()
            if not model:
                continue
            if ":" in model:
                raise ValueError("A custom model id must not contain ':'")
            cleaned.append(model)
        return list(dict.fromkeys(cleaned))

    def provider_id(self, name: str) -> str:
        """The Pydantic AI provider id this entry instantiates."""
        return self.type or name


class LocalSettings(BaseSettings):
    """Precedence: explicit values > HRNS_LOCAL_* > TOML > defaults.

    Provider credentials normally use the provider's own environment variables.
    ``api_key_env`` selects another variable; ``api_key`` is never saved by the UI.
    """

    model_config = SettingsConfigDict(
        env_prefix="HRNS_LOCAL_",
        extra="forbid",
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    config_file: Path | None = Field(default_factory=default_local_config_path, exclude=True)
    provider: str = Field(
        default="",
        pattern=r"^([A-Za-z0-9][A-Za-z0-9._-]*)?$",
        description="Name of the active provider entry.",
    )
    model: str = ""
    providers: dict[str, ProviderEntry] = Field(
        default_factory=dict,
        description="Named provider entries; names are unique and colon-free.",
    )
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    model_settings: dict[str, Any] = Field(default_factory=dict)
    capabilities: CapabilitiesSettings = Field(default_factory=CapabilitiesSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    store: Path = Field(default_factory=lambda: user_data_path("hrns") / "local-sessions.db")

    @field_validator("providers")
    @classmethod
    def validate_provider_names(cls, value: dict[str, ProviderEntry]) -> dict[str, ProviderEntry]:
        for name in value:
            check_provider_name(name)
        return value

    @model_validator(mode="after")
    def adopt_env_provider(self) -> LocalSettings:
        """An env-selected provider id becomes an implicit entry.

        ``HRNS_LOCAL_PROVIDER=deepseek`` with ``DEEPSEEK_API_KEY`` in the environment is
        the headless setup and carries no ``[providers]`` table, so the selection is
        promoted to an entry with no endpoint or key of its own; the provider reads its
        own variables.
        """
        if self.provider and self.provider not in self.providers:
            self.providers = {**self.providers, self.provider: ProviderEntry()}
        if self.model and ":" in self.model:
            raise ValueError("model is a bare model id; the provider lives in provider.")
        return self

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
            raise ValueError("Use api_key_env in TOML or HRNS_LOCAL_API_KEY in the environment.")
        if "config_file" in toml():
            raise ValueError(
                "Select the config file with --local-config or HRNS_LOCAL_CONFIG_FILE."
            )
        return init_settings, env_settings, toml

    # ----------------------------------------------------------- providers

    def entry(self, name: str | None = None) -> ProviderEntry | None:
        """The named entry, defaulting to the active one; ``None`` when unknown."""
        return self.providers.get(name if name is not None else self.provider)

    def require_entry(self, name: str | None = None) -> ProviderEntry:
        selected = name if name is not None else self.provider
        entry = self.providers.get(selected)
        if entry is None:
            raise ValueError(
                f"Provider {selected!r} is not configured. Add it with /providers in the "
                "terminal or under [providers.<name>] in hrns.local.toml."
            )
        return entry

    def provider_id(self, name: str | None = None) -> str:
        """The Pydantic AI provider id for an entry, or the name when it is unknown."""
        selected = name if name is not None else self.provider
        entry = self.providers.get(selected)
        return entry.provider_id(selected) if entry is not None else selected

    def provider_ids(self) -> dict[str, str]:
        """Entry name -> Pydantic AI provider id."""
        return {name: entry.provider_id(name) for name, entry in self.providers.items()}

    def models_for(
        self, name: str, catalog: Mapping[str, Sequence[str]] | None = None
    ) -> tuple[str, ...]:
        """Catalog model ids for an entry followed by the ids it adds itself."""
        entry = self.providers.get(name)
        known = tuple(catalog.get(self.provider_id(name), ())) if catalog else ()
        custom = tuple(entry.custom_models) if entry is not None else ()
        return tuple(dict.fromkeys([*known, *custom]))

    def model_targets(self, catalog: Mapping[str, Sequence[str]] | None = None) -> tuple[str, ...]:
        """Every ``name:model`` id the configured entries can serve."""
        targets = [
            f"{name}:{model}" for name in self.providers for model in self.models_for(name, catalog)
        ]
        return tuple(dict.fromkeys(targets))

    def active_target(self) -> str:
        """The ``name:model`` id of the current selection."""
        return f"{self.provider}:{self.model}"

    def require_model(self) -> None:
        if not self.provider or self.provider not in self.providers:
            raise ValueError(
                "No local provider is configured. Add one with /providers in the terminal "
                "or under [providers.<name>] in hrns.local.toml."
            )
        if not self.model:
            raise ValueError(
                "Configure a local model with /models in the terminal or set model in "
                "hrns.local.toml."
            )

    # --------------------------------------------------------- credentials

    def resolved_api_key(self, name: str | None = None) -> str | None:
        """The key for an entry, from the environment or ``HRNS_LOCAL_API_KEY``."""
        selected = name if name is not None else self.provider
        if self.api_key is not None and selected == self.provider:
            return self.api_key.get_secret_value()
        entry = self.providers.get(selected)
        if entry is not None and entry.api_key_env:
            value = os.environ.get(entry.api_key_env)
            if not value:
                raise ValueError(f"API key environment variable {entry.api_key_env} is not set.")
            return value
        return None

    def save(self, path: Path | None = None, *, exclude_defaults: bool = False) -> Path:
        """Atomically save non-secret settings; do not change the existing file on failure."""
        target = (path or self.config_file or default_local_config_path()).expanduser().absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = _compact(self.model_dump(mode="json", exclude_defaults=exclude_defaults))
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(tomli_w.dumps(payload).encode())
                stream.close()
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return target


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Leave unset provider fields out so the file reads as if written by hand."""
    providers = payload.get("providers")
    if isinstance(providers, dict):
        for entry in providers.values():
            if isinstance(entry, dict):
                for key in [key for key, value in entry.items() if value in ("", [], {})]:
                    del entry[key]
    return payload


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
