# Agent servers

`AcpServer` owns agent registrations and live conversations. Register a factory once,
then mount ACP independently of the optional [AG-UI observation routes](ag_ui.md).
Factories run once per live session, including restoration after a process restart.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from hrns.server import AcpServer, build_agent

server = AcpServer()
coder = server.add_agent("coder", factory=lambda: build_agent(cwd="/srv/project"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with server:
        yield

app = FastAPI(lifespan=lifespan)
server.mount_acp(app, prefix="/acp")
```

Connect to `ws://127.0.0.1:8000/acp/coder`. Each connection speaks standard ACP
JSON in WebSocket text frames. Register multiple agents under different safe IDs;
only letters, digits, underscores, and hyphens are allowed, starting with a letter
or digit. Duplicate IDs and registration after startup fail.

## Registration and configuration

`add_agent(agent_id, *, factory, deps=None, session_store=None, model_resolver=None,
session_config=None, attachment_config=None, permission_policy=None,
usage_limits=None, tool_presenter=None, mcp_capabilities=None)` returns a typed
`RegisteredAgent`. The zero-argument factory returns a fresh Pydantic AI agent.
Dependency and output types flow through the session and turn handles.

`session_config` receives `SessionContext(agent_id, session_id, cwd)` and returns
`AcpSessionConfig(deps=..., capabilities=..., toolsets=..., workspace=...)`.
It runs once per live session, without a client, and may be async. The factory and
server configuration determine the authoritative workspace and permissions.

`attachment_config` receives `AcpSession` with the outbound client, negotiated
capabilities, MCP declarations, caller/channel, session ID, and cwd. It returns
`AcpAttachmentConfig(toolsets=...)`, and runs for every attachment. It cannot replace
server dependencies or workspace. For client MCP, opt in explicitly:

```python
from acp import schema
from hrns.server.acp import AcpAttachmentConfig, AcpSession, acp_mcp_toolsets

def client_tools(attachment: AcpSession) -> AcpAttachmentConfig[None]:
    return AcpAttachmentConfig(toolsets=acp_mcp_toolsets(attachment))

# Pass these when registering:
# attachment_config=client_tools,
# mcp_capabilities=schema.McpCapabilities(acp=True, http=True, sse=True)
```

Client tools refresh at model-step boundaries through `DynamicToolset`. Dispatch
checks the attachment generation, so a departed client's call is never silently
routed to a replacement client. MCP binding/protocol/tool errors retain their native
paths. A connection loss during dispatch reports an unknown execution outcome;
side effects are never automatically replayed by the server.

## Programmatic sessions and turns

Inside the server lifecycle:

```python
session = await coder.new_session(cwd="/srv/project")
turn = await session.submit("Investigate the failure")
follow_up = await session.submit("Then inspect the migration")
result = await turn.result()
print(result.stop_reason, result.output)

await session.cancel()
queued = await session.pending_prompts()
await session.resume_queue()
await session.clear_queue()
```

`get_session(session_id)` reuses the live session or restores it from its registration's
store. `history()` returns committed Pydantic AI model messages. `status()` reports
revisioned running/waiting/idle state and the last outcome.

Text and typed ACP prompt-content sequences are accepted. `submit` returns immediately
with a `TurnHandle`; cancelling `result()`'s waiter does not cancel execution. Results
carry the typed output, ACP stop reason, and usage. Runtime errors raise from `result()`;
cancelled or discarded work returns a cancelled outcome.

Python and ACP prompts share one FIFO, with one active turn. Follow-ups enter history
only when started. Cancellation pauses the queue; errors also pause it. New submissions
stay queued while paused. `pending_prompts`, `resume_queue`, and `clear_queue` are
Python-only. Clear resolves pending handles as cancelled. There are no ACP queue
extensions, and the terminal's local drafts/queue remain separate.

## ACP attachment and lifecycle

One ACP client may attach to a session. `session/new` creates and attaches;
`session/load` reuses the live agent and replays completed plus active-turn ACP updates
before responding. The current owner may reload the same session on its connection.
Loading must use the session's stored cwd. Another attached client
is rejected; there is no takeover.

Disconnect removes attachment resources while accepted turns and queued work remain
alive. A lost JSON-RPC prompt request is never automatically resubmitted. `session/close`
explicitly cancels and closes the live session; its saved state can be loaded later.
Server shutdown rejects new work, cancels/joins active execution, and resolves queued
handles. Use one process/worker: running tasks, decisions, queues, and subscriptions are
not durable or shared between workers.

Permissions and questions wait when no ACP client is connected, then are issued after
attachment replay. Disconnect retains the decision and invalidates old replies.
A connected client lacking form elicitation gets the existing unavailable-question
outcome. Server-side tools/workspace stay configured across attachments.

The advertised `_hrns/session/state` notification and `hrns/session/state` load metadata
synchronize server-started turns. They carry `sessionId`, `revision`, `status`,
`activeTurnId`, `lastOutcome`, and `lastUsage`. The existing client handles these
without duplicating lifecycle events for its own prompts. They provide no queue controls.

## Admission and storage

Both mounts accept `authenticate: Callable[[HTTPConnection], Awaitable[object | None]]`.
It runs before admission; `None` rejects the connection. Use the same callback for
ACP and AG-UI. Identities are admission results, not factory inputs or user partitions:
all admitted users share the registration's server scope.

```python
from starlette.requests import HTTPConnection

async def authenticate(connection: HTTPConnection) -> object | None:
    return "admitted" if connection.headers.get("authorization") == "Bearer token" else None

server.mount_acp(app, prefix="/private/acp", authenticate=authenticate)
```

Each registration has a separate `InMemorySessionStore` by default. A supplied store
must be exclusively scoped to that agent. Reusing the same instance is rejected; a
shared backend requires explicitly partitioned store instances. `StoredSession.cwd`
is authoritative on restoration. `SessionStore` implements save/load;
`ListableSessionStore` adds remember/list, and `DeletableSessionStore` adds delete.
Durable storage restores committed state after restart, never live tasks or queued work.

Model resolvers and provider routing belong to a registration. Changes via `providers/*`
notify all affected live sessions of that agent. Register a distinct resolver for each
agent; reusing a resolver instance across registrations is rejected.

## Stdio

```python
from hrns.server import build_agent, run_acp_stdio

run_acp_stdio(lambda: build_agent(cwd="/srv/project"))
```

Stdio uses the same runtime and owns its lifecycle until the connection ends.
`hrns acp` and `hrns agent` use this entry point; `hrns serve` registers `agent`
and exposes `WS /acp/agent`.

## The resolver object

There are two ways to implement it. Picking the wrong one is the most common mistake here.

| Type                       | Kind       | You implement                                          | It provides                                                              |
|----------------------------|------------|--------------------------------------------------------|--------------------------------------------------------------------------|
| `ModelResolver`            | `Protocol` | All seven methods                                      | Nothing — the whole contract is yours                                    |
| `ProviderModelResolver`    | `ABC`      | `list_models`, `build_model` (optionally the settings methods) | `resolve`, empty `list_options`/`resolve_settings`, and the three provider methods via `InMemoryProviderRegistry` |
| `InMemoryProviderRegistry` | Class      | —                                                      | Provider state, validation, `current_config`                             |
| `ProviderRegistry`         | `Protocol` | —                                                      | The registry contract, if you replace the storage                        |

- **`ModelResolver`** is the full contract: `list_models`, `resolve`, `list_options`,
  `resolve_settings`, `list_providers`, `set_provider`, `disable_provider`. Implement it
  directly only when provider routing does not reduce to "each advertised model belongs
  to exactly one provider" — for example, when a single model id may be served by several
  interchangeable gateways.
- **`ProviderModelResolver`** is the normal choice. It combines `ModelResolver` with
  `InMemoryProviderRegistry` and implements `resolve` for you: look the model up in
  `list_models`, read the config of **that choice's** `provider_id`, and hand both to
  `build_model`. A model therefore cannot be built through a provider other than the one
  it is advertised with.

The data classes are the vocabulary both share:

| Type                 | Fields                                            | Meaning                                                                                       |
|----------------------|---------------------------------------------------|-----------------------------------------------------------------------------------------------|
| `ModelChoice`        | `model_id`, `provider_id`, `name`                 | One value of the `model` option. `provider_id` is required and reported as `_meta.providerId` |
| `ResolvedModel`      | `model`, `settings`                               | The model for one turn, plus settings it requires                                             |
| `ProviderDescriptor` | `provider_id`, `supported`, `required`, `current` | One provider as `providers/list` reports it; `current=None` means the client disabled it      |
| `ProviderConfig`     | `route`, `headers`                                | What a client configured for one provider; `headers` may hold secrets                         |
| `ProviderRoute`      | `api_type`, `base_url`                            | The non-secret routing that is echoed back                                                    |
| `ProviderDisabled`   | —                                                 | `ValueError` raised when a model resolves through a disabled provider                         |
| `SettingOption`      | `id`, `name`, `choices`, `category`, `description`, `meta`; `default` is read from the flagged choice | One non-model session config option a client may change |
| `SettingChoice`      | `value`, `name`, `description`, `is_default`, `meta` | One selectable value; exactly one choice per option is the default, and `meta` becomes the value's `_meta` |

### How a choice becomes a model

With `ProviderModelResolver`, one turn goes through these steps:

1. `providers/list` returns each `ProviderDescriptor`. `current` is the non-secret
   `ProviderRoute`; `headers` are never echoed.
2. `providers/set(provider_id, api_type, base_url, headers)` validates that the provider
   exists and that `api_type` is in its `supported` list, then stores a `ProviderConfig`.
   Anything it rejects is a `ValueError`, which the adapter reports as `invalid_params`.
3. `providers/disable(provider_id)` sets `current=None`. A provider marked `required`
   raises instead.
4. Each turn, the session's model id is passed to `resolve(model_id)`, which calls
   `current_config(choice.provider_id)`. A disabled provider raises `ProviderDisabled`
   before any model is built.
5. `build_model(model_id, config)` returns a `ResolvedModel`. `model` is passed to the run
   as `model=`, and `settings` becomes the base of a per-request `model_settings` callable
   that merges the session's setting selections over it.

Because step 4 repeats every turn, a new route applies from the next turn — in an existing
session too. A turn already in flight keeps the endpoint it started with.

### Session settings

Reasoning effort is the motivating case: the adapter needs the values a client may pick
and a way to turn a pick into `ModelSettings`. Those are `list_options` and
`resolve_settings` on the same resolver.

```python
from collections.abc import Mapping, Sequence

from pydantic_ai.settings import ModelSettings

from hrns.server.acp import SettingChoice, SettingOption


def list_options(self, model_id: str) -> Sequence[SettingOption]:
    return [
        SettingOption(
            id="effort",
            name="Reasoning Effort",
            category="thought_level",          # ACP category; UX hint only
            choices=[
                SettingChoice(value="default", name="Default", is_default=True),
                SettingChoice(value="off", name="Off"),
                SettingChoice(value="low", name="Low"),
                SettingChoice(value="high", name="High"),
            ],
        )
    ]


def resolve_settings(self, model_id: str, setting_selections: Mapping[str, str]) -> ModelSettings:
    value = setting_selections.get("effort")
    if value in (None, "default"):
        return ModelSettings()
    if value == "off":
        return ModelSettings(thinking=False)
    if value in {"low", "high"}:
        return ModelSettings(thinking=value)
    raise ValueError(f"Unknown reasoning effort: {value!r}")
```

Rules that make this safe:

- **`list_options` is the contract.** A client may only select what it returns; the adapter
  rejects anything else with `invalid_params`, and sends `config_option_update` when a
  provider reroute changes the list.
- **Exactly one choice is the default**, marked `is_default=True`. The default is an element
  of `choices`, so an option can never name a value it does not offer; the adapter reads it
  back through `SettingOption.default`.
- **`resolve_settings` returns a delta**, merged over `ResolvedModel.settings` and the
  agent's own settings. Never return `extra_headers` or another credential-bearing value:
  the whole option list is public to the client.
- **It runs before each model request**, so a change made while a turn is running lands on
  the next request in the agent's tool loop. Keep it cheap — no model construction, no I/O.
- **Raise `ValueError` for a selection you cannot apply.** The adapter also calls it at
  `set_config_option` time, so a bad combination fails there instead of mid-turn.
- **A model switch may remove options.** Selections are revalidated when the model or a
  provider route changes, and a value that disappeared is dropped rather than replayed.

### Example: one provider, two models

```python
from collections.abc import Sequence

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from hrns.server.acp import (
    ModelChoice,
    ProviderConfig,
    ProviderDescriptor,
    ProviderModelResolver,
    ProviderRoute,
    ResolvedModel,
)


class GatewayResolver(ProviderModelResolver):
    def __init__(self, api_key: str) -> None:
        super().__init__(
            [
                ProviderDescriptor(
                    provider_id="gateway",
                    supported=("openai",),
                    required=True,
                    current=ProviderRoute(api_type="openai", base_url="https://api.example.com"),
                )
            ]
        )
        self._api_key = api_key

    def list_models(self) -> Sequence[ModelChoice]:
        return [
            ModelChoice(model_id="fast", provider_id="gateway", name="Fast"),
            ModelChoice(model_id="smart", provider_id="gateway", name="Smart"),
        ]

    def build_model(self, model_id: str, config: ProviderConfig | None) -> ResolvedModel:
        base_url = config.route.base_url if config is not None else "https://api.example.com"
        return ResolvedModel(
            model=OpenAIChatModel(model_id, provider=OpenAIProvider(base_url=base_url, api_key=self._api_key)),
            # Headers are request settings in Pydantic AI, not provider constructor arguments.
            settings=ModelSettings(extra_headers=dict(config.headers)) if config is not None else None,
        )
```

The first entry in `list_models` is each session's default model.
