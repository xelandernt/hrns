# Harness

**Define an agent once. Run it in Python, open it in a terminal, or host it remotely.**

Harness combines native Pydantic AI capabilities with policy-controlled sessions, an independent ACP terminal, and authenticated remote hosting. Agent behavior stays in ordinary Python; the terminal can also connect to standard ACP agents that do not use this runtime.

## Start developing

Requires **Python 3.11+**, macOS or Linux, [uv](https://docs.astral.sh/uv/),
and [just](https://just.systems/).

```sh
just install
just check
```

The checked-in lockfile pins the tested upstream versions. `just install` also
installs the [prek](https://prek.j178.dev/) Git hook, which runs `just check` before
each commit without applying automatic fixes. To install the hook after a manual
dependency sync, run `uv run prek install`.

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

`uv run hrns` starts a session in the current directory using that model. The bundled
`harness.coder` capability gives it file tools, content search, and a shell in that
directory: reads are automatic, while writes, edits, and commands ask for approval first.
`harness.ask_user` lets the model ask structured multiple-choice questions in the terminal;
disable it with `[capabilities] ask_user = false` for headless runs. `harness.todo` gives
the model a session-scoped plan and shows it in the **Todos** tab of the right drawer.
Configuration uses pydantic-settings. See
[local setup](docs/LOCAL.md) for OpenAI-compatible endpoints, local inference, other
Pydantic AI providers, and headless execution.

Useful slash commands include `/connect` (switch or add a target), `/models`
(configure or select the local model), `/effort` (change reasoning effort live),
`/plugins` (toggle, configure, or add plugins), `/mcp` (list MCP servers and their
tools), `/skills` (browse Agent Skills), and `/todo` (open the session's plan).
Typing `/` shows an inline filtered list. Type `$` in the composer to find a skill and
insert a `$skill-name` reference, which runs that skill for the message. The older
`/model` and `/capabilities` names remain aliases.

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

`add_agent` binds and validates the definition's resource requirements at registration; pass
`resources=`, or a `definition.bind(...)` result, when a capability needs host values.
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

Run `just docs` to preview the Zensical site at <http://127.0.0.1:8000> with live
reload. The separate `docs` dependency group is installed automatically. The site
includes these guides, a Python API reference generated from source and docstrings,
`llms.txt`, and `llms-full.txt`.

Publishing a GitHub release builds the documentation at the release tag and saves
the `site/` output as a `docs-<tag>` workflow artifact. Documentation is not built
by commit hooks or ordinary push/pull-request CI. The site uses root-relative URLs;
set `site_url` in `zensical.toml` to the public documentation URL when hosting it.

- [Architecture](docs/ARCHITECTURE.md) and [shared contracts](docs/CONTRACTS.md)
- [Extension protocol and wire examples](docs/PROTOCOL.md)
- [Runtime Python API](docs/RUNTIME_API.md)
- [ACP host](docs/ACP.md) and [client usage](docs/CLIENT_API.md)
- [Embed agents in FastAPI](docs/SERVER.md)
- [Workspace lifecycle](docs/WORKSPACES.md)
- [Security model](docs/SECURITY.md)
- [Concept compliance matrix](docs/COMPLIANCE.md)
- [Verification and support boundaries](docs/VERIFICATION.md)

## Checks and distribution builds

```sh
uv run ruff format --check src examples
uv run ruff check .
uv run pyrefly check src examples --no-progress-bar
uv build
```

Or run `just check` with [just](https://just.systems/) installed; `just fix` applies the
automatic formatting and lint fixes. CI exercises the same non-mutating checks on
macOS/Linux and Python 3.11–3.13. A wheel and source distribution are written to `dist/`; optional dependencies are declared as extras.

Package versions come from Git tags through [hatch-vcs](https://pypi.org/project/hatch-vcs/).
Tag releases as `v1.2.3`; commits between releases receive development versions.
When version detection is unavailable, the build falls back to `0.0.1`.

External provider, sandbox and durability integrations need their upstream extras and credentials. See the coverage matrix for managed, locally verified and deployment-only distinctions.

## License

MIT. See [LICENSE](LICENSE).
