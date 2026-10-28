# Benchmark results

Three runs per hrns on a benchmark task using **GPT-6-Luna with medium reasoning**.

| hrns | Tests pass | All quality gates pass | Model responses | Tool calls | Total cost | Average/run |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Codex | 3/3 | 3/3 | 44 | 56 | $0.03658 | $0.01219 |
| OpenCode | 3/3 | 2/3 | 43 | 47 | $0.03578 | $0.01193 |
| **hrns** | **3/3** | **3/3** | **44** | **59** | **$0.03300** | **$0.01100** |

All runs pass **130/130 tests**. Quality gates cover Ruff lint, Ruff formatting,
and Pyrefly type checking.

Counts and costs are totals across three runs. Model responses include tool-only
responses and final answers. Tool calls count underlying attempts, including
failures; Codex's 41 orchestration calls contain 56 underlying attempts.
Costs are cache-aware **API equivalents in USD** using common Standard model pricing.

Skill loads: **Codex 0/3**, **OpenCode 1/3**, **hrns 3/3**. The loaded skill is
`incremental-implementation`, with relevant selection and partial adherence.

---

# hrns

**Define an agent once. Run it in Python, open it in a terminal, or host it remotely.**

hrns combines native Pydantic AI capabilities with policy-controlled sessions, an independent ACP terminal, and authenticated remote hosting. Agent behavior stays in ordinary Python; the terminal can also connect to standard ACP agents that do not use this runtime.

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

The local developer agent is the default connection mode. The base install ships the
terminal, the runtime, and the OpenAI-compatible providers (DeepSeek, OpenAI, and local
servers); add the `anthropic` extra for Anthropic models. Set the API key for the provider
you use:

```sh
export DEEPSEEK_API_KEY='your-key'
uvx hrns
```

From a checkout, run `uv sync --locked` and then `uv run hrns` instead.

Press **l** to configure providers, type `/providers` or `/models` in a session, or create
`hrns.local.toml` in the project:

```toml
provider = "deepseek"
model = "deepseek-chat"

[providers.deepseek]
api_key_env = "DEEPSEEK_API_KEY"
```

The top level picks the active provider entry and model; each `[providers.<name>]` entry
owns its endpoint and key, so several providers (even two endpoints of one provider type)
stay configured side by side.

`uv run hrns` starts a session in the current directory using that model. Run the same
command from an installed copy (`uvx hrns` or `hrns`). The bundled
`hrns.coder` capability gives it file tools, content search, and a shell in that
directory: reads are automatic, while writes, edits, and commands ask for approval first.
`hrns.ask_user` lets the model ask structured multiple-choice questions in the terminal;
disable it with `[capabilities] ask_user = false` for headless runs. `hrns.todo` gives
the model a session-scoped plan and shows it in the **Todos** tab of the right drawer.
A second family of tools can run in the terminal instead of on the host: `/settings` →
**Client-side tools** (or `--client-io all`) lets the agent use this client's files and
terminal, and the `client.terminal` binding routes its commands there.
Configuration uses pydantic-settings. Providers that authenticate with an account
rather than a key work too: `hrns login github-copilot` runs GitHub's device flow, or
press **Ctrl+G** in the provider form. See
[local setup](docs/LOCAL.md) for OpenAI-compatible endpoints, local inference, other
Pydantic AI providers, and headless execution.

Useful slash commands include `/connect` (switch or add a target), `/providers` (add, edit
or delete local providers), `/models` (select a model across providers, or add a custom
model id), `/effort` (change reasoning effort live),
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
from hrns.runtime import SQLiteSessionStore
from hrns.server import AgentServer

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

hrns is one distribution. The terminal, the runtime and the OpenAI-compatible providers
ship by default; FastAPI hosting, Anthropic models and PostgreSQL are extras.

| Module | Extra | Purpose |
| --- | --- | --- |
| `hrns.protocol` | base | Versioned extension schemas and transport contracts |
| `hrns.config` | base | Shared local settings using pydantic-settings |
| `hrns.runtime` | base | Native agent definitions, plugin management, sessions, `hrns-host` and `hrns-local` |
| `hrns.client` | base | Cyclopts CLI, Textual terminal, scripted ACP runner and stdio bridge |
| `hrns.server` | `server` | FastAPI agent registration and in-process WebSocket ACP |

Install only what you need, for example `pip install 'hrns[server]'`,
`pip install 'hrns[anthropic]'`, or `pip install 'hrns[all]'`.

The `hrns.client` module does not import Pydantic AI or installed agent
plugins. Local stdio agents run in a separate process. Network agents run inside an existing
FastAPI application through `AgentServer`, sharing an explicit SQLite or PostgreSQL store.

## Design essentials

- **Upstream ACP execution:** `PydanticAIACPAgent` owns prompts, streaming, approvals, cancellation, history, and transcript replay.
- **Native composition:** managed bindings contribute their complete Pydantic AI behavior—tools, instructions, hooks and settings—and are selected when a session is composed.
- **Live model and effort:** both use standard ACP configuration options and apply from the next request.
- **Host-owned state:** sessions pin their principal/deployment scope, retain history and metadata, and never silently retry an uncertain prompt.
- **Explicit resources:** workspace and provider bindings belong to the host. A local workspace is a path, not a sandbox.
- **Standard ACP first:** generic streaming content, tools and approvals work without project-specific extensions.

## Checks and distribution builds

```sh
just check
just lint
just build
just fix
```

`just check` runs the checks and builds the package. `just lint` runs formatting,
lint, and type checks; `just build` builds the package. `just fix` applies automatic
formatting and lint fixes. CI exercises the same non-mutating checks on
macOS/Linux and Python 3.11–3.15. A wheel and source distribution are written to `dist/`; optional dependencies are declared as extras.

External provider, sandbox and durability integrations need their upstream extras and credentials. See the coverage matrix for managed, locally verified and deployment-only distinctions.

## Releasing

The version comes from the git tag via hatch-vcs, so there is no version string to
edit. Publishing runs on release: create a GitHub release and
`.github/workflows/publish.yml` builds the distribution, runs `just check`, verifies
the tag, and uploads to PyPI.

```sh
git tag 0.1.0
git push origin 0.1.0
gh release create 0.1.0 --generate-notes
```

The tag must match the version hatch-vcs derives, so `0.1.0` publishes `0.1.0`. A
mismatched tag fails the build instead of uploading an unintended version, and a
version that already exists on PyPI is rejected.

Uploads use PyPI Trusted Publishing, so no API token is stored in the repository.
Before the first release, configure a publisher on PyPI for the `hrns` project with
this repository's owner and name, workflow `publish.yml`, and environment `pypi`.
Because `hrns` does not exist on PyPI yet, use the pending publisher form.

## License

MIT. See [LICENSE](LICENSE).
