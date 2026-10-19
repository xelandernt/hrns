"""Configured local developer agent served through the existing ACP host."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from harness_config import LocalSettings, thinking_from_settings
from harness_protocol import HarnessError
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import infer_provider_class

from .host import (
    HarnessHostAgent,
    HostBundle,
    HostMetadataStore,
    HostPolicy,
    _serve_until_closed,
    build_store,
)
from .registry import PluginRegistry
from .session import SessionManager
from .starters import developer_definition

#: Frequently used provider ids are shown first in the terminal picker.
_PROVIDER_PRIORITY = (
    "deepseek",
    "openai",
    "openai-chat",
    "openai-responses",
    "anthropic",
    "google",
    "google-cloud",
    "ollama",
    "bedrock",
    "azure",
    "groq",
    "mistral",
    "cohere",
    "xai",
    "zai",
)

#: Documented Pydantic AI providers that do not contribute a known-model list.
#: Their model ids are free-form, so the terminal keeps a custom-model input.
_EXTRA_PROVIDERS = (
    "azure-responses",
    "openai-codex",
    "openai-decisions",
    "openrouter",
    "vercel",
    "fireworks",
    "together",
    "github",
    "github-copilot",
    "litellm",
    "vllm",
    "nebius",
    "ovhcloud",
    "alibaba",
    "sambanova",
    "sentence-transformers",
    "system-one",
    "voyageai",
)


def _provider_sort_key(name: str) -> tuple[int, int, str]:
    if name in _PROVIDER_PRIORITY:
        return (0, _PROVIDER_PRIORITY.index(name), name)
    if name.startswith("gateway/"):
        return (2, 0, name)
    return (1, 0, name)


def thinking_support(model_ids: Iterable[str]) -> dict[str, str]:
    """Map model ids to ``"supported"``/``"always"`` when the model reasons.

    The client never imports Pydantic AI, so the catalog carries this instead.
    Profile lookup does not need credentials; providers whose extra is not
    installed simply contribute no entry.
    """
    from pydantic_ai.models import infer_model_profile, known_model_names

    known = set(known_model_names())
    support: dict[str, str] = {}
    for model_id in model_ids:
        if model_id not in known:
            # A custom id the registry cannot profile. The user chose it, so let
            # them opt into effort instead of claiming the model cannot reason.
            support[model_id] = "custom"
            continue
        try:
            profile = infer_model_profile(model_id)
        except Exception:  # a missing provider extra is not a catalog failure
            continue
        if profile.get("thinking_always_enabled"):
            support[model_id] = "always"
        elif profile.get("supports_thinking"):
            support[model_id] = "supported"
    return support


def known_models() -> dict[str, list[str]]:
    """Model ids grouped by provider, without the (slower) profile lookup."""
    from pydantic_ai.models import known_model_names, parse_model_id

    grouped: dict[str, set[str]] = {name: set() for name in _EXTRA_PROVIDERS}
    for known in known_model_names():
        provider, model = parse_model_id(known)
        if provider is None:
            continue
        grouped.setdefault(provider, set()).add(model)
    return {
        provider: sorted(models, key=str.lower) for provider, models in grouped.items()
    }


def provider_catalog() -> dict[str, Any]:
    """Known providers, their model ids, and reasoning support, from Pydantic AI."""
    grouped = known_models()
    providers: list[dict[str, Any]] = []
    for provider in sorted(grouped, key=_provider_sort_key):
        models = grouped[provider]
        support = thinking_support(f"{provider}:{model}" for model in models)
        providers.append(
            {
                "name": provider,
                "models": models,
                "thinking": {
                    model: level
                    for model in models
                    if (level := support.get(f"{provider}:{model}"))
                },
            }
        )
    return {"providers": providers}


def configured_model(settings: LocalSettings, model_id: str | None = None) -> Model:
    """Delegate provider/model selection to the pinned Pydantic AI registry."""
    settings.require_model()
    selected = model_id or f"{settings.provider}:{settings.model}"
    provider, _, _ = selected.partition(":")
    same_provider = provider == settings.provider
    options = dict(settings.provider_options) if same_provider else {}
    if same_provider and settings.base_url:
        options["base_url"] = settings.base_url
    key = settings.resolved_api_key() if same_provider else None
    if key is not None:
        options["api_key"] = key
    try:
        if not options:
            return infer_model(selected)
        if provider.startswith("gateway/"):
            from pydantic_ai.providers.gateway import gateway_provider

            return infer_model(
                selected,
                provider_factory=lambda name: gateway_provider(
                    name.removeprefix("gateway/"), **options
                ),
            )
        return infer_model(
            selected,
            provider_factory=lambda name: infer_provider_class(name)(**options),
        )
    except ImportError as exc:
        raise ValueError(
            f"Install the Pydantic AI provider extra for {provider!r} in the runtime "
            "environment (DeepSeek/OpenAI-compatible: harness-runtime[openai])."
        ) from exc
    except (ValueError, TypeError, UserError) as exc:
        # Constructor errors can contain configured secrets; only report parameter names.
        raise ValueError(
            f"Cannot configure provider {provider!r} / model {selected!r}. "
            "Check the provider name, credentials and supported provider_options. "
            "For a custom OpenAI-compatible base_url use provider = 'openai-chat'."
        ) from exc


async def _require_tool_approval(_ctx: Any, tools: list[Any]) -> list[Any]:
    return [replace(tool, kind="unapproved") for tool in tools]


def _local_models(settings: LocalSettings) -> tuple[str, ...]:
    """Known model ids for the configured provider, current model first."""
    current = f"{settings.provider}:{settings.model}"
    known = known_models().get(settings.provider)
    if known:
        models = [f"{settings.provider}:{model}" for model in known]
        if current not in models:
            models.insert(0, current)
        return tuple(dict.fromkeys(models))
    return (current,)


def build_local_agent(
    settings: LocalSettings,
    *,
    cwd: Path | None = None,
    model: Model | None = None,
) -> HarnessHostAgent:
    """Bind a model and trusted local workspace; every function tool needs approval."""
    workspace = (cwd or Path.cwd()).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("The local working directory must be a directory.")
    model = model if model is not None else configured_model(settings)
    definition = developer_definition()
    definition.agent_id = "harness.local"
    definition.name = f"Local · {settings.provider}:{settings.model}"
    definition.fixed_capabilities.append(PrepareTools(_require_tool_approval))
    spec = definition.spec

    def factory(*, model: Any, managed_capabilities: list[Any]) -> Agent:
        selected = (
            configured_model(settings, model)
            if isinstance(model, str)
            else model
        )
        return Agent.from_spec(
            spec,
            model=selected,
            # Model settings (including reasoning effort) arrive per turn from the
            # host so live changes do not require rebuilding the process.
            capabilities=managed_capabilities,
            output_type=[str, DeferredToolRequests],
        )

    definition.spec = None
    definition.factory = factory
    registry = PluginRegistry()
    registry.discover()
    store = build_store(settings.store.expanduser())
    manager = SessionManager(registry, store=store, definitions=[definition])
    bundle = HostBundle(
        definition,
        registry,
        manager,
        str(store.path),
        HostMetadataStore(store.path),
    )
    models = _local_models(settings)
    return HarnessHostAgent(
        bundle,
        HostPolicy(
            allowed_cwds=(str(workspace),),
            default_cwd=str(workspace),
            models=models,
            default_model=f"{settings.provider}:{settings.model}",
            resources={"model.default": model},
            thinking_models=thinking_support(models),
            default_effort=thinking_from_settings(settings),
            model_settings=settings.model_settings,
        ),
    )


async def _run(settings: LocalSettings) -> None:
    # Provider HTTP clients are constructed on the same event loop as their requests.
    await _serve_until_closed(build_local_agent(settings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the configured local developer agent over ACP."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path, help="Local settings TOML file")
    source.add_argument("--env-only", action="store_true", help="Use environment settings only")
    parser.add_argument(
        "--catalog",
        action="store_true",
        help="Print the known provider/model catalog as JSON and exit",
    )
    args = parser.parse_args(argv)
    if args.catalog:
        print(json.dumps(provider_catalog(), ensure_ascii=False))
        return 0
    try:
        settings = LocalSettings(
            **(
                {"config_file": None}
                if args.env_only
                else {"config_file": args.config}
                if args.config
                else {}
            )
        )
        asyncio.run(_run(settings))
    except (ValueError, OSError, HarnessError) as exc:
        print(f"harness-local: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
