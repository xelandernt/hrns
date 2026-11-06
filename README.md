# hrns

**One Pydantic AI agent. One protocol adapter. No custom backend.**

hrns builds a Pydantic AI `Agent` from capabilities and exposes it over **ACP**,
for terminals and editors.

## Install

```sh
uv sync --extra app
```

ACP support is implemented in `hrns.app.acp`; `pydantic-ai-harness` supplies the
coding capabilities.

## Define the agent

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai_harness.coder import Coder


def build_agent(model: str = "anthropic:claude-sonnet-5-5") -> Agent:
    return Agent(
        model,
        name="hrns",
        capabilities=[
            LocalWorkspace("."),
            Coder(),
        ],
    )
```

Everything the agent does is a capability. There is no plugin registry,
binding, policy catalog, manifest, or configuration service.

## ACP over stdio

```python
from hrns.app.acp import InMemorySessionStore, run_acp_stdio

run_acp_stdio(build_agent(), session_store=InMemorySessionStore())
```

Or use the packaged entrypoint:

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app acp
```

## ACP WebSocket on FastAPI

```python
from fastapi import FastAPI
from hrns.app.fastapi import mount_acp

app = FastAPI()
mount_acp(app, lambda _principal: build_agent(), path="/acp")
```

Each WebSocket gets its own `PydanticAIACPAgent`. The adapter is stdio-oriented
and single-connection, so the FastAPI mount bridges each WebSocket text frame to
the adapter's newline-delimited JSON stream. Shared multi-viewer ACP sessions
still need a fan-out layer above it.

### ACP model switching

ACP exposes model selection as a standard session config option, but the adapter only
advertises it when given a `model_resolver`. The resolver returns the models to advertise;
the first is each session's default:

```python
from hrns.app.acp import ModelChoice, ProviderConfig, ProviderModelResolver, ResolvedModel


class Models(ProviderModelResolver):
    def list_models(self) -> list[ModelChoice]:
        return [
            ModelChoice(model_id="openai:gpt-5.1", provider_id="openai"),
            ModelChoice(model_id="anthropic:claude-sonnet-4-5", provider_id="anthropic"),
        ]

    def build_model(self, model_id: str, config: ProviderConfig | None) -> ResolvedModel:
        # A bare id lets Pydantic AI construct the model from its own provider defaults.
        return ResolvedModel(model=model_id)


mount_acp(app, lambda _principal: build_agent(), model_resolver=Models())
```

The same resolver carries the provider configuration below, so a client-configured route
is what the next `resolve` builds.

The terminal client consumes that option for remote agents. In local mode,
`/model provider:model` saves the choice and applies it live when the runtime
advertises the id. Without an argument, `/model` still opens the picker.
`hrns-local` advertises every model configured in the shared configuration document
(`config.toml`; see [Configuration](docs/configuration.md)).

The terminal client can connect to that endpoint directly:

```sh
hrns connect --server ws://127.0.0.1:8000/acp
hrns run --server ws://127.0.0.1:8000/acp "summarize this repo"
```

`ws://` is accepted only for loopback development; remote hosts need `wss://`. Pass a
bearer token with `--token env:NAME` (or `keyring:service/user`), or save the endpoint:

```sh
hrns profiles add team --server wss://host/acp --token env:TEAM_TOKEN --cwd /workspace
hrns run --profile team "summarize this repo"
```

`--token` also accepts a literal token, and the TUI can connect directly:

```text
/connect wss://host/acp --token env:TEAM_TOKEN --cwd /workspace
```

In the TUI connection picker, `n` adds a remote or stdio target and `e` edits the
selected saved profile, including renaming it. A bare `hrns` reopens the connection you
used last — the local model, or a saved profile by name — and only falls back to the
picker, with the reason in its status line, when that connection is gone or unreachable.

Prefer `env:` or `keyring:` references: literal tokens are stored in the profile file
when a profile is saved.

## Run the FastAPI app

```python
from hrns.app.fastapi import create_app

app = create_app(lambda _principal: build_agent())
# WS /acp
```

Or run it:

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app serve --port 8000
```

## Authentication

`mount_acp` and `create_app` accept an async `authenticate` callback. It receives
the raw FastAPI `WebSocket` and returns an identity. Returning `None` rejects the
peer: ACP closes before the upgrade is accepted. Exceptions are server errors and
never produce an accepted connection.

```python
from fastapi import WebSocket
from hrns.app.fastapi import create_app

async def authenticate(connection: WebSocket) -> str | None:
    token = connection.headers.get("authorization")
    return user_for_token(token) if token else None

app = create_app(lambda principal: build_agent(), authenticate=authenticate)
```

The agent factory always takes exactly one argument: the principal, or `None`
when no callback is configured. It runs once per accepted ACP connection. The
principal type is generic, so the callback can return whatever identity object
the deployment uses and the factory receives that same type.

### Per-user ACP sessions

ACP stores sessions in a `SessionStore`. Give each principal their own store so a
reconnecting user can `session/load` their sessions without ever seeing another
user's:

```python
from hrns.app.acp import InMemorySessionStore
from hrns.app.fastapi import create_app

stores: dict[str, InMemorySessionStore] = {}

def session_store_for(principal: str | None) -> InMemorySessionStore:
    return stores.setdefault(principal or "anonymous", InMemorySessionStore())

app = create_app(
    lambda principal: build_agent(),
    authenticate=authenticate,
    session_store_factory=session_store_for,
)
```

`InMemorySessionStore` is process-local. Use a shared durable implementation for
multiple workers or restarts.

`InMemorySessionStore` also implements `session/list` and `session/delete`: clients can
list sessions, filter by `cwd`, page with the returned cursor, and delete sessions.
A title is the first user text in the session transcript, and `updatedAt` advances
whenever a turn is committed. A durable store opts into the same support by
implementing `ListableSessionStore` (`save`, `load`, `remember`, and `list_sessions`)
and `DeletableSessionStore` (`delete`) from `hrns.app.acp`.

### Client-managed providers

A client can point the agent's LLM traffic at its own gateway with `providers/list`,
`providers/set`, and `providers/disable`. The adapter advertises those methods only when
you pass a `model_resolver`, and it does not own the routing: one `ModelResolver` supplies
the models, resolves a selection for a run, and holds the provider state a client changes,
so a configured route is what the next run uses.

```python
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.settings import ModelSettings

from hrns.app.acp import (
    ModelChoice,
    ProviderConfig,
    ProviderDescriptor,
    ProviderModelResolver,
    ProviderRoute,
    ResolvedModel,
    create_acp_agent,
)


class AnthropicResolver(ProviderModelResolver):
    def __init__(self) -> None:
        super().__init__(
            [
                ProviderDescriptor(
                    provider_id="main",
                    supported=("anthropic",),
                    required=True,
                    current=ProviderRoute(
                        api_type="anthropic", base_url="https://api.anthropic.com"
                    ),
                )
            ]
        )

    def list_models(self) -> list[ModelChoice]:
        return [ModelChoice(model_id="claude-sonnet-4-5", provider_id="main")]

    def build_model(self, model_id: str, config: ProviderConfig | None) -> ResolvedModel:
        base_url = (
            config.route.base_url if config is not None else "https://api.anthropic.com"
        )
        return ResolvedModel(
            model=AnthropicModel(model_id, provider=AnthropicProvider(base_url=base_url)),
            settings=ModelSettings(extra_headers=dict(config.headers))
            if config is not None
            else None,
        )


adapter = create_acp_agent(agent, model_resolver=AnthropicResolver())
```

`ProviderModelResolver` implements `resolve` for you: it looks the model up in
`list_models`, reads the config of **that model's** provider, and hands both to
`build_model`. A model therefore cannot be built through a provider other than the one it
is advertised with, and `current_config` raises `ProviderDisabled` before any model is
built once a client disables it. A subclass only declares the catalog and how one model
is constructed from its provider.

`ModelChoice.provider_id` is reported as `_meta.providerId` on the model config option.
That is how a client learns which provider serves which model: ACP's `providers/*` payload
reports endpoints, not the models behind them. Leaving it `None` keeps the option
untagged, and clients that ignore `_meta` still see a plain selector.

Provider configuration is process-scoped and in memory; it is never written to disk or
saved with a session. `providers/list` reports non-secret routing only, so headers a
client sent are never echoed back. A model is resolved for every turn, so a new route
applies from the next turn — in an existing session too; a turn already in flight keeps
the endpoint it started with.

The bundled terminal client drives this with `/providers` when the connected agent
advertises the capability: pick a provider, press `s` to send `providers/set`, or `d` to
send `providers/disable`. The table's `Models` column lists the models the agent tags with
that provider id, and `/models` labels each choice the same way, so you can see which
provider a model runs through before switching. For a local `hrns-local` connection
`/providers` keeps opening the config-file editor instead, because that is where its
provider entries come from.

## Adapter API

```python
from hrns.app import PydanticAIACPAgent, build_agent
from hrns.app.acp import InMemorySessionStore

agent = build_agent("openai:gpt-6.1-sol")
acp = PydanticAIACPAgent(agent, session_store=InMemorySessionStore())
```

The adapter is deliberately thin. It translates protocol events; it does not
define agent behavior.

## Local configuration

The client and the local runtime read one document: `config.toml` in the platform
config directory, with an optional `hrns.toml` in the working directory overriding it
key by key. `HRNS_LOCAL_*` environment variables override both.
[Configuration](docs/configuration.md) is the field reference and the location map.

`hrns-local` launches the ACP entrypoint with the configured provider and model. The
terminal client reuses that entrypoint, so it passes the same document to it.

In the TUI connection picker, `l` opens provider management. Add or edit a provider
to paste its API key into the password field; the key is stored in the OS keyring,
not in the TOML file. After saving a provider without an active model, the model
picker opens so a catalog or custom id can be selected. `/model provider:model` also
sets it directly.

`/providers` acts on whatever you are connected to. Over stdio to `hrns-local` it opens
the entry editor above; connected to a remote agent that advertises provider
configuration it lists that agent's providers and sends `providers/set` /
`providers/disable` for you.

`hrns-local` seeds its ACP provider registry from those TOML entries, so a client that
speaks `providers/*` sees one provider per entry and can reroute it to its own gateway.
The entry this deployment starts with is marked `required`, because disabling it would
leave the default session without a model; the headers a client sends reach the request
as `extra_headers` model settings.

## What was removed

`hrns.runtime`, `hrns.server`, `hrns.protocol`, the plugin registry, policy catalog,
manifests, SQLite/PostgreSQL session machinery, durable queue, leases, and every
`_hrns/*` extension are gone. ACP is the only backend surface, and the terminal
client speaks standard ACP only.

## Limitations

- The ACP adapter has one connection per adapter. `session/list` works for listable
  stores (including `InMemorySessionStore`), and `session/delete` works when the store
  also implements `DeletableSessionStore`; multi-viewer fan-out is not implemented.
- Provider configuration is in memory and applies from the next model resolution, which
  is the next turn; a turn already in flight is unaffected. MCP-over-ACP
  (`mcp/message`) and next edit suggestions (`nes/*`) remain unimplemented and are not
  advertised.
- `build_agent` adds a small approval capability by default: reads run freely, writes and
  shell calls are marked `unapproved` so ACP can request permission. Pass
  `approval=False` to disable it.
