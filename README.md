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

ACP exposes model selection as a standard session config option, but the adapter
only advertises it when given a list. Pass `models`; the first id is each
session's default. Use `model_resolver` when the advertised ids are not Pydantic
AI model ids:

```python
mount_acp(
    app,
    lambda _principal: build_agent(),
    models=["openai:gpt-5.1", "anthropic:claude-sonnet-4-5"],
)
```

The terminal client consumes that option for remote agents. In local mode,
`/model provider:model` saves the choice and applies it live when the runtime
advertises the id. Without an argument, `/model` still opens the picker.
`hrns-local` advertises every model configured in `hrns.local.toml`.

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
selected saved profile, including renaming it.

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

`hrns-local` reads `hrns.local.toml` (or `HRNS_LOCAL_*` environment variables) and
launches the ACP entrypoint with the configured provider and model. The existing
terminal client still uses this entrypoint.

In the TUI connection picker, `l` opens provider management. Add or edit a provider
to paste its API key into the password field; the key is stored in the OS keyring,
not in the TOML file. After saving a provider without an active model, the model
picker opens so a catalog or custom id can be selected. `/model provider:model` also
sets it directly.

## What was removed

`hrns.runtime`, `hrns.server`, `hrns.protocol`, the plugin registry, policy catalog,
manifests, SQLite/PostgreSQL session machinery, durable queue, leases, and every
`_hrns/*` extension are gone. ACP is the only backend surface, and the terminal
client speaks standard ACP only.

## Limitations

- The ACP adapter has one connection per adapter. `session/list` works for listable
  stores (including `InMemorySessionStore`), and `session/delete` works when the store
  also implements `DeletableSessionStore`; multi-viewer fan-out is not implemented.
- `build_agent` adds a small approval capability by default: reads run freely, writes and
  shell calls are marked `unapproved` so ACP can request permission. Pass
  `approval=False` to disable it.
