# `mount_acp`

`mount_acp` exposes an ACP agent over a FastAPI WebSocket endpoint.

```python
from hrns.server.fastapi import mount_acp

def mount_acp(
    app: FastAPI,
    agent_factory: AgentFactory[Principal],
    *,
    path: str = "/acp",
    authenticate: Authenticator[Principal] | None = None,
    session_store_factory: SessionStoreFactory[Principal] | None = None,
    model_resolver: ModelResolver | None = None,
) -> None
```

The three callable types are plain aliases:

```python
AgentFactory = Callable[[Principal | None], AbstractAgent[None, str]]
Authenticator = Callable[[WebSocket], Awaitable[Principal | None]]
SessionStoreFactory = Callable[[Principal | None], SessionStore]
```

`Principal` is whatever identity your `authenticate` callback returns; `None` means "no
authenticator configured". Every argument after the factory is a keyword argument.

## Arguments

| Argument                | Default  | Role                                                      |
|-------------------------|----------|-----------------------------------------------------------|
| `app`                   | required | The FastAPI app to add a WebSocket route to               |
| `agent_factory`         | required | Builds one agent per accepted connection                  |
| `path`                  | `"/acp"` | WebSocket route path                                      |
| `authenticate`          | `None`   | Admits or refuses the peer before the handshake completes |
| `session_store_factory` | `None`   | Chooses the session store for one principal               |
| `model_resolver`        | `None`   | Enables model switching and `providers/*`                 |

### `app`

The route is registered when `mount_acp` is called, so call it before the server starts
(module import is the normal place). Mount one handler per app: two mounts on the same
path do not raise, the first one simply shadows the second.

```python
app = FastAPI()
mount_acp(app, agent_factory)                 # WS /acp
```

### `agent_factory`

Called **once per accepted WebSocket**, after authentication, with the principal.
Return a freshly built agent — do not share one between connections. The harness
capabilities hold per-agent state, and the ACP adapter binds session state to the agent
it wraps.

```python
def agent_factory(principal: str | None) -> AbstractAgent[None, str]:
    return build_agent(cwd=f"/srv/workspaces/{principal or 'anonymous'}")
```

The return type is `AbstractAgent[None, str]`, i.e. a Pydantic AI agent with no
dependencies and a string output. Use `principal` for anything that should differ per
user: the workspace root, deps, read-only tools, or a model.

### `path`

```python
mount_acp(app, agent_factory, path="/agent/acp")
```

The endpoint speaks ACP over the WebSocket's **text** frames: one JSON message per frame,
encoded as UTF-8, with no embedded newlines. The terminal client connects to it with
`hrns connect --server ws://127.0.0.1:8000/acp`.

### `authenticate`

Runs **before** `websocket.accept()`, so a rejected peer never completes the handshake.

```python
from fastapi import WebSocket

async def authenticate(connection: WebSocket) -> str | None:
    token = connection.headers.get("authorization", "")
    return token.removeprefix("Bearer ") or None

mount_acp(app, agent_factory, authenticate=authenticate)
```

Three outcomes:

| Outcome             | Effect                                                                                    |
|---------------------|-------------------------------------------------------------------------------------------|
| Returns a principal | Connection is accepted; the principal reaches `agent_factory` and `session_store_factory` |
| Returns `None`      | Connection is closed with code `1008`; the factory is never called                        |
| Raises              | Connection is closed with code `1011` and the exception propagates                        |

Omitting `authenticate` admits every peer and passes `None` as the principal.

### `session_store_factory`

Chooses the store for one principal. Sessions in one store are invisible to every other
principal, so a reconnecting user can `session/load` their own work and only their own.

```python
from hrns.server.acp import InMemorySessionStore

_stores: dict[str, InMemorySessionStore] = {}

def session_store_for(principal: str | None) -> InMemorySessionStore:
    return _stores.setdefault(principal or "anonymous", InMemorySessionStore())

mount_acp(app, agent_factory, session_store_factory=session_store_for)
```

Omitting it gives every connection a fresh `InMemorySessionStore`: sessions work for the
life of that connection, and `session/load` cannot reach a session from an earlier one.

What a store enables depends on which interface it implements:

| Interface               | Methods                       | Enables          |
|-------------------------|-------------------------------|------------------|
| `SessionStore`          | `save`, `load`                | `session/load`   |
| `ListableSessionStore`  | `+ remember`, `list_sessions` | `session/list`   |
| `DeletableSessionStore` | `+ delete`                    | `session/delete` |

`InMemorySessionStore` implements all three. It is process-local: use a shared durable
implementation when you run several workers or want sessions to survive a restart.

### `model_resolver`

Supplies the models a client may switch between, resolves one for each turn, and owns the
provider routing a client changes through `providers/*`.

```python
mount_acp(app, agent_factory, model_resolver=MyResolver())
```

One resolver is shared by every connection and for the life of the process: a route one
client sets is the route the next connection sees. Configuration is in memory only, as
ACP's provider RFD requires.

Omitting it removes the `model` and setting session config options and makes
`providers/list`, `providers/set`, and `providers/disable` answer `method_not_found`; each
session then uses the model the agent itself was built with.

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

## All together

The pieces only fit together in one place: the principal from `authenticate` reaches both
the factory and the store factory, while the resolver is shared by every connection.

```python
from fastapi import FastAPI, WebSocket
from pydantic_ai.agent import AbstractAgent

from hrns.server import build_agent
from hrns.server.acp import InMemorySessionStore
from hrns.server.fastapi import mount_acp

app = FastAPI()
_stores: dict[str, InMemorySessionStore] = {}

async def authenticate(connection: WebSocket) -> str | None:
    token = connection.headers.get("authorization", "")
    return token.removeprefix("Bearer ") or None

def agent_factory(principal: str | None) -> AbstractAgent[None, str]:
    return build_agent(cwd=f"/srv/workspaces/{principal or 'anonymous'}")

def session_store_for(principal: str | None) -> InMemorySessionStore:
    return _stores.setdefault(principal or "anonymous", InMemorySessionStore())

mount_acp(
    app,
    agent_factory,
    authenticate=authenticate,
    session_store_factory=session_store_for,
    model_resolver=GatewayResolver(api_key=...),
)
```

A complete runnable version is in [`examples/fastapi_app.py`](https://github.com/xelandernt/hrns/blob/main/examples/fastapi_app.py).

## Related entry points

| Entry point | Use it when |
| --- | --- |
| `mount_acp(app, factory, ...)` | You already have a FastAPI app |
| `create_app(factory, ...)` | You want the packaged app; it forwards `authenticate`, `session_store_factory`, and `model_resolver`, and mounts at the default path |
| `create_acp_agent(agent, session_store=..., model_resolver=...)` | You are not on FastAPI and drive the adapter yourself |
| `run_acp_stdio(agent, ...)` | You want ACP over stdio instead of WebSocket |

`create_app` is the one-liner equivalent:

```python
from hrns.server.fastapi import create_app

app = create_app(agent_factory, authenticate=authenticate)  # WS /acp
```
