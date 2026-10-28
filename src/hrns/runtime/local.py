"""Configured local developer agent served through the upstream ACP adapter."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import AsyncIterator, Iterable
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
from .resources import ANSWERS, CLIENT, MODEL, PLAN, WORKSPACE, ResourceRequirement
from .workspaces import require_pwsh

_LOCAL_INSTRUCTIONS = """\
You are a local hrns agent connected through ACP, working in the user's project.

The bundled coder capability provides file tools, content search, and a shell in the
workspace. Keep commands non-interactive and confirm changes with a command instead of
assuming they work. Answer directly when no tool is needed.
"""

_WINDOWS_INSTRUCTIONS = """\

The shell is PowerShell 7 (`pwsh`) on this Windows machine. Write shell commands in
PowerShell syntax, not POSIX shell syntax. File listing and search use a built-in walk that
skips common generated directories instead of repository ignore files.
"""


def _instructions() -> str:
    if os.name == "nt":
        return _LOCAL_INSTRUCTIONS + _WINDOWS_INSTRUCTIONS
    return _LOCAL_INSTRUCTIONS


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


def _thinking_map(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Map a target id to ``"supported"``/``"always"``/``"custom"`` for its model id."""
    from pydantic_ai.models import infer_model_profile, known_model_names

    known = set(known_model_names())
    support: dict[str, str] = {}
    for target, model_id in pairs:
        if model_id not in known:
            support[target] = "custom"
            continue
        try:
            profile = infer_model_profile(model_id)
        except Exception:
            continue
        if profile.get("thinking_always_enabled"):
            support[target] = "always"
        elif profile.get("supports_thinking"):
            support[target] = "supported"
    return support


def thinking_support(model_ids: Iterable[str]) -> dict[str, str]:
    """Map model ids to ``"supported"``/``"always"`` when the model reasons."""
    return _thinking_map((model_id, model_id) for model_id in model_ids)


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


def local_models(settings: LocalSettings) -> tuple[str, ...]:
    """Every ``entry:model`` target the configured providers can serve.

    The active model comes first because the adapter starts each session on the first
    advertised id. An entry whose provider type has no catalog still contributes its
    selected model, so the session can switch back to it.
    """
    targets = list(settings.model_targets(known_models()))
    current = settings.active_target() if settings.provider and settings.model else ""
    if current:
        targets = [current, *(target for target in targets if target != current)]
    return tuple(dict.fromkeys(targets))


def local_thinking(settings: LocalSettings) -> dict[str, str]:
    """Reasoning support keyed by ``entry:model`` target."""
    catalog = known_models()
    pairs = [
        (f"{name}:{model}", f"{settings.provider_id(name)}:{model}")
        for name in settings.providers
        for model in settings.models_for(name, catalog)
    ]
    return _thinking_map(pairs)


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


_COPILOT_RESPONSES_ONLY: dict[str, frozenset[str]] = {}
"""Per-token catalog cache: ids Copilot serves on ``/responses`` only."""


def _copilot_token(api_key: str | None) -> str | None:
    """The Copilot bearer, from settings or the variables its provider reads."""
    if api_key:
        return api_key
    for name in ("GITHUB_COPILOT_API_KEY", "GITHUB_COPILOT_API_TOKEN", "COPILOT_GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _copilot_catalog_key(token: str, base_url: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()
    return f"{base_url}\0{digest}"


def _responses_only(item: object) -> bool:
    """Whether a catalog entry lists ``/responses`` without ``/chat/completions``."""
    extra_object = getattr(item, "model_extra", None)
    extra: dict[str, object] = extra_object if isinstance(extra_object, dict) else {}
    endpoints = extra.get("supported_endpoints")
    if not isinstance(endpoints, list):
        return False
    has_responses = any(isinstance(entry, str) and "/responses" in entry for entry in endpoints)
    has_chat = any(isinstance(entry, str) and "chat/completions" in entry for entry in endpoints)
    return has_responses and not has_chat


async def _prefetch_copilot_catalog(settings: LocalSettings) -> None:
    """Record which ids these tokens must be called on ``/responses``.

    Copilot's catalog is the only reliable switch: availability is per plan and per
    OAuth client, and the split moves as models ship. One fetch per entry per runtime
    process; a failed fetch leaves every id on Chat Completions, as before.
    """
    for name in settings.providers:
        if settings.provider_id(name) == "github-copilot":
            await _prefetch_copilot_entry(settings, name)


async def _prefetch_copilot_entry(settings: LocalSettings, name: str) -> None:
    entry = settings.require_entry(name)
    try:
        token = _copilot_token(settings.resolved_api_key(name))
    except ValueError:
        return
    if token is None:
        return
    key = _copilot_catalog_key(token, entry.base_url)
    if key in _COPILOT_RESPONSES_ONLY:
        return
    from pydantic_ai.providers.github_copilot import GitHubCopilotProvider

    try:
        async with GitHubCopilotProvider(
            api_key=token, base_url=entry.base_url or None
        ) as provider:
            listing = await provider.client.models.list()
    except Exception:  # noqa: BLE001 - the session still works on Chat Completions
        return
    _COPILOT_RESPONSES_ONLY[key] = frozenset(
        item.id for item in listing.data if _responses_only(item)
    )


def _copilot_uses_responses(model_name: str, api_key: str | None, options: dict[str, Any]) -> bool:
    """Whether the cached catalog says this id is served on ``/responses`` only."""
    token = _copilot_token(api_key)
    if token is None:
        return False
    key = _copilot_catalog_key(token, str(options.get("base_url") or ""))
    return model_name in _COPILOT_RESPONSES_ONLY.get(key, frozenset())


def _copilot_responses_model(model_name: str, options: dict[str, Any]) -> Model:
    """Copilot's Responses transport, which Pydantic AI does not ship.

    ``GitHubCopilotModel`` is an ``OpenAIChatModel``, so Responses-only ids such as
    ``gpt-5.6-luna`` can never reach their endpoint through it. Reuse the Copilot
    client (base URL and editor headers) with the Responses model, and the Copilot
    family profile so reasoning settings resolve per vendor.
    """
    from openai import AsyncStream
    from openai.types import responses
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.openai import (
        OpenAIModelName,
        OpenAIResponsesModel,
        OpenAIResponsesModelSettings,
        OpenAIResponsesStreamedResponse,
    )
    from pydantic_ai.providers.github_copilot import GitHubCopilotProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    class _CopilotResponsesStream(AsyncStream[responses.ResponseStreamEvent]):
        """Keep one Responses item ID for each Copilot function call."""

        def __init__(self, wrapped: AsyncStream[responses.ResponseStreamEvent]) -> None:
            self._wrapped = wrapped
            self._item_ids: dict[str, str] = {}

        def __aiter__(self) -> AsyncIterator[responses.ResponseStreamEvent]:
            return self

        async def __anext__(self) -> responses.ResponseStreamEvent:
            return self._normalize(await anext(self._wrapped))

        def _normalize(self, event: responses.ResponseStreamEvent) -> responses.ResponseStreamEvent:
            if not isinstance(
                event,
                responses.ResponseOutputItemAddedEvent | responses.ResponseOutputItemDoneEvent,
            ):
                return event
            item = event.item
            if not isinstance(item, responses.ResponseFunctionToolCall):
                return event
            if item.id is None:
                return event
            stable_id = self._item_ids.setdefault(item.call_id, item.id)
            if item.id == stable_id:
                return event
            return event.model_copy(update={"item": item.model_copy(update={"id": stable_id})})

        async def close(self) -> None:
            await self._wrapped.close()

        async def aclose(self) -> None:
            await self.close()

    class _CopilotResponsesProvider(OpenAIProvider):
        """OpenAI Responses against Copilot's client, reported under its own name."""

        @property
        def name(self) -> str:
            return "github-copilot"

    class _CopilotResponsesModel(OpenAIResponsesModel):
        """Normalize Copilot's changing streamed item IDs before Pydantic AI sees them."""

        async def _process_streamed_response(
            self,
            response: AsyncStream[responses.ResponseStreamEvent],
            model_settings: OpenAIResponsesModelSettings,
            model_request_parameters: ModelRequestParameters,
            *,
            expected_model_name: OpenAIModelName | None = None,
            expected_response_id: str | None = None,
        ) -> OpenAIResponsesStreamedResponse:
            return await super()._process_streamed_response(
                _CopilotResponsesStream(response),
                model_settings,
                model_request_parameters,
                expected_model_name=expected_model_name,
                expected_response_id=expected_response_id,
            )

    copilot = GitHubCopilotProvider(
        api_key=options.get("api_key"),
        base_url=options.get("base_url"),
    )
    return _CopilotResponsesModel(
        model_name,
        provider=_CopilotResponsesProvider(openai_client=copilot.client),
        profile=GitHubCopilotProvider.model_profile(model_name),
        # Copilot returns an empty reasoning summary unless one is requested, so no
        # ThinkingPart would reach the client. `auto` asks for the provider's summary;
        # `model_settings.openai_reasoning_summary` overrides it.
        settings=OpenAIResponsesModelSettings(openai_reasoning_summary="auto"),
    )


def configured_model(settings: LocalSettings, model_id: str | None = None) -> Model:
    """Build the model for one configured provider entry.

    Session ids read ``entry:model``. The entry decides which Pydantic AI provider is
    instantiated, so two entries may share a provider type with different endpoints and
    keys. ``infer_model`` dispatches on the provider id, so only the entry's type —
    never its name — reaches it.
    """
    settings.require_model()
    selected = model_id or settings.active_target()
    name, _, model_name = selected.partition(":")
    entry = settings.providers.get(name)
    if entry is None:
        raise ValueError(
            f"Provider {name!r} is not configured. Add it with /providers in the terminal."
        )
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
        if provider == "github-copilot" and _copilot_uses_responses(model_name, key, options):
            return _copilot_responses_model(model_name, options)
        if not options:
            return infer_model(pydantic_id)
        if provider.startswith("gateway/"):
            from pydantic_ai.providers.gateway import gateway_provider

            gateway_name = provider.removeprefix("gateway/")
            return infer_model(
                pydantic_id,
                provider_factory=lambda _name: gateway_provider(gateway_name, **options),
            )
        return infer_model(
            pydantic_id,
            provider_factory=lambda _name: infer_provider_class(provider)(**options),
        )
    except ImportError as exc:
        raise ValueError(
            f"Install the Pydantic AI provider extra for {provider!r} in the runtime "
            "environment (for example hrns[anthropic]). OpenAI-compatible providers "
            "such as DeepSeek ship with hrns."
        ) from exc
    except (ValueError, TypeError, UserError) as exc:
        raise ValueError(
            f"Cannot configure {name!r} ({provider}) with model {model_name!r}. Check the "
            "provider id, its endpoint, its key and its options in /providers."
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


def local_definition(settings: LocalSettings) -> AgentDefinition:
    """The built-in local definition: coder tools, Todo, Agent Skills, and MCP servers."""
    token_path = settings.mcp.token_store_path or str(default_token_store_path())
    bindings = [
        DefinitionBinding(
            plugin_id="hrns.coder",
            binding_id="coder",
        ),
        DefinitionBinding(
            plugin_id="hrns.client_terminal",
            binding_id="client.terminal",
            enabled=False,
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
            CLIENT,
            required=False,
            description=(
                "The connected ACP client, for bindings whose tools run there instead of here."
            ),
        ),
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
        spec=AgentSpec(instructions=_instructions()),
        resources=resources,
        bindings=bindings,
    )
    definition.fixed_capabilities.append(PrepareTools(_require_tool_approval))
    return definition


class LocalModelSource:
    """The model targets of the local config, re-read so a live session follows edits.

    Adding a provider or a custom model in the terminal must take effect without
    restarting ``hrns-local``. Every read therefore checks the config file's mtime first.
    ``thinking_models`` and ``provider_types`` are handed to the host policy and the
    session effort state, and updated in place, so a live session sees the new ids.
    """

    def __init__(self, settings: LocalSettings) -> None:
        self._settings = settings
        self._stamp = self._config_stamp()
        self._models = local_models(settings)
        self.thinking_models: dict[str, str] = local_thinking(settings)
        self.provider_types: dict[str, str] = settings.provider_ids()

    def _config_stamp(self) -> tuple[int, int] | None:
        path = self._settings.config_file
        if path is None:
            return None
        try:
            stat = path.expanduser().stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh(self) -> None:
        stamp = self._config_stamp()
        if stamp == self._stamp:
            return
        self._stamp = stamp
        path = self._settings.config_file
        try:
            settings = LocalSettings(config_file=path) if path else LocalSettings(config_file=None)
        except (ValueError, OSError):
            # A half-written or invalid file keeps the last known-good view.
            return
        self._settings = settings
        self._models = local_models(settings)
        self.thinking_models.clear()
        self.thinking_models.update(local_thinking(settings))
        self.provider_types.clear()
        self.provider_types.update(settings.provider_ids())

    def models(self) -> tuple[str, ...]:
        """The advertised ``entry:model`` ids, current as of the last config write."""
        self._refresh()
        return self._models

    def model(self, model_id: str) -> Model:
        """Resolve one advertised id; the key and endpoint follow the config file."""
        self._refresh()
        return configured_model(self._settings, model_id)


def build_local_agent(
    settings: LocalSettings,
    *,
    cwd: Path | None = None,
    model: Model | None = None,
) -> HrnsACPAgent:
    """Bind a model and trusted local workspace; workspace-changing tools need approval."""
    if os.name == "nt":
        # Fail at startup with an actionable message rather than on the first shell call.
        require_pwsh()
    workspace = (cwd or Path.cwd()).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("The local working directory must be a directory.")
    model = model if model is not None else configured_model(settings)
    definition = local_definition(settings)
    registry = PluginRegistry()
    registry.discover()
    store = build_store(str(settings.store.expanduser()))
    source = LocalModelSource(settings)
    bundle = HostBundle(
        definition=definition,
        registry=registry,
        store=store,
        close_store=True,
        model_resolver=source.model,
    )
    return HrnsACPAgent(
        bundle,
        HostPolicy(
            allowed_cwds=(str(workspace),),
            default_cwd=str(workspace),
            models=source.models(),
            default_model=settings.active_target(),
            resources={MODEL: model, WORKSPACE: str(workspace)},
            thinking_models=source.thinking_models,
            provider_types=source.provider_types,
            default_effort=thinking_from_settings(settings),
            model_settings=ModelSettings(**settings.model_settings),
        ),
        refresh_models=source.models,
    )


async def _run(settings: LocalSettings) -> None:
    await _prefetch_copilot_catalog(settings)
    agent = build_local_agent(settings)
    await agent.store.backend.initialize()
    await _serve_until_closed(agent)


def _emit_event(event: dict[str, object]) -> None:
    """One JSON line on stdout; the client renders these while the login runs."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _login(provider: str, account: str, client_id: str = "") -> int:
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

    outcome = asyncio.run(device_login(provider, on_authorization=announce, client_id=client_id))
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
    parser.add_argument(
        "--client-id",
        default="",
        metavar="ID",
        help=(
            "GitHub OAuth app for --login (default: GitHub Copilot's public client, "
            "which receives the current model catalog)"
        ),
    )
    args = parser.parse_args(argv)
    if args.catalog:
        print(json.dumps(provider_catalog(), ensure_ascii=False))
        return 0
    if args.login:
        try:
            return _login(args.login, args.account or args.login, args.client_id)
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
