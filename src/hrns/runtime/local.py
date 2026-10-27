"""Configured local developer agent served through the upstream ACP adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.settings import ModelSettings

from hrns.config import LocalSettings, thinking_from_settings
from hrns.config.credentials import store_secret
from hrns.config.mcp import McpServerConfig, default_token_store_path
from hrns.protocol import HrnsError

from .cli import _serve_until_closed
from .definitions import AgentDefinition, AgentSpec, DefinitionBinding
from .host import HrnsACPAgent
from .host_support import HostBundle, HostPolicy, build_store
from .login import DeviceAuthorization, device_login, login_providers, supports_login
from .registry import PluginRegistry
from .resources import ANSWERS, MODEL, PLAN, WORKSPACE, ResourceRequirement

_LOCAL_INSTRUCTIONS = """\
You are a local hrns agent connected through ACP, working in the user's project.

The bundled coder capability provides file tools, content search, and a shell in the
workspace. Keep commands non-interactive and confirm changes with a command instead of
assuming they work. Answer directly when no tool is needed.
"""

#: Tools that only read the workspace (or ask the user); everything else needs approval.
_READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "grep",
        "list_directory",
        "search_files",
        "find_files",
        "ask_user_question",
    }
)

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
    """Map model ids to ``"supported"``/``"always"`` when the model reasons."""
    from pydantic_ai.models import infer_model_profile, known_model_names

    known = set(known_model_names())
    support: dict[str, str] = {}
    for model_id in model_ids:
        if model_id not in known:
            support[model_id] = "custom"
            continue
        try:
            profile = infer_model_profile(model_id)
        except Exception:
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
    return {provider: sorted(models, key=str.lower) for provider, models in grouped.items()}


def provider_catalog() -> dict[str, Any]:
    """Known providers, their model ids, and reasoning support, from Pydantic AI."""
    grouped = known_models()
    logins = set(login_providers())
    providers: list[dict[str, Any]] = []
    for provider in sorted(grouped, key=_provider_sort_key):
        models = grouped[provider]
        support = thinking_support(f"{provider}:{model}" for model in models)
        entry: dict[str, Any] = {
            "name": provider,
            "models": models,
            "thinking": {
                model: level for model in models if (level := support.get(f"{provider}:{model}"))
            },
        }
        if provider in logins:
            entry["login"] = True
        providers.append(entry)
    return {"providers": providers}


def _login_hint(provider: str, key: str | None) -> str:
    """Point at the account login when a provider supports one and no key was given."""
    if key is not None or not supports_login(provider):
        return ""
    return (
        f" {provider} also accepts an account login: run `hrns login {provider}` "
        "or press Ctrl+G in the local model form."
    )


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
            "environment (for example hrns[anthropic]). OpenAI-compatible providers "
            "such as DeepSeek ship with hrns."
        ) from exc
    except (ValueError, TypeError, UserError) as exc:
        raise ValueError(
            f"Cannot configure provider {provider!r} / model {selected!r}. "
            "Check the provider name, credentials and supported provider_options. "
            "For a custom OpenAI-compatible base_url use provider = 'openai-chat'."
            + _login_hint(provider, key)
        ) from exc


async def _require_tool_approval(_ctx: Any, tools: list[Any]) -> list[Any]:
    """Approve workspace reads; ask before anything that writes or runs a command."""
    return [
        tool
        if getattr(tool, "name", None) in _READ_ONLY_TOOLS
        else replace(tool, kind="unapproved")
        for tool in tools
    ]


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


def local_definition(settings: LocalSettings) -> AgentDefinition:
    """The built-in local definition: coder tools, Todo, Agent Skills, and MCP servers."""
    token_path = settings.mcp.token_store_path or str(default_token_store_path())
    bindings = [
        DefinitionBinding(
            plugin_id="hrns.coder",
            binding_id="coder",
        ),
        DefinitionBinding(
            plugin_id="hrns.todo",
            binding_id="todo",
        ),
        DefinitionBinding(
            plugin_id="hrns.skills",
            binding_id="skills",
        ),
        *[
            DefinitionBinding(
                plugin_id="hrns.mcp",
                binding_id=f"mcp.{name}",
                config=McpServerConfig(
                    **server.model_dump(mode="json"),
                    token_store=settings.mcp.token_store,
                    token_store_path=token_path,
                ).model_dump(mode="json"),
                enabled=server.enabled,
            )
            for name, server in settings.mcp.servers.items()
        ],
    ]
    resources = [
        ResourceRequirement.of(
            MODEL, description="Agent model (provider string or native Model instance)."
        ),
        ResourceRequirement.of(WORKSPACE, description="Directory the bundled tools act on."),
        ResourceRequirement.of(
            PLAN,
            description="Session-scoped plan channel that streams todos to the connected client.",
        ),
    ]
    if settings.capabilities.ask_user:
        bindings.append(
            DefinitionBinding(
                plugin_id="hrns.ask_user",
                binding_id="ask_user",
            )
        )
        resources.append(
            ResourceRequirement.of(
                ANSWERS,
                description="Answerer that carries agent questions to the connected client.",
            )
        )
    definition = AgentDefinition(
        agent_id="hrns.local",
        name=f"Local · {settings.provider}:{settings.model}",
        description=(
            "Local coding agent with bundled file, search, and shell tools, a live Todo "
            "plan, Agent Skills, and structured user questions."
        ),
        spec=AgentSpec(instructions=_LOCAL_INSTRUCTIONS),
        resources=resources,
        bindings=bindings,
    )
    definition.fixed_capabilities.append(PrepareTools(_require_tool_approval))
    return definition


def build_local_agent(
    settings: LocalSettings,
    *,
    cwd: Path | None = None,
    model: Model | None = None,
) -> HrnsACPAgent:
    """Bind a model and trusted local workspace; workspace-changing tools need approval."""
    workspace = (cwd or Path.cwd()).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("The local working directory must be a directory.")
    model = model if model is not None else configured_model(settings)
    definition = local_definition(settings)
    registry = PluginRegistry()
    registry.discover()
    store = build_store(str(settings.store.expanduser()))
    models = _local_models(settings)
    bundle = HostBundle(
        definition=definition,
        registry=registry,
        store=store,
        close_store=True,
        model_resolver=lambda model_id: configured_model(settings, model_id),
    )
    return HrnsACPAgent(
        bundle,
        HostPolicy(
            allowed_cwds=(str(workspace),),
            default_cwd=str(workspace),
            models=models,
            default_model=f"{settings.provider}:{settings.model}",
            resources={MODEL: model, WORKSPACE: str(workspace)},
            thinking_models=thinking_support(models),
            default_effort=thinking_from_settings(settings),
            model_settings=ModelSettings(**settings.model_settings),
        ),
    )


async def _run(settings: LocalSettings) -> None:
    agent = build_local_agent(settings)
    await agent.store.backend.initialize()
    await _serve_until_closed(agent)


def _emit_event(event: dict[str, object]) -> None:
    """One JSON line on stdout; the client renders these while the login runs."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _login(provider: str, account: str) -> int:
    """Run a device flow to completion and store the token in the OS keyring."""

    def announce(authorization: DeviceAuthorization) -> None:
        _emit_event(
            {
                "event": "device",
                "provider": provider,
                "userCode": authorization.user_code,
                "verificationUri": authorization.verification_uri,
                "expiresIn": authorization.expires_in,
            }
        )

    outcome = asyncio.run(device_login(provider, on_authorization=announce))
    reference = store_secret(account, outcome.token)
    if reference is None:
        raise ValueError("Signed in, but the OS keyring is unavailable so the token was not saved.")
    _emit_event(
        {
            "event": "authorized",
            "provider": provider,
            "account": account,
            "reference": reference,
            "models": list(outcome.models),
        }
    )
    return 0


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
    parser.add_argument(
        "--login",
        metavar="PROVIDER",
        help="Sign in to a provider with an account login, print JSON events, and exit",
    )
    parser.add_argument(
        "--account",
        default="",
        metavar="NAME",
        help="Keyring account for --login (default: the provider id)",
    )
    args = parser.parse_args(argv)
    if args.catalog:
        print(json.dumps(provider_catalog(), ensure_ascii=False))
        return 0
    if args.login:
        try:
            return _login(args.login, args.account or args.login)
        except (ValueError, OSError, HrnsError) as exc:
            print(f"hrns-local: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130
    try:
        settings = (
            LocalSettings(config_file=None)
            if args.env_only
            else LocalSettings(config_file=args.config)
            if args.config
            else LocalSettings()
        )
        asyncio.run(_run(settings))
    except (ValueError, OSError, HrnsError) as exc:
        print(f"hrns-local: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
