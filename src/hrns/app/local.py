"""Local ACP entry point backed by the configured Pydantic AI model."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model, infer_model, known_model_names
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.settings import ModelSettings

from hrns.config import LocalSettings

from .acp import run_acp_stdio
from .agent import build_agent

__all__ = ["configured_model", "main"]


def configured_model(settings: LocalSettings, model_id: str | None = None) -> Model:
    """Build the Pydantic AI model for one ``entry:model`` selection."""
    settings.require_model()
    selected = model_id or settings.active_target()
    name, _, model_name = selected.partition(":")
    entry = settings.providers.get(name)
    if entry is None:
        raise ValueError(f"Provider {name!r} is not configured.")
    if not model_name:
        raise ValueError(f"Model id {selected!r} must read entry:model.")

    provider = entry.provider_id(name)
    options = dict(entry.options)
    if entry.base_url:
        options["base_url"] = entry.base_url
    key = settings.resolved_api_key(name)
    if key is not None:
        options["api_key"] = key

    pydantic_id = f"{provider}:{model_name}"
    try:
        if not options:
            return infer_model(pydantic_id)
        return infer_model(
            pydantic_id,
            provider_factory=lambda _name: infer_provider_class(provider)(**options),
        )
    except ImportError as exc:
        raise ValueError(
            f"Install the Pydantic AI provider extra for {provider!r} in this environment."
        ) from exc
    except (ValueError, TypeError, UserError) as exc:
        raise ValueError(
            f"Cannot configure {name!r} ({provider}) with model {model_name!r}."
        ) from exc


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

    model = configured_model(settings)
    agent = build_agent(
        model,
        cwd=cwd,
        model_settings=ModelSettings(**settings.model_settings),
    )
    # Advertise every configured model and resolve the client's choice through
    # this entry's provider configuration. The active model stays the default.
    targets = list(settings.model_targets(_known_models()))
    active = settings.active_target()
    models = [active, *(target for target in targets if target != active)]
    run_acp_stdio(
        agent,
        models=models,
        model_resolver=lambda target: configured_model(settings, target),
    )


app = App(
    name="hrns-local",
    help="Launch the configured local model as an ACP stdio server.",
    default_command=run,
)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
