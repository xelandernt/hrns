"""The one configuration document shared by the terminal client and the local runtime.

Both programs read the same file. Sections name their owner: ``[profile.<name>]`` and
``[tui]`` belong to the client, while ``provider``/``model``, ``[providers.<name>]``,
``[model_settings]``, ``[mcp]`` and ``store`` belong to the runtime.
A single pydantic-settings model parses the whole document, so a typo in any section
fails the same way wherever it is read.

Precedence, highest first: constructor arguments, ``HRNS_LOCAL_*`` environment
variables, the project file ``./hrns.toml``, the user file ``config.toml`` in the
platform config directory, then defaults. Files merge key by key: a project file only
overrides the entries and values it names, so a repository can add one profile or
provider without copying the user's document. ``--config PATH`` (or
``HRNS_LOCAL_CONFIG_FILE``) selects a single file and disables that merge.

Providers are named entries: ``[providers.<name>]`` holds how to reach one provider, and
``provider``/``model`` at the top level say which entry and model are active. Two entries
may use the same Pydantic AI provider id, so ``openai-work`` and ``openai-home`` can hold
different endpoints and keys.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import tomllib
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

#: The user document, in the platform config directory.
CONFIG_FILE_NAME = "config.toml"

#: The optional project document, read from the working directory.
PROJECT_CONFIG_NAME = "hrns.toml"

#: Environment variable that selects one document instead of the merge.
CONFIG_ENV_VAR = "HRNS_LOCAL_CONFIG_FILE"

#: What this terminal offers the agent: filesystem reads, writes, and commands.
ClientIoMode = Literal["off", "read", "all"]

#: How tool approvals are answered without asking.
PermissionMode = Literal["ask", "reads", "all"]

#: Status-bar labels for the permission modes; the stored value is the mode itself.
PERMISSION_MODES: tuple[PermissionMode, ...] = ("ask", "reads", "all")

#: Identity of the built-in local runtime; every other identity is a profile name.
LOCAL_CONNECTION = "local"


class ProviderNameError(ValueError):
    """A provider entry name that cannot prefix a session model id."""


def default_config_path() -> Path:
    """The user document: every setting in one file, in the platform config directory."""
    return user_config_path("hrns") / CONFIG_FILE_NAME


def project_config_path() -> Path:
    """The document a working directory may add, overriding the user's key by key."""
    return Path.cwd() / PROJECT_CONFIG_NAME


def config_files() -> tuple[Path, ...]:
    """The documents a default load reads, highest priority first."""
    return tuple(path for path in (project_config_path(), default_config_path()) if path.is_file())


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


class Profile(BaseModel):
    """One saved connection: how to start or reach an ACP agent.

    ``stdio`` profiles launch ``command``/``args`` as a subprocess; ``server`` profiles
    connect to ``url``. Fields the transport does not use are ignored, so switching a
    profile between the two keeps its text intact.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    #: The ``[profile.<name>]`` key; TOML owns it, so it is never written as a field.
    name: str = Field(default="", exclude=True)

    transport: Literal["stdio", "server"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    agent: str = ""
    token: str = ""
    remote_cwd: str = ""
    auth_method: str = ""
    client_io: ClientIoMode | None = None
    default: bool = False
    notes: str = ""

    @property
    def is_remote(self) -> bool:
        return self.transport == "server"

    def describe_location(self) -> str:
        if self.is_remote:
            return f"{self.url or 'no url'}" + (f" · {self.agent}" if self.agent else "")
        command = " ".join([self.command, *self.args]) if self.command else "(command unspecified)"
        where = f" in {self.cwd}" if self.cwd else ""
        return f"{command}{where}"


class TuiSettings(BaseModel):
    """Terminal preferences; the client owns this section and saves it as it changes."""

    model_config = ConfigDict(extra="forbid")

    theme: str = "hrns"
    auto_expand_tools: bool = False
    show_thoughts: bool = False
    permission_mode: PermissionMode = "ask"
    client_io: ClientIoMode = "all"
    sidebar_width: int = Field(default=52, ge=32, le=76)
    last_connection: str = ""


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


class HrnsConfig(BaseSettings):
    """The whole document: client sections and runtime sections, one schema.

    Environment overrides use the ``HRNS_LOCAL_`` prefix (``HRNS_LOCAL_PROVIDER``,
    ``HRNS_LOCAL_MODEL``, ``HRNS_LOCAL_CONFIG_FILE``), which is what the runtime has
    always read. ``HRNS_MODEL`` and ``HRNS_STORE`` belong to the other entry points and
    are deliberately not read here.
    """

    model_config = SettingsConfigDict(
        env_prefix="HRNS_LOCAL_",
        env_nested_delimiter="__",
        extra="forbid",
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    #: The document to read and write; ``None`` on an instance means "env only".
    config_file: Path | None = Field(default=None, exclude=True)

    # ------------------------------------------------------------- client

    profile: dict[str, Profile] = Field(
        default_factory=dict,
        description="Saved connections, each named by its table key.",
    )
    tui: TuiSettings = Field(
        default_factory=TuiSettings,
        description="Terminal preferences.",
    )

    # ------------------------------------------------------------ runtime

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
    mcp: McpSettings = Field(default_factory=McpSettings)
    store: Path = Field(default_factory=lambda: user_data_path("hrns") / "local-sessions.db")

    @field_validator("providers")
    @classmethod
    def validate_provider_names(cls, value: dict[str, ProviderEntry]) -> dict[str, ProviderEntry]:
        for name in value:
            check_provider_name(name)
        return value

    @field_validator("profile")
    @classmethod
    def validate_profile_names(cls, value: dict[str, Profile]) -> dict[str, Profile]:
        """The table key is the profile's name; the field mirrors it for callers."""
        for name, profile in value.items():
            if not name.strip():
                raise ValueError("A profile name cannot be empty")
            profile.name = name
        return value

    @model_validator(mode="after")
    def adopt_env_provider(self) -> HrnsConfig:
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

    # ------------------------------------------------------------- loading

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        init_kwargs = init_settings.init_kwargs
        if "config_file" in init_kwargs and init_kwargs["config_file"] is None:
            # ``hrns-local --env-only``: no document at all.
            return init_settings, env_settings
        explicit = init_kwargs.get("config_file") or env_settings().get("config_file")
        if explicit:
            # A selected file is a contract: typos must not silently use defaults.
            path = Path(explicit).expanduser()
            if not path.is_file():
                raise ValueError(f"Configuration file does not exist: {path}")
            paths = [path]
        else:
            # Later sources are read first; both files deep-merge, project over user.
            paths = [project_config_path(), default_config_path()]
        sources: list[TomlConfigSettingsSource] = []
        for path in paths:
            if not path.is_file():
                continue
            source = TomlConfigSettingsSource(settings_cls, path)
            payload = source()
            if "config_file" in payload:
                raise ValueError(
                    f"{path}: select the document with --config or {CONFIG_ENV_VAR}, "
                    "not with a config_file key."
                )
            if "api_key" in payload:
                raise ValueError(
                    f"{path}: use api_key_env in the document, or HRNS_LOCAL_API_KEY in "
                    "the environment."
                )
            sources.append(source)
        return (init_settings, env_settings, *sources)

    # ------------------------------------------------------------- saving

    @property
    def path(self) -> Path:
        """The file ``save()`` writes: the selected one, else the user document."""
        return (self.config_file or default_config_path()).expanduser().absolute()

    def save(self, path: Path | None = None) -> Path:
        """Write the document atomically, leaving defaults and secrets out.

        ``path`` defaults to the file this instance was read from, then to the user
        document. Values equal to their defaults are omitted, so the file reads like one
        written by hand and a future default change reaches existing files.
        """
        target = path.expanduser().absolute() if path is not None else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(
            mode="json",
            exclude={"config_file", "api_key"},
            exclude_defaults=True,
        )
        text = tomli_w.dumps(payload)
        tomllib.loads(text)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(text.encode())
                stream.close()
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    # ---------------------------------------------------------- providers

    def entry(self, name: str | None = None) -> ProviderEntry | None:
        """The named entry, defaulting to the active one; ``None`` when unknown."""
        return self.providers.get(name if name is not None else self.provider)

    def require_entry(self, name: str | None = None) -> ProviderEntry:
        selected = name if name is not None else self.provider
        entry = self.providers.get(selected)
        if entry is None:
            raise ValueError(
                f"Provider {selected!r} is not configured. Add it with /providers in the "
                f"terminal or under [providers.<name>] in {CONFIG_FILE_NAME}."
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
                f"or under [providers.<name>] in {CONFIG_FILE_NAME}."
            )
        if not self.model:
            raise ValueError(
                "Configure a local model with /models in the terminal or set model in "
                f"{CONFIG_FILE_NAME}."
            )

    # ------------------------------------------------------------ profiles

    def get_profile(self, name: str) -> Profile:
        try:
            return self.profile[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.profile)) or "(none saved)"
            raise KeyError(f"No profile named '{name}'. Saved profiles: {available}") from exc

    def default_profile(self) -> Profile | None:
        """The profile marked default, or the only one; ``None`` when ambiguous."""
        for profile in self.profile.values():
            if profile.default:
                return profile
        if len(self.profile) == 1:
            return next(iter(self.profile.values()))
        return None

    @property
    def last_connection(self) -> str:
        """Identity of the connection the terminal reopened last, if any."""
        return self.tui.last_connection

    def connection_identity(self, profile: Profile) -> str | None:
        """How the terminal remembers a connection: a profile name or ``local``.

        An ad-hoc profile assembled on the command line belongs to no file, so it is
        not remembered at all; there would be nothing to reopen.
        """
        if profile.name in self.profile:
            return profile.name
        return LOCAL_CONNECTION if is_local_hrns(profile) else None

    def remember_connection(self, identity: str | None) -> None:
        """Persist the connection to reopen next time; a preference never costs a session."""
        if not identity or identity == self.last_connection:
            return
        self.tui.last_connection = identity
        with contextlib.suppress(Exception):
            self.save()

    def forget_connection(self) -> None:
        """Drop the remembered connection, so a stale identity stops failing at launch."""
        if not self.tui.last_connection:
            return
        self.tui.last_connection = ""
        with contextlib.suppress(Exception):
            self.save()

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


def is_local_hrns(profile: Profile | None) -> bool:
    """Whether a profile launches this installation's ``hrns-local`` runtime.

    The command is stored as the resolved path, so compare the file name rather
    than the profile text.
    """
    return profile is not None and Path(profile.command).name == "hrns-local"


def thinking_from_settings(settings: HrnsConfig) -> str | None:
    """The configured effort as a TUI/ACP value, or ``None`` for the default."""
    value = settings.model_settings.get("thinking")
    if value is False:
        return EFFORT_OFF
    if isinstance(value, str) and value in THINKING_LEVELS:
        return value
    # ``True`` and an unset value both mean "provider default".
    return None


def with_thinking(settings: HrnsConfig, effort: str | None) -> HrnsConfig:
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
