"""Local ACP entry point backed by the configured Pydantic AI model."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import infer_model, infer_model_profile, known_model_names
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.settings import ModelSettings, merge_model_settings

from hrns.config import (
    EFFORT_DEFAULT,
    EFFORT_OFF,
    THINKING_LEVELS,
    HrnsConfig,
    ProviderEntry,
    thinking_from_settings,
)

from .acp import (
    ModelChoice,
    ProviderConfig,
    ProviderDescriptor,
    ProviderDisabled,
    ProviderModelResolver,
    ProviderRoute,
    ResolvedModel,
    SettingChoice,
    SettingOption,
    run_acp_stdio,
)
from .agent import build_agent

__all__ = ["LocalModelResolver", "main"]

#: The ACP config option id for the reasoning-effort ladder.
_EFFORT_OPTION_ID = "effort"

#: One ``ModelSettings`` per selectable effort; the default value is absent by design.
_THINKING_BY_EFFORT: dict[str, ModelSettings] = {
    EFFORT_OFF: ModelSettings(thinking=False),
    **{level: ModelSettings(thinking=level) for level in THINKING_LEVELS},
}

#: Display names and one-line explanations, shared by the ACP option and the client.
_EFFORT_COPY: dict[str, tuple[str, str]] = {
    EFFORT_DEFAULT: ("Default", "Let the provider choose its default reasoning effort."),
    EFFORT_OFF: ("Off", "Disable reasoning when the model allows it."),
    "minimal": ("Minimal", "Fastest responses with the least reasoning."),
    "low": ("Low", "Light reasoning for straightforward work."),
    "medium": ("Medium", "Balanced reasoning for everyday work."),
    "high": ("High", "Deeper reasoning for harder work."),
    "xhigh": ("XHigh", "Maximum reasoning; highest cost and latency."),
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


class LocalModelResolver(ProviderModelResolver):
    """The local configuration, exposed as the models and providers ACP can use.

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

    def list_models(self) -> Sequence[ModelChoice]:
        """Every ``entry:model`` target, with the active one first as the default."""
        targets = list(self.settings.model_targets(_known_models()))
        active = self.settings.active_target()
        ordered = [active, *(target for target in targets if target != active)]
        choices: list[ModelChoice] = []
        for target in ordered:
            name, separator, model_name = target.partition(":")
            if not separator or not model_name or name not in self.settings.providers:
                continue
            choices.append(ModelChoice(model_id=target, provider_id=name))
        return choices

    def build_model(self, model_id: str, config: ProviderConfig | None) -> ResolvedModel:
        """Build one ``entry:model`` selection through the entry's provider.

        The client's routing takes precedence over the entry's own ``base_url``: the base
        URL goes to the provider, while the headers ride along as ``extra_headers`` model
        settings because pydantic-ai applies request headers per run.
        """
        settings = self.settings
        name, entry, model_name = self._split(model_id)

        if config is None:
            provider = entry.provider_id(name)
            base_url = entry.base_url
            headers: dict[str, str] = {}
        else:
            provider = config.route.api_type
            base_url = config.route.base_url
            headers = dict(config.headers)

        options = dict(entry.options)
        if base_url:
            options["base_url"] = base_url
        key = settings.resolved_api_key(name)
        if key is not None:
            options["api_key"] = key

        pydantic_id = f"{provider}:{model_name}"
        try:
            model = (
                infer_model(pydantic_id)
                if not options
                else infer_model(
                    pydantic_id,
                    provider_factory=lambda _name: infer_provider_class(provider)(**options),
                )
            )
        except ImportError as exc:
            raise ValueError(
                f"Install the Pydantic AI provider extra for {provider!r} in this environment."
            ) from exc
        except (ValueError, TypeError, UserError) as exc:
            raise ValueError(
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

    def _run_provider_id(self, name: str, entry: ProviderEntry) -> str:
        """The Pydantic AI provider the next run would use for this entry.

        A client-configured route replaces the entry's protocol, so the profile that
        decides whether reasoning exists must be read from the route, not the entry.
        """
        try:
            config = self.current_config(name)
        except ProviderDisabled:
            return entry.provider_id(name)
        return config.route.api_type if config is not None else entry.provider_id(name)

    def list_options(self, model_id: str) -> Sequence[SettingOption]:
        """The reasoning-effort ladder ``[thinking]`` allows for one model.

        The configured levels are policy; the model's profile decides whether reasoning
        exists at all and whether it can be turned off. A model outside the runtime's
        catalog is offered the policy as-is, so a custom id is opt-in rather than blocked.
        """
        levels = self.settings.thinking.levels
        if not levels:
            return ()
        try:
            name, entry, model_name = self._split(model_id)
        except ValueError:
            return ()
        profile = _profile_for(f"{self._run_provider_id(name, entry)}:{model_name}")
        if profile is not None and not profile.get("supports_thinking"):
            return ()
        choices = [self._effort_choice(EFFORT_DEFAULT, is_default=True)]
        if self.settings.thinking.allow_off and not (
            profile is not None and profile.get("thinking_always_enabled")
        ):
            choices.append(self._effort_choice(EFFORT_OFF))
        choices.extend(self._effort_choice(level) for level in levels)
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
        """Turn the session's effort choice into a ``thinking`` delta.

        ``default`` emits nothing so the agent's settings and the provider's default stand;
        ``off`` disables reasoning where the model allows it.
        """
        value = setting_selections.get(_EFFORT_OPTION_ID)
        if value is None or value == EFFORT_DEFAULT:
            return ModelSettings()
        if value == EFFORT_OFF:
            if not self.settings.thinking.allow_off:
                raise ValueError("reasoning cannot be disabled: [thinking].allow_off is false")
        elif value not in self.settings.thinking.levels:
            raise ValueError(f"Reasoning effort {value!r} is not offered by [thinking]")
        settings = _THINKING_BY_EFFORT.get(value)
        if settings is None:
            raise ValueError(f"Unknown reasoning effort: {value!r}")
        return settings

    @staticmethod
    def _effort_choice(value: str, *, is_default: bool = False) -> SettingChoice:
        name, description = _EFFORT_COPY.get(value, (value, ""))
        return SettingChoice(
            value=value, name=name, description=description or None, is_default=is_default
        )


def _known_models() -> Mapping[str, Sequence[str]]:
    grouped: dict[str, list[str]] = {}
    for target in known_model_names():
        provider, _, model = target.partition(":")
        if provider and model:
            grouped.setdefault(provider, []).append(model)
    return grouped


def _print_catalog(settings: HrnsConfig) -> None:
    known = _known_models()
    providers = []
    for name, entry in settings.providers.items():
        provider = entry.provider_id(name)
        models = list(dict.fromkeys([*known.get(provider, ()), *entry.custom_models]))
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
                "login": False,
            }
        )
    print(json.dumps({"providers": providers}, indent=2))


def _check_configured_effort(
    settings: HrnsConfig, model_id: str, resolver: LocalModelResolver
) -> None:
    """Refuse a configured effort the policy does not offer for the startup model.

    The agent's own settings are the baseline every session inherits, so a value outside
    ``[thinking]`` would apply without ever appearing among a client's options.
    """
    configured = thinking_from_settings(settings)
    if configured is None:
        return
    offered = {
        choice.value for option in resolver.list_options(model_id) for choice in option.choices
    }
    if not offered:
        # The model advertises no effort option; the provider ignores the setting anyway.
        return
    if configured not in offered:
        raise SystemExit(
            f"model_settings.thinking = {configured!r} is not offered by [thinking] for "
            f"{model_id}. Allowed: {', '.join(sorted(offered))}."
        )


def run(
    *,
    config: Annotated[Path | None, Parameter(help="Path to the hrns configuration file.")] = None,
    env_only: Annotated[
        bool,
        Parameter(help="Ignore the configuration file and use environment variables only."),
    ] = False,
    catalog: Annotated[bool, Parameter(help="Print the provider/model catalog.")] = False,
    login: Annotated[bool, Parameter(help="Account login (not supported).")] = False,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
) -> None:
    """Launch the configured local model as an ACP stdio server."""
    settings = (
        HrnsConfig(config_file=None)
        if env_only
        else HrnsConfig(config_file=config)
        if config is not None
        else HrnsConfig()
    )

    if catalog:
        _print_catalog(settings)
        return
    if login:
        raise SystemExit("Account login is not supported by the minimal backend.")

    resolver = LocalModelResolver(settings)
    models = resolver.list_models()
    if not models:
        raise SystemExit("No models are configured. Add a provider and model first.")
    _check_configured_effort(settings, models[0].model_id, resolver)
    resolved = resolver.resolve(models[0].model_id)
    agent = build_agent(
        resolved.model,
        cwd=cwd,
        model_settings=merge_model_settings(
            ModelSettings(**settings.model_settings), resolved.settings
        ),
    )
    # Advertise every configured model and resolve the client's choice through this
    # entry's provider configuration. The active model stays the default.
    run_acp_stdio(agent, model_resolver=resolver)


app = App(
    name="hrns-local",
    help="Launch the configured local model as an ACP stdio server.",
    default_command=run,
)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
