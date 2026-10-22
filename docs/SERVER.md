# Embed agents in FastAPI

`AgentServer` registers native `AgentDefinition` objects on an existing FastAPI application
and serves ACP over WebSockets. Agents execute inside that process through the same
`HarnessACPAgent` and `compose_definition` path as local stdio. There is no server
configuration file, worker command, token registry, or separate server CLI.

## Register and mount

```sh
uv sync --locked --extra server --extra client --extra openai
```

```python
from fastapi import FastAPI
from harness.runtime import AgentDefinition, SQLiteSessionStore
from harness.server import AgentServer

app = FastAPI()
agents = AgentServer(store=SQLiteSessionStore("./sessions.sqlite3"))
agents.add_agent(definition)
agents.mount(app)
```

Definitions may use an `AgentSpec`, a Python factory returning a native agent, fixed
capabilities, or managed plugin bindings. The server snapshots editable configuration while
retaining factories and native capability objects. IDs come from `definition.agent_id` and
must be URL-safe (`[A-Za-z0-9][A-Za-z0-9._-]*`).

Managed `bindings` are chosen when a session starts and can be edited during the session
through the validated `_harness/configuration/update` transaction. Edits apply at the next
turn boundary. `new_session`-lifecycle bindings still require a new session. Model and
reasoning effort are standard ACP options and can change from the next request.

Run with your normal ASGI runner:

```sh
uv run uvicorn my_app:app --host 127.0.0.1 --port 8080 \
  --ws-max-size 16777216 --ws-ping-interval 30 --ws-ping-timeout 30
```

`examples/fastapi_app.py` is a complete application; `test.py` adds a managed capability and
bearer authentication.

## Storage and lifespan

```python
from harness.runtime import SQLiteSessionStore

agents = AgentServer(store=SQLiteSessionStore("./sessions.sqlite3"))
```

PostgreSQL (`harness[postgres]`):

```python
import os
from harness.runtime.postgres import PostgresSessionStore

agents = AgentServer(store=PostgresSessionStore(os.environ["DATABASE_URL"]))
```

Mounting composes a router lifespan with the application's existing lifespan. It initializes
the database, closes active connections at shutdown, then closes the store. Give the server a
store instance dedicated to its lifetime.

Sessions are scoped by principal and by a namespace derived from the registered agent id and
definition version. cwd, title, effort, selected bindings, usage, native history, and the ACP
transcript persist in the shared store. Keep principal and definition version stable to reopen
conversations. Live adapters and in-flight turns are not restored after a restart; uncertain
prompts are never resent automatically. Durability follows the upstream save/load contract:
save failures are logged and the next successful save catches up.

## Optional authentication

Without a dependency, every caller is `Principal("local")`. Static bearer tokens:

```python
import os
from harness.server import AgentServer, BearerAuth, Principal

agents = AgentServer(
    store=store,
    authenticate=BearerAuth({os.environ["HARNESS_TOKEN"]: Principal("alice")}),
)
```

For application auth, return `Principal(id=...)` from a FastAPI dependency that accepts
`HTTPConnection` (so it works for both HTTP and WebSocket handshakes). Raise
`HTTPException(401)` for invalid credentials. `Principal.agent_ids` restricts visible agents;
unknown and forbidden IDs both return 404. The principal is fixed for the connection.

## Resources, models, and workspaces

```python
agents.add_agent(
    coding_definition,
    resources={"model.default": model, "workspace": "/srv/workspaces/project"},
    models=["openai:gpt-5-mini", "openai:gpt-5"],
    allowed_cwds=["/srv/workspaces/project"],
    default_cwd="/srv/workspaces/project",
)
```

Resources may be a mapping or `ResourceSet`. Model strings bound through resources or
`definition.model` are advertised automatically; `models` adds selectable IDs. Definitions
use the same native composition as Python and stdio.

The current directory at registration is the default allowed workspace root. Paths are
canonicalized; `default_cwd` must be one of the roots. ACP session paths must remain inside
those roots, and reopening uses the recorded path. Workspace execution is a local path in this
revision; sandbox/lease management is deployment-owned and never silently falls back to host
execution.

Pass `registry=your_registry` for custom plugins. The default registry contains the bundled
`harness.coder` plugin and installed `harness.capabilities` entry points; a definition still
has to bind and enable a plugin before a session can use it. Embedded connections can edit
existing next-turn bindings; remote clients cannot add local plugins or supply MCP commands.

## Routes and client connection

```sh
uv run hrns agents list --url http://127.0.0.1:8080
uv run hrns connect --url http://127.0.0.1:8080 --agent AGENT_ID
```

| Route | Purpose |
| --- | --- |
| `GET /v1/agents?limit=50&cursor=...` | Visible agents, signed pagination cursor |
| `GET /v1/agents/{agent_id}` | One registered definition's descriptor |
| `WS /v1/agents/{agent_id}/acp` | ACP connection, subprotocol `harness.acp.v1` |

`agents.mount(app, prefix="/ai")` prefixes these routes. Catalog errors use
`{error: {code, message, details}}`. WebSocket framing accepts one JSON-RPC object per UTF-8
text frame with a 16 MiB limit. Receive buffering applies backpressure; outbound writes wait
with a 30-second timeout. Connections are limited to 32 per server and four per principal.
Configure the ASGI runner's WebSocket size/ping limits as shown above. TLS and proxy trust
belong to the application runner or reverse proxy.
