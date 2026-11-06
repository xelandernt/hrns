"""Local ACP entry point backed by the configured Pydantic AI model."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import infer_model, known_model_names
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.settings import ModelSettings, merge_model_settings

from hrns.config import LocalSettings

from .acp import (
    ModelChoice,
    ProviderConfig,
    ProviderDescriptor,
    ProviderModelResolver,
    ProviderRoute,
    ResolvedModel,
    run_acp_stdio,
)
from .agent import build_agent

__all__ = ["LocalModelResolver", "main"]


class LocalModelResolver(ProviderModelResolver):
    """The local configuration, exposed as the models and providers ACP can use.

    One provider per configured entry, named after the entry, because ACP model ids read
    ``entry:model``. Each entry supports exactly the Pydantic AI provider it is configured
    for, so a client can move its routing to its own gateway but cannot change which
    protocol the entry speaks. The entry this deployment starts with is ``required``:
    disabling it would leave the default session without a model.
    """

    def __init__(self, settings: LocalSettings) -> None:
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
        name, _, model_name = model_id.partition(":")
        entry = settings.providers.get(name)
        if entry is None:
            raise ValueError(f"Provider {name!r} is not configured.")
        if not model_name:
            raise ValueError(f"Model id {model_id!r} must read entry:model.")

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


def _known_models() -> Mapping[str, Sequence[str]]:
    grouped: dict[str, list[str]] = {}
    for target in known_model_names():
        provider, _, model = target.partition(":")
        if provider and model:
            grouped.setdefault(provider, []).append(model)
    return grouped


def _print_catalog(settings: LocalSettings) -> None:
    known = _known_models()
    providers = []
    for name, entry in settings.providers.items():
        provider = entry.provider_id(name)
        models = list(dict.fromkeys([*known.get(provider, ()), *entry.custom_models]))
        providers.append(
            {
                "name": name,
                "models": models,
                "thinking": {},
                "login": False,
            }
        )
    print(json.dumps({"providers": providers}, indent=2))


def run(
    *,
    config: Annotated[Path | None, Parameter(help="Path to hrns.local.toml.")] = None,
    env_only: Annotated[
        bool,
        Parameter(help="Ignore the local config file and use environment variables only."),
    ] = False,
    catalog: Annotated[bool, Parameter(help="Print the provider/model catalog.")] = False,
    login: Annotated[bool, Parameter(help="Account login (not supported).")] = False,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
) -> None:
    """Launch the configured local model as an ACP stdio server."""
    settings = (
        LocalSettings(config_file=None)
        if env_only
        else LocalSettings(config_file=config)
        if config is not None
        else LocalSettings()
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
