"""Built-in ACP entry point backed by the configured Pydantic AI model."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from functools import lru_cache, partial
from pathlib import Path
from typing import Annotated, Literal

from acp import schema
from cyclopts import App, Parameter
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.exceptions import ModelAPIError, UserError
from pydantic_ai.models import infer_model, infer_model_profile, known_model_names
from pydantic_ai.models.github_copilot import GitHubCopilotModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.providers.github_copilot import GitHubCopilotProvider
from pydantic_ai.providers.openai_codex import OpenAICodexProvider
from pydantic_ai.settings import ModelSettings

from hrns.shared.config import (
    EFFORT_DEFAULT,
    EFFORT_OFF,
    HrnsConfig,
    ProviderEntry,
)
from hrns.shared.config.credentials import delete_secret, stored_secret
from hrns.shared.config.settings import (
    check_provider_name,
    write_config_document,
)

from ..local_server import configuration_id, load_settings
from .acp import (
    AcpAttachmentConfig,
    AcpServer,
    AcpSession,
    ModelChoice,
    ProviderConfig,
    ProviderDescriptor,
    ProviderDisabled,
    ProviderModelResolver,
    ProviderRoute,
    ResolvedModel,
    SessionContext,
    SettingChoice,
    SettingOption,
    acp_mcp_toolsets,
    run_acp_stdio,
)
from .acp.models import ModelSetupError
from .acp.store import SessionStoreError
from .agent import build_agent
from .copilot_pricing import CopilotCarbonModel
from .local_sessions import LocalSessionStore
from .provider_login import LoginMethod, ProviderLogin, codex_source, login_account, login_methods
from .reasoning import native_efforts, openai_efforts

__all__ = ["RuntimeModelResolver", "main"]

logger = logging.getLogger(__name__)
_COPILOT_URL = "https://api.githubcopilot.com"
_COPILOT_VERSION_HEADER = "X-GitHub-Api-Version"
_COPILOT_DEFAULT_VERSION = "2025-04-01"
_COPILOT_EXTENDED_VERSION = "2026-08-01"
_COPILOT_LONG_SUFFIX = "@long-context"
_CopilotEndpoint = Literal["/chat/completions", "/responses"]
# Codex catalog compatibility level, based on minimal_client_version in its bundled catalog.
_CODEX_CATALOG_CLIENT_VERSION = "0.166.0"
_LIVE_CATALOG_PROVIDERS = {"github-copilot", "openai-codex"}


def _context_size_label(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:g}M"
    if size >= 1_000:
        return f"{size / 1_000:g}K"
    return str(size)


class _CopilotLimits(BaseModel):
    max_context_window_tokens: int | None = None
    max_output_tokens: int | None = None


class _CopilotSupports(BaseModel):
    reasoning_effort: list[str] | None = None


class _CopilotCapabilities(BaseModel):
    limits: _CopilotLimits | None = None
    supports: _CopilotSupports | None = None


class _CopilotPriceTier(BaseModel):
    max_prompt_tokens: int | None = None
    context_max: int | None = None

    def prompt_limit(self) -> int:
        return self.max_prompt_tokens or self.context_max or 0


class _CopilotTokenPrices(BaseModel):
    default: _CopilotPriceTier | None = None
    long_context: _CopilotPriceTier | None = None


class _CopilotBilling(BaseModel):
    token_prices: _CopilotTokenPrices | None = None


class _CopilotModel(BaseModel):
    id: str
    supported_endpoints: list[str] = []
    capabilities: _CopilotCapabilities | None = None
    billing: _CopilotBilling | None = None

    def reasoning_efforts(self) -> Sequence[str]:
        supports = self.capabilities.supports if self.capabilities else None
        return (supports.reasoning_effort or ()) if supports else ()

    def tier_prompt_limits(self) -> tuple[int, int] | None:
        prices = self.billing.token_prices if self.billing else None
        if prices and prices.default and prices.long_context:
            default, extended = prices.default.prompt_limit(), prices.long_context.prompt_limit()
            if 0 < default < extended:
                return default, extended
        return None

    def context_window(self, *, extended: bool = False) -> int | None:
        limits = self.capabilities.limits if self.capabilities else None
        size = limits.max_context_window_tokens if limits else None
        if tiers := self.tier_prompt_limits():
            default_prompt, extended_prompt = tiers
            prompt = extended_prompt if extended else default_prompt
            output = max(limits.max_output_tokens or 0, 0) if limits else 0
            tier_window = prompt + output
            return min(tier_window, size) if size and size > 0 else tier_window
        return size if size is not None and size > 0 else None

    def endpoint(self) -> _CopilotEndpoint | None:
        if "/chat/completions" in self.supported_endpoints:
            return "/chat/completions"
        if "/responses" in self.supported_endpoints:
            return "/responses"
        return None


class _CopilotCatalog(BaseModel):
    data: list[_CopilotModel]


class _CodexCatalogModel(BaseModel):
    slug: str
    visibility: str
    priority: int


class _CodexCatalog(BaseModel):
    models: list[_CodexCatalogModel]


#: The ACP config option id for the reasoning-effort ladder.
_EFFORT_OPTION_ID = "effort"

#: Display names and one-line explanations, shared by the ACP option and the client.
_EFFORT_COPY: dict[str, tuple[str, str]] = {
    EFFORT_DEFAULT: ("Default", "Let the provider choose its default reasoning effort."),
    EFFORT_OFF: ("Off", "Disable reasoning when the model allows it."),
    "minimal": ("Minimal", "Fastest responses with the least reasoning."),
    "low": ("Low", "Light reasoning for straightforward work."),
    "medium": ("Medium", "Balanced reasoning for everyday work."),
    "high": ("High", "Deeper reasoning for harder work."),
    "xhigh": ("XHigh", "Extra high reasoning for complex work."),
    "max": ("Max", "Maximum reasoning depth."),
}


@lru_cache(maxsize=256)
def _profile_for(pydantic_id: str) -> ModelProfile | None:
    """The registry profile for a ``provider:model`` id, or ``None`` when it is unknown.

    ``None`` means the id is outside the pinned runtime's catalog, so the deployment's own
    policy decides what it accepts; it does not mean "cannot reason".
    """
    if pydantic_id not in known_model_names():
        return None
    try:
        return infer_model_profile(pydantic_id)
    except (ImportError, ValueError, UserError):
        return None


def _thinking_support(pydantic_id: str) -> str | None:
    """``"supported"``, ``"always"``, or ``None`` for a model the catalog knows."""
    profile = _profile_for(pydantic_id)
    if profile is None:
        return None
    if profile.get("thinking_always_enabled"):
        return "always"
    return "supported" if profile.get("supports_thinking") else None


class _ProviderEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    create: bool
    api_type: str
    base_url: str
    models: list[str] | None = None
    aws_profile: str | None = None
    aws_region: str | None = None


class _ProviderRemoval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class _ProviderSignIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str
    method: LoginMethod
    key: SecretStr | None = None


class RuntimeModelResolver(ProviderModelResolver):
    """The built-in configuration, exposed as the models and providers ACP can use.

    One provider per configured entry, named after the entry, because ACP model ids read
    ``entry:model``. Each entry supports exactly the Pydantic AI provider it is configured
    for, so a client can move its routing to its own gateway but cannot change which
    protocol the entry speaks. The entry this deployment starts with is ``required``:
    disabling it would leave the default session without a model.
    """

    def __init__(self, settings: HrnsConfig) -> None:
        active = settings.provider if settings.provider in settings.providers else None
        super().__init__(
            [
                ProviderDescriptor(
                    provider_id=name,
                    supported=(entry.provider_id(name),),
                    required=name == active,
                    current=ProviderRoute(
                        api_type=entry.provider_id(name), base_url=entry.base_url
                    ),
                )
                for name, entry in settings.providers.items()
            ]
        )
        self.settings = settings
        self.startup_model: str | None = None
        self.last_provider_models: dict[str, str] = {}
        self._copilot_models: dict[str, dict[str, _CopilotModel]] = {}
        self._codex_models: dict[str, tuple[str, ...]] = {}
        self._active_login: ProviderLogin | None = None

    async def refresh_provider_models(self, name: str) -> str | None:
        match self.settings.provider_id(name):
            case "github-copilot":
                return await self.refresh_copilot_models(name)
            case "openai-codex":
                return await self.refresh_codex_models(name)
        return None

    async def refresh_copilot_models(self, name: str) -> str | None:
        entry = self.settings.require_entry(name)
        try:
            key = await asyncio.to_thread(self._api_key, name, entry)
            provider = GitHubCopilotProvider(
                api_key=key,
                base_url=entry.base_url or (_COPILOT_URL if key else None),
            )
        except (ValueError, UserError):
            return None  # An entry can be configured before it has credentials.

        try:
            async with provider.client as client:
                raw = await client.with_options(max_retries=0, timeout=5).get(
                    "/models",
                    cast_to=object,
                    options={
                        "headers": {_COPILOT_VERSION_HEADER: _COPILOT_EXTENDED_VERSION}
                        if provider.base_url.rstrip("/") == _COPILOT_URL
                        else {}
                    },
                )
            catalog = _CopilotCatalog.model_validate(raw)
        except (OpenAIError, ValueError):
            self._copilot_models.pop(name, None)
            return f"Could not load Copilot models for {name!r}. Check your Copilot access."

        models = {
            item.id: item
            for item in catalog.data
            if item.id.strip() and item.endpoint() is not None
        }
        self._copilot_models[name] = models
        if not models:
            return f"Copilot returned no compatible models for {name!r}."
        return None

    async def refresh_codex_models(self, name: str) -> str | None:
        entry = self.settings.require_entry(name)

        def create_provider() -> OpenAICodexProvider:
            options = dict(entry.options)
            if entry.base_url:
                options["base_url"] = entry.base_url
            if (source := codex_source(name, entry)) is not None:
                options["credential_source"] = source
            return OpenAICodexProvider(**options)

        try:
            provider = await asyncio.to_thread(create_provider)
        except UserError:
            return None  # A configured entry may still need sign-in.
        except (TypeError, ValueError):
            self._codex_models.pop(name, None)
            return f"Check the Codex provider settings for {name!r}."

        try:
            async with provider.client as client:
                raw = await client.with_options(max_retries=0, timeout=5).get(
                    "/models",
                    cast_to=object,
                    options={"params": {"client_version": _CODEX_CATALOG_CLIENT_VERSION}},
                )
            catalog = _CodexCatalog.model_validate(raw)
        except (ModelAPIError, OpenAIError, ValueError):
            self._codex_models.pop(name, None)
            return f"Could not load Codex models for {name!r}. Check your Codex access."

        models = tuple(
            model.slug.strip()
            for model in sorted(catalog.models, key=lambda model: model.priority)
            if model.visibility == "list" and model.slug.strip()
        )
        self._codex_models[name] = models
        if not models:
            return f"Codex returned no selectable models for {name!r}."
        return None

    async def load_provider_catalogs(self) -> None:
        warnings = await asyncio.gather(
            *(
                self.refresh_provider_models(name)
                for name, entry in self.settings.providers.items()
                if entry.provider_id(name) in _LIVE_CATALOG_PROVIDERS
            )
        )
        for warning in warnings:
            if warning:
                logger.warning("%s", warning)

    def provider_models(self, name: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.settings.models_for(name, _known_models()),
                    *self._copilot_models.get(name, ()),
                    *self._codex_models.get(name, ()),
                )
            )
        )

    def provider_editor(self) -> Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]:
        login = ProviderLogin(refresh_models=self.refresh_provider_models)
        return partial(self.manage_provider, login=login)

    async def manage_provider(
        self, method: str, params: dict[str, object], *, login: ProviderLogin
    ) -> dict[str, object]:
        if method == "hrns/providers/list":
            return {
                "providers": [
                    {
                        "id": name,
                        "api_type": entry.provider_id(name),
                        "base_url": entry.base_url,
                        "models": entry.custom_models,
                        "aws_profile": entry.options.get("profile_name", ""),
                        "aws_region": entry.options.get("region_name", ""),
                        "login_methods": login_methods(entry.provider_id(name)),
                        "setup_message": self._setup_message(name, entry),
                    }
                    for name, entry in self.settings.providers.items()
                ],
            }
        if method == "hrns/providers/login":
            request = _ProviderSignIn.model_validate(params)
            entry = self.settings.require_entry(request.id)
            if self._active_login is not None and self._active_login.active:
                raise ValueError("Finish or cancel the current sign-in first.")
            self._active_login = login
            return await login.start(
                request.id,
                entry,
                request.method,
                request.key.get_secret_value() if request.key is not None else None,
            )
        if method == "hrns/providers/login-status":
            return await login.status()
        if method == "hrns/providers/login-cancel":
            await login.cancel()
            return {}
        if self._active_login is not None and self._active_login.active:
            raise ValueError("Finish or cancel the current sign-in before changing providers.")
        removed = self.edit_provider_document(method, params)
        if removed is not None:
            await asyncio.to_thread(delete_secret, removed)
        return {}

    def edit_provider_document(self, method: str, params: dict[str, object]) -> str | None:
        """Change a built-in stdio provider and the same entry's runtime catalog."""
        edit = _ProviderEdit.model_validate(params) if method == "hrns/providers/save" else None
        name = check_provider_name(
            edit.id if edit is not None else _ProviderRemoval.model_validate(params).id
        )
        existing = self.settings.providers.get(name)
        if edit is not None and edit.create != (existing is None):
            raise ValueError(
                "A provider with this name already exists."
                if edit.create
                else "Provider does not exist."
            )
        if edit is None and existing is None:
            raise ValueError("Provider does not exist.")
        if edit is None and os.getenv("HRNS_PROVIDER") == name:
            raise ValueError("Unset HRNS_PROVIDER before removing this provider.")

        target = self.settings.path
        document = {}
        if target.is_file():
            with target.open("rb") as stream:
                document = tomllib.load(stream)
        entries = document.get("providers", {})
        if not isinstance(entries, dict):
            raise ValueError("The providers section must be a TOML table.")
        if edit is None:
            if name not in entries and document.get("provider") != name:
                raise ValueError("This provider is configured outside the local TOML file.")
            entries.pop(name, None)
            if document.get("provider") == name:
                document.pop("provider", None)
                document.pop("model", None)
            write_config_document(target, document)
            removed = self.settings.providers.pop(name)
            if self.settings.provider == name:
                file = self.settings.config_file
                effective = HrnsConfig(config_file=file) if file is not None else HrnsConfig()
                self.settings.provider = effective.provider
                self.settings.model = effective.model
                if descriptor := self._providers.get(effective.provider):
                    self._providers[effective.provider] = replace(descriptor, required=True)
            self._providers.pop(name, None)
            self._configs.pop(name, None)
            self._copilot_models.pop(name, None)
            self._codex_models.pop(name, None)
            return login_account(name, removed)

        api_type = edit.api_type.strip()
        fields: dict[str, object] = {"type": api_type, "base_url": edit.base_url}
        if edit.models is not None:
            fields["custom_models"] = edit.models
        previous_type = existing.provider_id(name) if existing is not None else ""
        provider_type = api_type or name
        if previous_type and previous_type != provider_type:
            fields.update(options={}, api_key_env="", copilot_client_id="")
        if provider_type == "bedrock":
            options = (
                dict(existing.options)
                if existing is not None and previous_type == provider_type
                else {}
            )
            for field, value in (
                ("profile_name", edit.aws_profile),
                ("region_name", edit.aws_region),
            ):
                if value is not None:
                    if value.strip():
                        options[field] = value.strip()
                    else:
                        options.pop(field, None)
            fields["options"] = options
        if provider_type == "openai-compatible" and not edit.base_url:
            raise ValueError("An OpenAI-compatible provider needs a base URL.")
        try:
            entry = ProviderEntry.model_validate(
                {**(existing or ProviderEntry()).model_dump(), **fields}
            )
        except ValidationError as exc:
            reason = "; ".join(error["msg"] for error in exc.errors(include_input=False))
            raise ValueError(reason) from exc
        try:
            infer_provider_class(self._inferred_type(entry.provider_id(name)))
        except (ImportError, ValueError, UserError) as exc:
            raise ValueError(
                f"Provider type {entry.provider_id(name)!r} is unavailable in this environment."
            ) from exc
        stored = entries.get(name, {})
        if not isinstance(stored, dict):
            raise ValueError("The provider entry must be a TOML table.")
        stored.update({key: getattr(entry, key) for key in fields})
        entries[name] = stored
        document["providers"] = entries
        write_config_document(target, document)
        self.settings.providers[name] = entry
        if existing is not None and (
            previous_type != entry.provider_id(name) or existing.base_url != entry.base_url
        ):
            self._copilot_models.pop(name, None)
            self._codex_models.pop(name, None)
        api_type = entry.provider_id(name)
        self._providers[name] = ProviderDescriptor(
            provider_id=name,
            supported=(api_type,),
            required=name == self.settings.provider,
            current=ProviderRoute(api_type=api_type, base_url=entry.base_url),
        )
        self._configs.pop(name, None)
        return None

    @staticmethod
    def _inferred_type(provider: str) -> str:
        return "openai" if provider == "openai-compatible" else provider

    def _api_key(self, name: str, entry: ProviderEntry) -> str | None:
        provider = entry.provider_id(name)
        if provider in {"bedrock", "openai-codex"}:
            return None
        return stored_secret(login_account(name, entry)) or self.settings.resolved_api_key(name)

    def _setup_message(self, name: str, entry: ProviderEntry) -> str | None:
        provider = entry.provider_id(name)
        try:
            key = self._api_key(name, entry)
        except ValueError as exc:
            return str(exc)
        env_vars = {
            "deepseek": ("DEEPSEEK_API_KEY",),
            "openai": ("OPENAI_API_KEY",),
            "github-copilot": (
                "GITHUB_COPILOT_API_KEY",
                "GITHUB_COPILOT_API_TOKEN",
                "COPILOT_GITHUB_TOKEN",
            ),
        }
        if provider in {*env_vars, "openai-compatible"} and not (
            key or any(os.getenv(var) for var in env_vars.get(provider, ()))
        ):
            return f"Select {name!r} in /providers and run /login."
        return None

    def list_models(self) -> Sequence[ModelChoice]:
        """Every ``entry:model`` target, with the active one first as the default."""
        targets = [
            f"{name}:{model}{suffix}"
            for name in self.settings.providers
            for model in self.provider_models(name)
            for suffix in self._context_suffixes(name, model)
        ]
        configured = self.settings.active_target()
        if configured not in targets:
            targets.insert(0, configured)
        active = self.startup_model if self.startup_model in targets else configured
        ordered = [active, *(target for target in targets if target != active)]
        preferred: dict[str, str] = {}
        for target in (configured, *targets):
            name, _, model_name = target.partition(":")
            if model_name:
                preferred.setdefault(name, target)
        available = set(targets)
        preferred.update(
            (name, target)
            for name, target in self.last_provider_models.items()
            if target in available
        )
        choices: list[ModelChoice] = []
        setup_messages: dict[str, str | None] = {}
        for target in ordered:
            name, separator, model_name = target.partition(":")
            if not separator or not model_name or name not in self.settings.providers:
                continue
            if name not in setup_messages:
                setup_messages[name] = self._setup_message(name, self.settings.providers[name])
            base_model, extended = self._context_selection(model_name)
            catalog_model = self._local_copilot_model(name, base_model)
            tiers = catalog_model.tier_prompt_limits() if catalog_model is not None else None
            group = label = None
            if tiers and catalog_model is not None:
                group = f"{name}:{base_model}"
                size = (
                    (catalog_model.context_window(extended=True) or tiers[1])
                    if extended
                    else tiers[0]
                )
                label = _context_size_label(size)
            choices.append(
                ModelChoice(
                    model_id=target,
                    provider_id=name,
                    name=f"{group} ({label})" if group else None,
                    setup_message=setup_messages[name],
                    preferred_for_provider=target == preferred.get(name),
                    context_group=group,
                    context_size=label,
                )
            )
        return choices

    def _context_suffixes(self, name: str, model_name: str) -> tuple[str, ...]:
        model = self._local_copilot_model(name, model_name)
        return ("", _COPILOT_LONG_SUFFIX) if model and model.tier_prompt_limits() else ("",)

    def _local_copilot_model(self, name: str, model_name: str) -> _CopilotModel | None:
        model = self._copilot_models.get(name, {}).get(model_name)
        if model is None:
            return None
        try:
            return model if self.current_config(name) is None else None
        except ProviderDisabled:
            return None

    @staticmethod
    def _context_selection(model_name: str) -> tuple[str, bool]:
        extended = model_name.endswith(_COPILOT_LONG_SUFFIX)
        return model_name.removesuffix(_COPILOT_LONG_SUFFIX), extended

    def build_model(self, model_id: str, config: ProviderConfig | None) -> ResolvedModel:
        """Build one ``entry:model`` selection through the entry's provider.

        The client's routing takes precedence over the entry's own ``base_url``: the base
        URL goes to the provider, while the headers ride along as ``extra_headers`` model
        settings because pydantic-ai applies request headers per run.
        """
        name, entry, model_name = self._split(model_id)

        if config is None:
            provider = entry.provider_id(name)
            base_url = entry.base_url
            headers: dict[str, str] = {}
        else:
            provider = config.route.api_type
            base_url = config.route.base_url
            headers = dict(config.headers)

        extended = False
        if config is None and provider == "github-copilot":
            model_name, extended = self._context_selection(model_name)

        options = dict(entry.options)
        if base_url:
            options["base_url"] = base_url
        if config is None:
            key = self._api_key(name, entry)
            if key is not None:
                options["api_key"] = key
            if provider == "github-copilot" and not base_url and key:
                options["base_url"] = _COPILOT_URL
            if provider == "openai-codex" and (source := codex_source(name, entry)) is not None:
                options["credential_source"] = source

        if provider == "openai-compatible" and (not base_url or not options.get("api_key")):
            raise ModelSetupError(f"Set a base URL and sign in to {name!r} with /login.")

        pydantic_id = f"{self._inferred_type(provider)}:{model_name}"
        try:
            if provider == "openai-compatible":
                from pydantic_ai.models.openai import OpenAIChatModel
                from pydantic_ai.profiles.openai import OpenAIModelProfile
                from pydantic_ai.providers.openai import OpenAIProvider

                model = OpenAIChatModel(
                    model_name,
                    provider=OpenAIProvider(**options),
                    profile=OpenAIModelProfile(),
                )
            elif config is None and provider == "github-copilot":
                from pydantic_ai.models.openai import OpenAIResponsesModel
                from pydantic_ai.profiles.openai import OpenAIModelProfile

                copilot = GitHubCopilotProvider(**options)
                profile = OpenAIModelProfile()
                catalog_model = self._copilot_models.get(name, {}).get(model_name)
                tiers = catalog_model.tier_prompt_limits() if catalog_model else None
                if extended and tiers is None:
                    raise ModelSetupError(f"Extended context is unavailable for {model_name!r}.")
                if extended or (
                    catalog_model is not None and copilot.base_url.rstrip("/") == _COPILOT_URL
                ):
                    headers[_COPILOT_VERSION_HEADER] = (
                        _COPILOT_DEFAULT_VERSION
                        if tiers and not extended
                        else _COPILOT_EXTENDED_VERSION
                    )
                if catalog_model is not None and (
                    window := catalog_model.context_window(extended=extended)
                ):
                    profile["context_window"] = window
                if catalog_model is not None and catalog_model.endpoint() == "/responses":
                    profile.update(
                        openai_responses_requires_streaming=True,
                        openai_responses_requires_store_false=True,
                    )
                    model = OpenAIResponsesModel(model_name, provider=copilot, profile=profile)
                else:
                    model_type = (
                        CopilotCarbonModel
                        if model_name == "gemini-4-carbon"
                        and copilot.base_url.rstrip("/") == _COPILOT_URL
                        else GitHubCopilotModel
                    )
                    model = model_type(model_name, provider=copilot, profile=profile)
            else:
                model = (
                    infer_model(pydantic_id)
                    if not options
                    else infer_model(
                        pydantic_id,
                        provider_factory=lambda _name: infer_provider_class(provider)(**options),
                    )
                )
        except ImportError as exc:
            raise ModelSetupError(
                f"Install the Pydantic AI provider extra for {provider!r} in this environment."
            ) from exc
        except UserError as exc:
            raise ModelSetupError(
                f"Check the settings for {name!r} in /providers and sign in with /login."
            ) from exc
        except ModelSetupError:
            raise
        except (ValueError, TypeError) as exc:
            raise ModelSetupError(
                f"Cannot configure {name!r} ({provider}) with model {model_name!r}."
            ) from exc
        return ResolvedModel(
            model=model,
            settings=ModelSettings(extra_headers=headers) if headers else None,
        )

    def _split(self, model_id: str) -> tuple[str, ProviderEntry, str]:
        """Split an advertised ``entry:model`` id into its entry name, entry, and model."""
        name, _, model_name = model_id.partition(":")
        entry = self.settings.providers.get(name)
        if entry is None:
            raise ValueError(f"Provider {name!r} is not configured.")
        if not model_name:
            raise ValueError(f"Model id {model_id!r} must read entry:model.")
        return name, entry, model_name

    def _allowed_efforts(self, model_id: str) -> dict[str, ModelSettings]:
        policy = self.settings.thinking
        if not policy.levels:
            return {}
        try:
            name, entry, model_name = self._split(model_id)
            config = self.current_config(name)
        except ValueError:
            return {}

        provider = config.route.api_type if config else entry.provider_id(name)
        if provider == "github-copilot":
            base_model, _ = self._context_selection(model_name)
            catalog_model = self._local_copilot_model(name, base_model)
            supported = openai_efforts(catalog_model.reasoning_efforts()) if catalog_model else {}
        else:
            base_url = config.route.base_url if config else entry.base_url
            supported = native_efforts(
                provider, model_name, base_url, entry.options, self.settings.model_settings
            )

        allowed: dict[str, ModelSettings] = {
            level: supported[level] for level in policy.levels if level in supported
        }
        if policy.allow_off and EFFORT_OFF in supported:
            return {EFFORT_OFF: supported[EFFORT_OFF], **allowed}
        return allowed

    def _configured_effort(self) -> object:
        effort = self.settings.model_settings.get("thinking", EFFORT_DEFAULT)
        return EFFORT_OFF if effort is False else effort

    def list_options(self, model_id: str) -> Sequence[SettingOption]:
        """Advertise the effective route's verified efforts allowed by the deployment."""
        allowed = self._allowed_efforts(model_id)
        if not allowed:
            return ()
        configured = self._configured_effort()
        default = (
            configured if isinstance(configured, str) and configured in allowed else EFFORT_DEFAULT
        )
        choices = [
            self._effort_choice(value, is_default=value == default)
            for value in (EFFORT_DEFAULT, *allowed)
        ]
        return [
            SettingOption(
                id=_EFFORT_OPTION_ID,
                name="Reasoning Effort",
                category="thought_level",
                description="How much the model reasons before it answers.",
                choices=choices,
            )
        ]

    def resolve_settings(
        self, model_id: str, setting_selections: Mapping[str, str]
    ) -> ModelSettings:
        """Validate an explicit setting and translate it to the provider's request format."""
        configured = _EFFORT_OPTION_ID not in setting_selections
        value = self._configured_effort() if configured else setting_selections[_EFFORT_OPTION_ID]
        if value == EFFORT_DEFAULT:
            return ModelSettings()
        allowed = self._allowed_efforts(model_id)
        if isinstance(value, str) and value in allowed:
            return allowed[value]
        choices = ", ".join((EFFORT_DEFAULT, *allowed))
        source = " in [model_settings].thinking" if configured else ""
        raise ValueError(
            f"Reasoning effort {value!r}{source} is unsupported for {model_id!r}. Choose {choices}."
        )

    @staticmethod
    def _effort_choice(value: str, *, is_default: bool = False) -> SettingChoice:
        name, description = _EFFORT_COPY.get(value, (value, ""))
        return SettingChoice(
            value=value, name=name, description=description or None, is_default=is_default
        )


@lru_cache(maxsize=1)
def _known_models() -> Mapping[str, Sequence[str]]:
    grouped: dict[str, list[str]] = {}
    for target in known_model_names():
        provider, _, model = target.partition(":")
        if provider and model:
            grouped.setdefault(provider, []).append(model)
    return grouped


def _print_catalog(resolver: RuntimeModelResolver) -> None:
    providers = []
    for name, entry in resolver.settings.providers.items():
        provider = entry.provider_id(name)
        models = resolver.provider_models(name)
        thinking = {
            model: support
            for model in models
            if (support := _thinking_support(f"{provider}:{model}")) is not None
        }
        providers.append(
            {
                "name": name,
                "models": models,
                "thinking": thinking,
                "login": provider in {"github-copilot", "openai-codex"},
            }
        )
    print(json.dumps({"providers": providers}, indent=2))


def _attachment_config(session: AcpSession) -> AcpAttachmentConfig[None]:
    """Turn the client's MCP declarations into Pydantic AI toolsets."""
    return AcpAttachmentConfig(toolsets=acp_mcp_toolsets(session))


def run(
    *,
    config: Annotated[Path | None, Parameter(help="Path to the hrns configuration file.")] = None,
    env_only: Annotated[
        bool,
        Parameter(help="Ignore the configuration file and use environment variables only."),
    ] = False,
    catalog: Annotated[bool, Parameter(help="Print the provider/model catalog.")] = False,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
    setup: Annotated[bool, Parameter(show=False)] = False,
    local_daemon: Annotated[bool, Parameter(show=False)] = False,
) -> None:
    """Run the configured agent as an ACP stdio server."""
    settings = load_settings(config, env_only=env_only)

    resolver = RuntimeModelResolver(settings)
    if any(
        entry.provider_id(name) in _LIVE_CATALOG_PROVIDERS
        for name, entry in settings.providers.items()
    ):
        asyncio.run(resolver.load_provider_catalogs())
    if catalog:
        _print_catalog(resolver)
        return
    store = LocalSessionStore(settings.store)
    scope: Path | None = None
    startup_override = {"HRNS_PROVIDER", "HRNS_MODEL"}.intersection(
        key.upper() for key in os.environ
    )
    if not env_only:
        scope = settings.path.resolve()
        try:
            resolver.last_provider_models = store.provider_models(scope)
            if not startup_override:
                resolver.startup_model = store.startup_model(scope)
        except SessionStoreError as exc:
            logger.warning("Could not load the last selected model: %s", exc)

    async def remember_model(model: str) -> None:
        if scope is not None:
            await store.remember_startup_model(scope, model)
            resolver.last_provider_models[model.partition(":")[0]] = model
            if not startup_override:
                resolver.startup_model = model

    models = resolver.list_models()
    if not models and env_only and not setup:
        raise SystemExit("No models are configured. Add a provider and model first.")

    def factory(session: SessionContext) -> AbstractAgent[None, str]:
        # Resolve the selected catalog entry at prompt time, including after local setup.
        return build_agent(
            cwd=session.cwd if local_daemon or cwd == "." else cwd,
            skills=settings.skills,
            model_settings=ModelSettings(
                **{
                    key: value
                    for key, value in settings.model_settings.items()
                    if key != "thinking"
                }
            ),
            _allow_unconfigured=True,
        )

    model_memory = remember_model if scope is not None else None
    mcp_capabilities = schema.McpCapabilities(acp=True, http=True, sse=True)
    if local_daemon:
        from .local_daemon import serve

        server = AcpServer[None]()
        registration = server.add_agent(
            "agent",
            factory=factory,
            model_resolver=resolver,
            session_store=store,
            attachment_config=_attachment_config,
            mcp_capabilities=mcp_capabilities,
        )

        def adapter():
            connection = registration.connection()
            connection._provider_editor = None if env_only else resolver.provider_editor()
            connection._remember_model = model_memory
            return connection

        serve(server, adapter, store.path, configuration_id(settings, env_only=env_only))
        return

    run_acp_stdio(
        factory,
        model_resolver=resolver,
        session_store=store,
        attachment_config=_attachment_config,
        mcp_capabilities=mcp_capabilities,
        _provider_editor=None if env_only else resolver.provider_editor(),
        _remember_model=model_memory,
    )


app = App(
    name="hrns agent",
    help="Run the configured agent as an ACP stdio server.",
    default_command=run,
)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
