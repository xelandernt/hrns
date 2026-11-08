# hrns

Pydantic AI agents with server-owned conversations, ACP interaction, and read-only AG-UI streams.

hrns builds a Pydantic AI `Agent` from capabilities and exposes it over **ACP**,
for terminals and editors.

## Install

```sh
uv sync --extra client --extra server
```

Use `--extra client` for the terminal only and `--extra server` for the agent and
HTTP entry points; the base install provides the shared `hrns config`, `profiles`,
and `mcp` commands. ACP support is implemented in `hrns.server.acp`;
`pydantic-ai-harness` supplies the coding capabilities.

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

## Skills

The agent reads [Agent Skills](https://agentskills.io/specification) from
`<workspace>/.agents/skills` and `~/.agents/skills` by default. A skill is a directory
with a `SKILL.md`; the model sees its name and description, and loads the instructions
only when it needs them. A person can list skills with `/skills` and invoke one with
`$name`; the terminal sends the id, and the server expands it, so the body never
travels over the wire.

```toml
[skills]
paths = ["~/shared/skills"]            # libraries read before the defaults
exclude = ["experimental-release-notes"]
```

`hrns.server.capabilities.Skills` wraps `pydantic_ai_skills.SkillsCapability`, and
`build_agent` installs it by default. Skills defined in Python can ship with an app
instead of on disk — see `examples/fastapi_app.py`. The full reference, including the
ACP `_meta` payloads and the security notes, is in [docs/skills.md](docs/skills.md).

## Serve agents

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from hrns.server import HrnsServer, build_agent

server = HrnsServer()
coder = server.add_agent("coder", factory=lambda: build_agent(cwd="/srv/project"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with server:
        yield

app = FastAPI(lifespan=lifespan)
server.mount_acp(app, prefix="/acp")
server.mount_ag_ui(app, prefix="/ag-ui")
```

Install observation support with `uv sync --extra client --extra server --extra ag-ui`.
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

run_acp_stdio(lambda: build_agent())
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
to paste its API key into the password field; the key is stored in the OS keyring,
not in the TOML file. After saving a provider without an active model, the model
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

## Limitations

- Sessions, queues, pending interactions, and observation are process-local. Run one
  worker per server. Durable session stores restore committed state after restart;
  they do not restore active tasks or queued prompts.
- Provider configuration is in memory and applies from the next model resolution, which
  is the next turn; a turn already in flight is unaffected. MCP-over-ACP supports tools,
  resources, prompts, completions, progress, and input-required results; long-lived
  subscriptions are not streamed yet (see [MCP over ACP](docs/mcp.md)). Legacy
  (handshake-era) upstream MCP servers are best-effort: the proxy mirrors their advertised
  capabilities and logs the negotiated revision, but does not bridge reverse requests
  (sampling, elicitation, roots). An exported server is offered only after the client's
  user approves it at session start (or `always_allow = true` is configured); headless
  clients deny it otherwise. Startup and authentication failures appear in the `/mcp`
  side panel and as a status hint. Next
  edit suggestions (`nes/*`) remain unimplemented and are not advertised.
- `build_agent` adds a small approval capability by default: reads run freely, writes and
  shell calls are marked `unapproved` so ACP can request permission. Pass
  `approval=False` to disable it.
