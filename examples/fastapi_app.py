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

Two skills are defined in Python rather than on disk: a deployment can ship a fixed
skill library with the app, and the client sees them in ``/skills`` exactly like
discovered ones. The agent loads one itself with ``load_capability``, or a person
writes ``$release-notes`` in the composer.

The agent also installs the ACP client filesystem and terminal toolsets. A client
that connects with ``client_io = "all"`` (the terminal's default) gives it
``read_file``, ``write_file``, and ``run_command`` against the client's machine;
``"read"`` advertises only reads, and ``"off"`` gives it no client tools. This
example has no server-side ``Coder``, so those are its only file and command tools.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic_ai.agent import AbstractAgent, Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai_skills import Skill, SkillResource
from starlette.requests import HTTPConnection

from hrns.server import HrnsServer
from hrns.server.acp import (
    AcpAttachmentConfig,
    AcpSession,
    ModelChoice,
    ProviderConfig,
    ProviderDescriptor,
    ProviderModelResolver,
    ProviderRoute,
    ResolvedModel,
    SessionContext,
    SettingChoice,
    SettingOption,
    acp_filesystem,
    acp_terminal,
)
from hrns.server.capabilities import AskUser, Skills, Todo

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


#: Hard-coded skills, shipped with the app rather than discovered on disk. A deployment
#: can bake its house workflows into the binary this way; the client can list and invoke
#: them, and the model can load them through ``load_capability``.
_RELEASE_NOTES = Skill(
    name="release-notes",
    description=(
        "Write release notes from a list of merged changes, grouped by user impact and "
        "using the team's house style."
    ),
    content="""# Release notes

Turn the changes you are given into notes a customer can read.

1. Group changes under **New**, **Improved**, and **Fixed**. Leave out a group with no entries.
2. Write each line in the present tense, from the user's point of view: "Sessions survive a
   reconnect", not "Fixed a bug in the reconnect handler".
3. Name the affected surface (terminal, server, API) when a reader could not guess it.
4. End with a one-sentence upgrade note if anything requires an action.

Follow the style guide resource when a rule here is ambiguous.
""",
    resources=[
        SkillResource(
            name="style-guide",
            content="""# Style guide

- One sentence per change, at most 120 characters.
- No internal ticket ids, branch names, or author names.
- Prefer "you" over "the user".
- Say what changed, not how it was implemented.
""",
        )
    ],
)

_INCIDENT_REVIEW = Skill(
    name="incident-review",
    description=(
        "Run a blameless incident review: build a timeline, separate trigger from cause, "
        "and produce follow-up actions with owners."
    ),
    content="""# Incident review

1. Build a timeline from the first signal to recovery, with timestamps and sources.
2. Separate the **trigger** (what changed) from the **causes** (why the system allowed
   it to matter). Name the missing safeguard, not the person.
3. List what went well: detection, rollback, communication.
4. Produce follow-up actions. Each one needs an owner, a due date, and a check that
   proves it worked.
5. State what would have caught this earlier, and whether it is worth the cost.
""",
)

#: The capability the example ships: only the two programmatic skills, no disk discovery.
_EXAMPLE_SKILLS = Skills(skills=(_RELEASE_NOTES, _INCIDENT_REVIEW))

resolver = ExampleModelResolver()


async def authenticate(connection: HTTPConnection) -> str | None:
    """Return the caller's identity, or ``None`` to refuse the connection."""
    token = connection.headers.get("authorization")
    if token == "Bearer example-token":
        return "example-user"
    return None


def agent_factory(session: SessionContext) -> AbstractAgent[None, str]:
    # Todo emits native ACP plan updates for the client's Todos panel; AskUser
    # travels as a form elicitation whenever the client advertises one. Skills
    # advertises the two programmatic skills on ``session/new`` and expands a
    # ``$release-notes`` reference without a model round trip.
    return Agent(capabilities=[Todo(), AskUser(), _EXAMPLE_SKILLS])


def attachment_config(session: AcpSession) -> AcpAttachmentConfig[None]:
    """Add the client's filesystem and terminal as tools for this attachment.

    ``client_io`` only advertises ACP ``fs/`` and ``terminal/`` capabilities. They
    become model tools only when the agent installs the matching toolsets, which is
    why this hook exists: the factory above has no ``Coder``, so these are the
    agent's only file and command tools. ``client_io = "read"`` yields ``read_file``;
    ``"all"`` adds client-backed ``write_file`` and ``run_command``.
    """
    toolsets = [
        toolset
        for toolset in (acp_filesystem(session), acp_terminal(session))
        if toolset is not None
    ]
    return AcpAttachmentConfig(toolsets=toolsets)


server = HrnsServer()
coder = server.add_agent(
    "coder",
    factory=agent_factory,
    model_resolver=resolver,
    attachment_config=attachment_config,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with server:
        yield


app = FastAPI(title="hrns example", lifespan=lifespan)
server.mount_acp(app, prefix="/acp", authenticate=authenticate)
server.mount_ag_ui(app, prefix="/ag-ui", authenticate=authenticate)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("examples.fastapi_app:app", host="0.0.0.0", port=8000)
