# Agent servers

`AcpServer` owns agent registrations and live conversations. Register a factory once,
then mount ACP independently of the optional [AG-UI observation routes](ag_ui.md).
Factories run once per live session, including restoration after a process restart.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from hrns.server import AcpServer, build_agent

server = AcpServer()
coder = server.add_agent("coder", factory=lambda: build_agent("anthropic:claude-sonnet-5-5", cwd="/srv/project"))

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

`server.add_agent(agent_id, factory=...)` returns a typed `RegisteredAgent`. Dependency
and output types flow through its session and turn handles.

| Argument | Default | Purpose |
| --- | --- | --- |
| `agent_id` | Required | Safe route segment identifying the agent |
| `factory` | Required | Zero-argument callable returning a fresh agent per live session |
| `deps` | `None` | Default agent dependencies |
| `session_store` | Separate `InMemorySessionStore` | Persistence scoped to this registration |
| `model_resolver` | `None` | Model choices, provider routing, and session settings |
| `session_config` | `None` | Per-session dependencies, capabilities, toolsets, and workspace |
| `attachment_config` | `None` | Client toolsets (MCP, filesystem, terminal) configured on each ACP attachment |
| `permission_policy` | `None` | Permission choices for deferred approvals |
| `usage_limits` | `None` | Limits applied to agent runs |
| `tool_presenter` | `None` | Tool-call presentation for ACP updates |
| `mcp_capabilities` | `None` | Advertised client MCP transports; requires `attachment_config` |

`server.mount_acp(app, ...)` adds the WebSocket route:

| Argument | Default | Purpose |
| --- | --- | --- |
| `app` | Required | Application receiving the route |
| `prefix` | `"/acp"` | Route prefix; the endpoint is `{prefix}/{agent_id}` |
| `authenticate` | `None` | Async admission callback receiving `HTTPConnection`; `None` admits all connections |

`session_config` receives `SessionContext(agent_id, session_id, cwd)` and returns
`AcpSessionConfig(deps=..., capabilities=..., toolsets=..., workspace=...)`.
It runs once per live session and may be async. The factory and
server configuration determine the authoritative workspace and permissions.

`attachment_config` receives `AcpSession` with the outbound client, negotiated
capabilities, MCP declarations, caller/channel, session ID, and cwd. It returns
`AcpAttachmentConfig(toolsets=...)`, and runs for every attachment. Server dependencies and workspace are set by `session_config`. For client MCP, opt in explicitly:

```python
from acp import schema
from hrns.server.acp import AcpAttachmentConfig, AcpSession, acp_mcp_toolsets

def client_tools(attachment: AcpSession) -> AcpAttachmentConfig[None]:
    return AcpAttachmentConfig(toolsets=acp_mcp_toolsets(attachment))

# Pass these when registering:
# attachment_config=client_tools,
# mcp_capabilities=schema.McpCapabilities(acp=True, http=True, sse=True)
```

### Client filesystem and terminal

The same hook installs ACP's client filesystem and terminal toolsets. These are
separate from MCP: the client's `client_io` setting advertises `fs/read_text_file`,
`fs/write_text_file`, and `terminal/*`, and the agent turns them into `read_file`,
`write_file`, and `run_command`. Install them explicitly:

```python
from hrns.server.acp import (
    AcpAttachmentConfig,
    AcpSession,
    acp_filesystem,
    acp_terminal,
)

def attachment_tools(attachment: AcpSession) -> AcpAttachmentConfig[None]:
    toolsets = [
        toolset
        for toolset in (acp_filesystem(attachment), acp_terminal(attachment))
        if toolset is not None
    ]
    return AcpAttachmentConfig(toolsets=toolsets)

# Pass this when registering:
# attachment_config=attachment_tools,
```

`acp_filesystem` returns `None` unless the client advertises `fs.readTextFile`;
`acp_terminal` returns `None` unless it advertises terminal support. With
`client_io = "read"`, `write_file` falls back to a local `FileSystem` bounded by
the session `cwd` instead of the client. `acp_filesystem` defines `read_file` and
`write_file`, which collide with `Coder`'s tools, so install it on an agent without
`Coder`, or rename/filter one side. `examples/fastapi_app.py` is a working example
that has no `Coder`.

### Dispatch

Client tools refresh at model-step boundaries through `DynamicToolset`. Dispatch
validates the attachment generation before calling a client tool. MCP binding/protocol/tool errors retain their native
paths. A connection loss during dispatch reports an unknown execution outcome;
the caller must inspect the outcome before retrying a side effect.

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
store. `history()` returns committed model messages. `status()` reports
revisioned running/waiting/idle state and the last outcome.

Text and typed ACP prompt-content sequences are accepted. `submit` returns immediately
with a `TurnHandle`; execution continues independently of the `result()` waiter. Results
carry the typed output, ACP stop reason, and usage. Runtime errors raise from `result()`;
cancelled or discarded work returns a cancelled outcome.

Python and ACP prompts share one FIFO, with one active turn. Follow-ups enter history
only when started. Cancellation pauses the queue; errors also pause it. New submissions
stay queued while paused. `pending_prompts`, `resume_queue`, and `clear_queue` are
Python APIs. Clear resolves pending handles as cancelled. The terminal also keeps its
own local drafts and queue.

## ACP attachment and lifecycle

One ACP client may attach to a session. `session/new` creates and attaches;
`session/load` reuses the live agent and replays completed plus active-turn ACP updates
before responding. The current owner may reload the same session on its connection.
Loading must use the session's stored cwd. Another attached client
receives an attachment conflict.

Disconnect removes attachment resources while accepted turns and queued work remain
alive. Reconnecting clients load the session to inspect accepted work. `session/close`
explicitly cancels and closes the live session; its saved state can be loaded later.
Server shutdown rejects new work, cancels/joins active execution, and resolves queued
handles. Use one process/worker: running tasks, decisions, queues, and subscriptions live
in the server process.

Permissions and questions wait for an ACP client, then are issued after
attachment replay. Disconnect retains the decision and invalidates old replies.
A connected client lacking form elicitation gets the existing unavailable-question
outcome. Server-side tools/workspace stay configured across attachments.

The advertised `_hrns/session/state` notification and `hrns/session/state` load metadata
synchronize server-started turns. They carry `sessionId`, `revision`, `status`,
`activeTurnId`, `lastOutcome`, and `lastUsage`. The existing client handles these
alongside the prompt responses for client-started turns.

## Admission and storage

Both mounts accept `authenticate: Callable[[HTTPConnection], Awaitable[object | None]]`.
It runs before admission; `None` rejects the connection. Use the same callback for
ACP and AG-UI. All admitted identities share the registration's server scope.

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
Durable storage restores committed state after restart. Tasks and queues live in memory.

Model resolvers and provider routing belong to a registration. Changes via `providers/*`
notify all affected live sessions of that agent. Register a distinct resolver for each
agent; reusing a resolver instance across registrations is rejected.

## Stdio

```python
from hrns.server import build_agent, run_acp_stdio

run_acp_stdio(lambda: build_agent("anthropic:claude-sonnet-5-5", cwd="/srv/project"))
```

Stdio uses the same runtime and owns its lifecycle until the connection ends.
`hrns acp` and `hrns agent` use this entry point; `hrns serve` registers `agent`
and exposes `WS /acp/agent`.

## The resolver object

Choose a resolver according to how models map to providers:

| Type                       | Kind       | You implement                                          | It provides                                                              |
|----------------------------|------------|--------------------------------------------------------|--------------------------------------------------------------------------|
| `ModelResolver`            | `Protocol` | All seven methods                                      | The complete resolver contract                                    |
| `ProviderModelResolver`    | `ABC`      | `list_models`, `build_model` (optionally the settings methods) | `resolve`, empty `list_options`/`resolve_settings`, and the three provider methods via `InMemoryProviderRegistry` |
| `InMemoryProviderRegistry` | Class      | —                                                      | Provider state, validation, `current_config`                             |
| `ProviderRegistry`         | `Protocol` | —                                                      | The registry contract, if you replace the storage                        |

- **`ModelResolver`** is the full contract: `list_models`, `resolve`, `list_options`,
  `resolve_settings`, `list_providers`, `set_provider`, `disable_provider`. Implement it
  directly for custom routing, such as one model id served by several interchangeable
  gateways.
- **`ProviderModelResolver`** is the normal choice. It combines `ModelResolver` with
  `InMemoryProviderRegistry` and implements `resolve` for you: look the model up in
  `list_models`, read the config of **that choice's** `provider_id`, and hand both to
  `build_model`. Each advertised model is built with its associated provider configuration.

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
   `ProviderRoute`; credentials stay in `ProviderConfig`.
2. `providers/set(provider_id, api_type, base_url, headers)` validates that the provider
   exists and that `api_type` is in its `supported` list, then stores a `ProviderConfig`.
   Anything it rejects is a `ValueError`, which the adapter reports as `invalid_params`.
3. `providers/disable(provider_id)` sets `current=None`. A provider marked `required`
   raises `ValueError`.
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
  of `choices`; the adapter reads it through `SettingOption.default`.
- **`resolve_settings` returns a delta**, merged over `ResolvedModel.settings` and the
  agent's own settings. Keep credentials in provider configuration; the option list is
  public to the client.
- **It runs before each model request**, so a change made while a turn is running lands on
  the next request in the agent's tool loop. Keep this function fast and synchronous.
- **Raise `ValueError` for a selection you cannot apply.** The adapter also calls it at
  `set_config_option` time to validate the combination before execution.
- **A model switch may remove options.** Selections are revalidated when the model or a
  provider route changes; removed values revert to the advertised default.

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
            # Forward routing headers as request settings.
            settings=ModelSettings(extra_headers=dict(config.headers)) if config is not None else None,
        )
```

The first entry in `list_models` is each session's default model.
