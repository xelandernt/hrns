# Harness

**Define an agent once. Run it in Python, open it in a terminal, or host it remotely.**

Harness combines native Pydantic AI capabilities with policy-controlled sessions, an independent ACP terminal, and authenticated remote hosting. Agent behavior stays in ordinary Python; the terminal can also connect to standard ACP agents that do not use this runtime.

## Start developing

Requires **Python 3.11+**, macOS or Linux, and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked --all-extras
uv run ruff check .
```

The checked-in lockfile pins the tested upstream versions.

## Use a local agent with your model

The local developer agent is the default connection mode. Install the provider
extra and set the API key for the provider you use:

```sh
uv sync --locked --extra client --extra runtime --extra openai
export DEEPSEEK_API_KEY='your-key'
uv run hrns
```

Press **l** to configure a provider and model, type `/models` in a session, or create
`harness.local.toml` in the project:

```toml
provider = "deepseek"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"
```

`uv run hrns` starts a session in the current directory using that model. The agent
works on files and runs commands with approval prompts. Configuration uses
pydantic-settings. See [local setup](docs/LOCAL.md) for OpenAI-compatible endpoints,
local inference, other Pydantic AI providers, and headless execution.

Useful slash commands include `/connect` (switch or add a target), `/models`
(configure or select the local model), `/effort` (change reasoning effort live),
`/plugins` (toggle, configure, or add capabilities), and `/agents` (inspect
subagents). Typing `/` shows an inline filtered list. The older `/model` and
`/capabilities` names remain aliases.

Any independent ACP agent can still be launched explicitly:

```sh
uv run hrns connect --stdio -- /path/to/agent --acp
```

See [client usage](docs/CLIENT_API.md) for headless execution, profiles, approvals
and the bridge.

## Serve agents in your FastAPI app

```python
from fastapi import FastAPI
from harness.runtime import SQLiteSessionStore
from harness.server import AgentServer

app = FastAPI()
agents = AgentServer(store=SQLiteSessionStore("./sessions.sqlite3"))
agents.add_agent(definition)
agents.mount(app)
```

Install the `server` extra, register your `AgentDefinition` objects, and run the app
with Uvicorn. Connect using `hrns connect --url http://127.0.0.1:8080 --agent AGENT_ID`.
PostgreSQL and optional FastAPI authentication dependencies use the same API.
See [server embedding](docs/SERVER.md) and [the runnable example](examples/fastapi_app.py).

## Layout

Harness is one distribution with optional extras. Protocol and local settings
ship by default; the runtime, terminal, and server dependencies are opt-in.

| Module | Extra | Purpose |
| --- | --- | --- |
| `harness.protocol` | base | Versioned extension schemas and transport contracts |
| `harness.config` | base | Shared local settings using pydantic-settings |
| `harness.runtime` | `runtime` | Native agent definitions, plugin management, sessions, `harness-host` and `harness-local` |
| `harness.client` | `client` | Textual terminal, scripted ACP runner and stdio bridge |
| `harness.server` | `server` | FastAPI agent registration and in-process WebSocket ACP |

Install only what you need, for example `pip install 'harness[client]'`,
`pip install 'harness[openai]'`, or `pip install 'harness[all]'`.

The `harness.client` module does not import Pydantic AI or installed agent
plugins, and installing `harness[client]` alone does not install the runtime.
Local stdio agents run in a separate process. Network agents run inside an existing
FastAPI application through `AgentServer`, sharing an explicit SQLite or PostgreSQL store.

## Design essentials

- **Upstream ACP execution:** `PydanticAIACPAgent` owns prompts, streaming, approvals, cancellation, history, and transcript replay.
- **Native composition:** managed bindings contribute their complete Pydantic AI behavior—tools, instructions, hooks and settings—and are selected when a session is composed.
- **Live model and effort:** both use standard ACP configuration options and apply from the next request.
- **Host-owned state:** sessions pin their principal/deployment scope, retain history and metadata, and never silently retry an uncertain prompt.
- **Explicit resources:** workspace and provider bindings belong to the host. A local workspace is a path, not a sandbox.
- **Standard ACP first:** generic streaming content, tools and approvals work without project-specific extensions.

## Documentation

- [Implementation plan and task checklist](PLAN.md)
- [Architecture](docs/ARCHITECTURE.md) and [shared contracts](docs/CONTRACTS.md)
- [Extension protocol and wire examples](docs/PROTOCOL.md)
- [Runtime Python API](docs/RUNTIME_API.md)
- [ACP host](docs/ACP.md) and [client usage](docs/CLIENT_API.md)
- [Embed agents in FastAPI](docs/SERVER.md)
- [Capability catalog and integration coverage](docs/CATALOG.md)
- [Workspace lifecycle](docs/WORKSPACES.md)
- [Security model](docs/SECURITY.md)
- [Concept compliance matrix](docs/COMPLIANCE.md)
- [Verification and support boundaries](docs/VERIFICATION.md)

## Checks and distribution builds

```sh
uv run ruff format --check src examples test.py
uv run ruff check .
uv run pyrefly check src examples --no-progress-bar
uv build
```

Or run `just check` with [just](https://just.systems/) installed. CI exercises the same checks on macOS/Linux and Python 3.11–3.13. A wheel and source distribution are written to `dist/`; optional dependencies are declared as extras.

External provider, sandbox and durability integrations need their upstream extras and credentials. See the coverage matrix for managed, locally verified and deployment-only distinctions.

## License

MIT. See [LICENSE](LICENSE).
