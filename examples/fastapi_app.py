"""Mount ACP on a FastAPI app.

Run with:

    DEEPSEEK_API_KEY=... uvicorn examples.fastapi_app:app --reload

Sessions default to ``deepseek-flash`` and can switch to ``deepseek-pro`` over
ACP. Provider keys stay server-side (read from ``DEEPSEEK_API_KEY``); the
client never sends an API key with a prompt.

Reasoning effort is a session config option too: the resolver advertises one
``thought_level`` select and turns the client's choice into ``thinking`` model
settings before every request, so it can change between turns — or mid-turn, on
the model request that follows a tool call.

The app also exposes one client-manageable provider, so a client can route these
models through its own OpenAI-compatible gateway with ``providers/set`` (the
terminal client's ``/providers``). That routing lives in this process only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from fastapi import FastAPI, WebSocket
from pydantic_ai.agent import AbstractAgent, Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from hrns.server.acp import (
    InMemorySessionStore,
    ModelChoice,
    ProviderConfig,
    ProviderDescriptor,
    ProviderModelResolver,
    ProviderRoute,
    ResolvedModel,
    SettingChoice,
    SettingOption,
)
from hrns.server.capabilities import AskUser, Todo
from hrns.server.fastapi import mount_acp

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# ACP model switching is opt-in: the first id is each session's default. ACP
# advertises string ids, so keep the mapping from each advertised id to the model
# name the provider wants, and build the model per run in the resolver below.
# Building it lazily keeps this module importable without ``DEEPSEEK_API_KEY``; the
# missing key surfaces when a turn actually runs.
_MODEL_NAMES = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-pro": "deepseek-v4-pro",
}

# Reasoning-effort ladder the deployment offers. ACP advertises these as one
# ``thought_level`` select; the client's choice reaches a request as the unified
# ``thinking`` setting, which Pydantic AI maps to DeepSeek's reasoning effort. The
# server owns this list: a client can only pick what is returned here.
_EFFORTS: dict[str, ModelSettings] = {
    "default": ModelSettings(),
    "off": ModelSettings(thinking=False),
    "low": ModelSettings(thinking="low"),
    "medium": ModelSettings(thinking="medium"),
    "high": ModelSettings(thinking="high"),
    "max": ModelSettings(thinking="xhigh"),
}

_EFFORT_COPY: dict[str, tuple[str, str]] = {
    "default": ("Default", "Let DeepSeek choose its default reasoning effort."),
    "off": ("Off", "Answer without reasoning; fastest and cheapest."),
    "low": ("Low", "Light reasoning for straightforward edits."),
    "medium": ("Medium", "Balanced reasoning for everyday work."),
    "high": ("High", "Deeper reasoning for harder problems."),
    "max": ("Max", "Maximum POWER"),
}

# One provider covers every model above, because they share an endpoint. It speaks
# the OpenAI protocol, so a client may point it at any OpenAI-compatible gateway.
# ``required`` says this deployment cannot run without it.
_PROVIDER_ID = "deepseek"


class ExampleModelResolver(ProviderModelResolver):
    """The example's models, plus the one provider a client may reroute.

    One object answers everything ACP asks: which models to advertise, how a selection
    resolves for a run, which settings a model offers and what they mean, and which
    providers exist and what they currently point at.
    """

    def __init__(self) -> None:
        super().__init__(
            [
                ProviderDescriptor(
                    provider_id=_PROVIDER_ID,
                    supported=("openai",),
                    required=True,
                    current=ProviderRoute(api_type="openai", base_url=_DEEPSEEK_BASE_URL),
                )
            ]
        )

    def list_models(self) -> Sequence[ModelChoice]:
        return [
            ModelChoice(model_id=model_id, name=model_id, provider_id=_PROVIDER_ID)
            for model_id in _MODEL_NAMES
        ]

    def build_model(self, model_id: str, config: ProviderConfig | None) -> ResolvedModel:
        name = _MODEL_NAMES.get(model_id)
        if name is None:
            # Unknown ids fall back to Pydantic AI's own resolver via the unchanged string.
            return ResolvedModel(model=model_id)
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if config is None:
            # The client has not configured routing, so the deployment endpoint stands.
            return ResolvedModel(
                model=OpenAIChatModel(name, provider=DeepSeekProvider(api_key=api_key))
            )
        # The client's gateway: same model, its base URL, its headers. Headers are model
        # settings in Pydantic AI, not provider arguments.
        return ResolvedModel(
            model=OpenAIChatModel(
                name, provider=OpenAIProvider(base_url=config.route.base_url, api_key=api_key)
            ),
            settings=ModelSettings(extra_headers=dict(config.headers)) if config.headers else None,
        )

    def list_options(self, model_id: str) -> Sequence[SettingOption]:
        """Advertise the deployment's effort ladder for every model it serves.

        The list is the contract: a client may only select these values, which is how a
        server keeps costlier or unsupported levels out without shipping a new client.
        """
        if model_id not in _MODEL_NAMES:
            return ()
        return [
            SettingOption(
                id="effort",
                name="Reasoning Effort",
                category="thought_level",
                description="How much the model reasons before it answers.",
                choices=[
                    SettingChoice(
                        value=value,
                        name=name,
                        description=description,
                        is_default=value == "default",
                    )
                    for value, (name, description) in _EFFORT_COPY.items()
                ],
            )
        ]

    def resolve_settings(
        self, model_id: str, setting_selections: Mapping[str, str]
    ) -> ModelSettings:
        """Turn the session's effort choice into a settings delta for each request."""
        value = setting_selections.get("effort")
        if value is None:
            return ModelSettings()
        settings = _EFFORTS.get(value)
        if settings is None:
            raise ValueError(f"Unknown reasoning effort: {value!r}")
        return settings


resolver = ExampleModelResolver()


async def authenticate(connection: WebSocket) -> str | None:
    """Return the caller's identity, or ``None`` to refuse the connection."""
    token = connection.headers.get("authorization")
    if token == "Bearer example-token":
        return "example-user"
    return None


def agent_factory(principal: str | None) -> AbstractAgent[None, str]:
    # Use ``principal`` to bind identity into the agent's tools or deps.
    # Todo emits native ACP plan updates for the client's Todos panel; AskUser
    # travels as a form elicitation whenever the client advertises one.
    return Agent(capabilities=[Todo(), AskUser()])


# ACP session stores keyed by principal. A user reconnecting with the same
# identity can load their own sessions; another user's store is never consulted.
# This is process-local: use shared durable storage when running several workers.
_session_stores: dict[str, InMemorySessionStore] = {}


def session_store_factory(principal: str | None) -> InMemorySessionStore:
    key = principal or "anonymous"
    return _session_stores.setdefault(key, InMemorySessionStore())


app = FastAPI(title="hrns example")

# One fresh ACP adapter per authenticated WebSocket connection. Sessions are
# saved into that principal's store, so ``session/load`` cannot cross users. The
# provider registry is shared by every connection: a route one client sets is the
# routing the next connection sees.
mount_acp(
    app,
    agent_factory,
    path="/acp",
    authenticate=authenticate,
    session_store_factory=session_store_factory,
    model_resolver=resolver,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("examples.fastapi_app:app", host="0.0.0.0", port=8000)
