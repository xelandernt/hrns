# hrns

**One Pydantic AI agent. Two protocol adapters. No custom backend.**

hrns builds a Pydantic AI `Agent` from capabilities and exposes it over either:

- **ACP** (`pydantic-ai-harness`), for terminals and editors.
- **AG-UI** (`pydantic-ai`), for web and multi-client frontends.

Both adapters can be mounted on the same FastAPI app.

## Install

```sh
uv sync --extra app
```

ACP support comes from `pydantic-ai-harness[acp]`; AG-UI comes from
`pydantic-ai-slim[ag-ui]`.

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
from pydantic_ai_harness.experimental.acp import InMemorySessionStore, run_acp_stdio_sync

run_acp_stdio_sync(build_agent(), session_store=InMemorySessionStore())
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
mount_acp(app, lambda: build_agent(), path="/acp")
```

Each WebSocket gets its own `PydanticAIACPAgent` and session namespace. The upstream
adapter is stdio-oriented and single-connection, so the FastAPI mount bridges each
WebSocket text frame to the adapter's newline-delimited JSON stream. Shared multi-viewer
ACP sessions still need a fan-out layer above it.

## AG-UI on FastAPI

```python
from hrns.app.fastapi import mount_agui

mount_agui(app, build_agent(), path="/agui")
```

`POST /agui` accepts a `RunAgentInput` and returns an AG-UI event stream. The Pydantic
adapter is per-request: it does not own a thread registry or multi-client fan-out.

## Both protocols in one app

```python
from hrns.app.fastapi import create_app

app = create_app(build_agent())
# WS /acp
# POST /agui
```

Or run it:

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app serve --port 8000
```

## Adapter API

```python
from hrns.app import PydanticAIACPAgent, PydanticAIAGUIAgent, build_agent

agent = build_agent("openai:gpt-6.1-sol")

acp = PydanticAIACPAgent(agent, session_store=InMemorySessionStore())
agui = PydanticAIAGUIAgent(agent)
```

The adapters are deliberately thin. They translate protocol events; they do not
define agent behavior.

## Local configuration

`hrns-local` reads `hrns.local.toml` (or `HRNS_LOCAL_*` environment variables) and
launches the ACP entrypoint with the configured provider and model. The existing
terminal client still uses this entrypoint.

## What was removed

`hrns.runtime`, `hrns.server`, `hrns.protocol`, the plugin registry, policy catalog,
manifests, SQLite/PostgreSQL session machinery, durable queue, leases, and every
`_hrns/*` extension are gone. ACP and AG-UI are the only backend surfaces, and the
terminal client speaks standard ACP only.

## Limitations

- The upstream ACP adapter has one connection per adapter and two store methods
  (`save`/`load`); there is no built-in session list, delete, or multi-viewer fan-out.
- The AG-UI adapter is per-run/request; multi-client thread management is an
  application concern.
- `build_agent` adds a small approval capability by default: reads run freely, writes and
  shell calls are marked `unapproved` so ACP can request permission. Pass
  `approval=False` to disable it.
