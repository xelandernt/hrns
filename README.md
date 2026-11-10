# hrns

<p align="center">
  <img src="https://raw.githubusercontent.com/xelandernt/hrns/main/docs/assets/hrns.svg" alt="hrns logo" width="128" height="128">
</p>

[Pydantic AI](https://ai.pydantic.dev/) agents with server-owned conversations, ACP interaction, and read-only AG-UI streams.

hrns builds a Pydantic AI `Agent` from capabilities and exposes it over **ACP**,
for terminals and editors.

## Install

```sh
uvx hrns
```

Or install with `pip install hrns`. Both include the terminal client and server.

For a minimal installation, use `hrns-slim`, adding `[client]` for the terminal or
`[server]` for the agent and HTTP entry points. Without extras, it provides the shared
`hrns config`, `profiles`, and `mcp` commands. Python imports and the command remain `hrns`.

For local development, run `just install`.

## Define the agent

```python
from hrns.server import build_agent

agent = build_agent("anthropic:claude-sonnet-5-5", cwd=".")
```

The default agent includes coding tools, task tracking, skills, user questions, and
approval handling. Compose your own agent with the [capabilities](docs/capabilities.md)
reference.

## Skills

The agent reads [Agent Skills](https://agentskills.io/specification) from
`<workspace>/.agents/skills` and `~/.agents/skills` by default. A skill is a directory
with a `SKILL.md`; the model sees its name and description, and loads the instructions
only when it needs them. A person can list skills with `/skills` and invoke one with
`$name`; the terminal sends the id, and the server expands the instructions.

```toml
[skills]
paths = ["~/shared/skills"]            # libraries read before the defaults
exclude = ["experimental-release-notes"]
```

`build_agent` discovers skills by default. Applications can supply Python definitions
with `Skills(skills=...)`; see `examples/fastapi_app.py`. The full reference, including the
ACP `_meta` payloads and the security notes, is in [docs/skills.md](docs/skills.md).

## Serve agents

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from hrns.server import HrnsServer, build_agent

server = HrnsServer()
coder = server.add_agent("coder", factory=lambda session: build_agent("anthropic:claude-sonnet-5-5", cwd=session.cwd))

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with server:
        yield

app = FastAPI(lifespan=lifespan)
server.mount_acp(app, prefix="/acp")
server.mount_ag_ui(app, prefix="/ag-ui")
```

Install observation support with `pip install 'hrns[ag-ui]'`.
Use `AcpServer` when only ACP is needed. Register multiple factories under different
agent IDs. One ACP client may interact with a session while any number of AG-UI clients
observe it. Execution survives client disconnects.

Inside the server lifecycle, start work directly:

```python
session = await coder.new_session(cwd="/srv/project")
turn = await session.submit("Investigate the deployment failure")
result = await turn.result()
```

Follow-ups share a FIFO with ACP prompts. Cancellation and failures pause it;
`pending_prompts()`, `resume_queue()`, and `clear_queue()` are Python-only controls.
Permissions/questions wait for an ACP attachment. See [agent servers](docs/mount-acp.md)
for configuration, authentication, stores, and model/provider routing, and
[AG-UI observation](docs/ag_ui.md) for read-only snapshots and SSE replay.

```sh
hrns connect --server ws://127.0.0.1:8000/acp/coder
hrns run --server ws://127.0.0.1:8000/acp/coder "summarize this repo"
```

Remote hosts require `wss://`. Pass a bearer token with `--token env:NAME` or
`--token keyring:service/user`; admitted clients share the agent's server scope.
The [FastAPI example](examples/fastapi_app.py) shows both mounts with authentication.

For the packaged single-agent server:

```sh
HRNS_MODEL=provider:model hrns serve --port 8000
# ACP endpoint: ws://127.0.0.1:8000/acp/agent
```

For stdio:

```python
from hrns.server import build_agent, run_acp_stdio

run_acp_stdio(lambda: build_agent("anthropic:claude-sonnet-5-5"))
```

`hrns acp` starts the default agent over stdio; `hrns agent` uses the configured
models and provider routing. The terminal uses the same configured entry point.

## Configuration

The client and server read one document: `config.toml` in the platform
config directory, with an optional `hrns.toml` in the working directory overriding it
key by key. `HRNS_*` environment variables override both.
[Configuration](docs/configuration.md) is the field reference and the location map.

`hrns agent` launches the ACP entrypoint with the configured provider and model. The
terminal client reuses that entrypoint, so it passes the same document to it.

In the TUI connection picker, `l` opens provider management. Add or edit a provider
to paste its API key into the password field; the key is stored in the OS keyring. After saving a provider without an active model, the model
picker opens so a catalog or custom id can be selected. `/model provider:model` also
sets it directly.

`/providers` acts on whatever you are connected to. Over stdio to `hrns agent` it opens
the entry editor above; connected to a remote agent that advertises provider
configuration it lists that agent's providers and sends `providers/set` /
`providers/disable` for you.

`hrns agent` seeds its ACP provider registry from those TOML entries, so a client that
speaks `providers/*` sees one provider per entry and can reroute it to its own gateway.
The entry this deployment starts with is marked `required`, because disabling it would
leave the default session without a model; the headers a client sends reach the request
as `extra_headers` model settings.

## Runtime behavior

- Run one worker per server. Sessions, queues, pending interactions, and observation live
  in the process. Durable session stores restore committed state after restart.
- Provider routing changes apply on the next turn. A running turn keeps its selected
  endpoint; session setting changes apply before its next model request.
- MCP exports require client consent at session start. Headless clients export servers
  configured with `always_allow = true`. The `/mcp` panel shows startup and authentication
  failures. See [MCP over ACP](docs/mcp.md) for the request scope and upstream behavior.
- Tool approvals are enabled by default. Reads run freely; file writes and shell calls
  request permission through ACP. `approval=False` delegates approval to the deployment.
