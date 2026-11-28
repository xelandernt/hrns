# hrns

<p align="center">
  <img src="https://raw.githubusercontent.com/xelandernt/hrns/main/docs/assets/hrns.svg" alt="hrns logo" width="128" height="128">
</p>

[Pydantic AI](https://ai.pydantic.dev/) agents with server-owned conversations, ACP interaction, and read-only AG-UI streams.

hrns builds a Pydantic AI `Agent` from capabilities and exposes it over **ACP**,
for terminals and editors.

## Install

```sh
uvx --from 'hrns>0.0.28' hrns
```

Or install with `pip install 'hrns>0.0.28'`. The full install includes the native
terminal, headless runner, and server. The native wheel supports Linux x64 with
glibc, macOS arm64 and x64, and Windows x64. The version floor excludes older
releases that shipped the Python terminal.

For a smaller install, use `hrns-slim` with `[client]` for the native terminal,
`[headless]` for scripted prompts, or `[server]` for the agent and HTTP entry points.
See the [install commands](docs/release.md#version-matched-native-dependency). Without extras, it provides the shared
`hrns config`, `profiles`, and `mcp` commands. Python imports and the command remain `hrns`.

For local development, install Rust 1.90 or newer and run `just install`.

## Terminal clients

The interactive client is being replaced by a native [Ratatui](https://ratatui.rs/)
frontend with a [Ratzilla](https://github.com/ratatui/ratzilla) browser build. Both use
the official Rust ACP SDK and the same composer, transcript, session and approval
state. Native supports the built-in agent, external stdio commands and remote
WebSocket targets; the browser connects only to an authenticated remote ACP mount and
advertises no client filesystem, terminal or MCP capability.

Press **Ctrl+G** or enter `/agents` to switch between Harness sessions. Use
`/new` in the current workspace or `/new /path/to/workspace` for another
directory. Runs keep going when you switch or quit, and the Agents view shows
which sessions are running, need input, or are open in another TUI.

In a local native session, type `@` to search workspace files. Use **Up** and
**Down** to browse, **Tab** or **Enter** to insert a highlighted `@path` reference,
and **Esc** to close the search. The agent can open the path when it needs the file.

Bare `hrns` and `hrns connect` launch the installed native client. In a source
checkout, `just install` and `uv sync --frozen --all-extras` build it from the
local Rust source. Run either command after editing the TUI to refresh the
executable. Published wheels are available on the platforms listed in the
[release guide](docs/release.md#version-matched-native-dependency).

The launcher passes the connection settings to the native executable. Remote
client I/O is off unless enabled for that endpoint by a CLI flag or profile.
See [client I/O configuration](docs/configuration.md#tui) for details.
To build the browser bundle, run `npm ci --prefix npm` and then
`trunk build --locked --release` from `rust/`. Serve `rust/dist/` from the ACP origin.
See [architecture](ARCHITECTURE.md#rust-terminal-preview) and
[browser deployment](docs/authentication.md#serve-the-ratzilla-client-with-acp-authentication).

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

Install observation support with `pip install 'hrns[ag-ui]>0.0.28'`.
Use `AcpServer` when only ACP is needed. Register multiple factories under different
agent IDs. One ACP client may interact with a session while any number of AG-UI clients
observe it. Execution survives client disconnects.

Inside the server lifecycle, start work directly:

```python
session = await coder.new_session(cwd="/srv/project")
turn = await session.submit("Investigate the deployment failure")
result = await turn.result()
```

Pending follow-ups run automatically when the active turn ends, including after
cancellation or an error. Use `pending_prompts()` and `clear_queue()` to inspect or
clear queued turns from Python.
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
HRNS_MODEL=provider:model hrns serve --cwd /path/to/projects --port 8000
# ACP endpoint: ws://127.0.0.1:8000/acp/agent
hrns connect --server ws://127.0.0.1:8000/acp/agent --cwd /path/to/projects
```

Sessions can select workspaces under the server's `--cwd` directory. For stdio:

```python
from hrns.server import build_agent, run_acp_stdio

run_acp_stdio(lambda: build_agent("anthropic:claude-sonnet-5-5"))
```

`hrns acp` starts the default agent over stdio. `hrns agent` uses the configured
models and provider routing. The native terminal starts or joins a local Harness
server with that configuration.

## Configuration

The client and server read `config.toml` from the platform config directory by
default. `--config PATH` selects another file. `HRNS_*` environment variables
override values from that file.
[Configuration](docs/configuration.md) is the field reference and the location map.

`hrns agent` launches the ACP entrypoint with the configured provider and model. The
terminal client reuses that entrypoint, so it passes the same document to it.

In Local mode, open `/providers` to add, edit, delete, or select a provider.
Save its connection settings, select the entry, then run `/login` and choose a
sign-in method. Use `/models` to pick a model for the session.

The local sign-in menu supports Bedrock with AWS credentials, GitHub Copilot
and Codex account login, and API keys for DeepSeek and OpenAI-compatible
endpoints. `hrns login <provider>` also signs in to a configured local entry.
[Provider setup](docs/configuration.md#local-provider-setup) lists the required settings.

## Runtime behavior

- Run one worker per server. Sessions, queues, pending interactions, and observation live
  in the process. Durable session stores restore committed state after restart.
- Provider routing changes apply on the next turn. A running turn keeps its selected
  endpoint; session setting changes apply before its next model request.
- MCP exports require client consent at session start. Headless clients export servers
  configured with `always_allow = true`. `/mcp` and the `Ctrl-B` sidebar show startup and authentication
  failures. See [MCP over ACP](docs/mcp.md) for the request scope and upstream behavior.
- Tool approvals are enabled by default. Reads run freely; file writes and shell calls
  request permission through ACP. `approval=False` delegates approval to the deployment.
