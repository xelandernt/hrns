# Embed agents in FastAPI

`AgentServer` registers agent definitions on an existing FastAPI application and serves
ACP over WebSockets. Agents execute inside that application process. Storage and
optional authentication are Python dependencies; there is no deployment configuration
file, worker command, token registry, or separate server CLI.

## Register and mount

Install `harness[server]` and the provider extra your agent needs. In this checkout:

```sh
uv sync --locked --extra server --extra client --extra openai
```

```python
from fastapi import FastAPI
from harness.runtime import AgentDefinition, SessionStore
from harness.server import AgentServer

app = FastAPI()
agents = AgentServer(store=SessionStore("./sessions.sqlite3"))
agents.add_agent(research_definition)
agents.add_agent(coding_definition)
agents.mount(app)
```

Both definitions are ordinary `AgentDefinition` objects, with Python factories, fixed
native capabilities, or managed plugin bindings. The server snapshots their editable
configuration while retaining factories and native capability objects. The registered
ID comes from `definition.agent_id`; IDs must be unique and contain only letters,
digits, dots, underscores, or hyphens. Register all agents before calling `mount()`.

`AgentDefinition` is a runtime recipe, not a subclass of Pydantic AI's `Agent`.
If you already construct an agent with `Agent(..., capabilities=[...])`, pass that
constructor function as `AgentDefinition(factory=build_agent, ...)`. Those private
capabilities stay fixed; there is no need to repeat them in `fixed_capabilities`.
The latter is an optional always-on shortcut. Managed `bindings` are the separate
capabilities offered for policy-controlled configuration and enable/disable changes.

A binding has a `plugin_id` naming a registered capability factory, a `binding_id`
naming that particular instance, initial `config`, and an `enabled` flag. For example,
the custom capability example binds `example.house_style` as `style` with
`config={"style": "punchy"}`. Two bindings can use one plugin with different settings.
Permitted changes are stored per session and affect the next turn; changing one
session's binding does not change the definition or another session.

Run the application with your normal ASGI runner:

```sh
uv run uvicorn my_app:app --host 127.0.0.1 --port 8080 \
  --ws-max-size 16777216 --ws-ping-interval 30 --ws-ping-timeout 30
```

`examples/fastapi_app.py` is a complete application with a custom native capability.
It uses OpenAI by default; export `OPENAI_API_KEY`, or select the offline structural
model with `EXAMPLE_MODEL=test`:

```sh
uv run uvicorn examples.fastapi_app:app --host 127.0.0.1 --port 8080 --ws-max-size 16777216
uv run hrns connect --url http://127.0.0.1:8080 --agent example.house_style
```

The `test` model produces synthetic output; it does not perform useful model inference.

## Storage and lifespan

Pass a storage instance once to `AgentServer`. All registered agents use it. SQLite:

```python
agents = AgentServer(store=SessionStore("./sessions.sqlite3"))
```

PostgreSQL (requires `harness[postgres]`):

```python
import os
from harness.runtime.postgres import PostgresSessionStore

agents = AgentServer(store=PostgresSessionStore(os.environ["DATABASE_URL"]))
```

Mounting includes a router lifespan that composes with your application's existing
lifespan. It initializes the database at startup, closes active agent connections and
releases their session claims at shutdown, then closes the store. Give the server a
store instance dedicated to its lifetime; disconnecting one client never closes the
shared store or another client's sessions.

Conversation history, capability configuration, working directory, selected model,
and reasoning effort persist in the chosen database. Sessions are scoped by the
principal ID and the registered definition's `(agent_id, definition_version)` pin.
Keep both stable to reopen existing conversations; publish a new definition version
when changing behavior. Live sessions and running jobs are not reconstructed after a
process restart, and uncertain prompts are never resent automatically.

## Optional authentication

Without an authentication dependency, all callers use `Principal("local")` and share
that identity's session access. For static bearer tokens, use the built-in dependency:

```python
import os
from harness.server import AgentServer, BearerAuth, Principal

agents = AgentServer(
    store=store,
    authenticate=BearerAuth({os.environ["HARNESS_TOKEN"]: Principal("alice")}),
)
```

Each token maps to a stable principal; multiple tokens may map to the same principal.
The helper hashes tokens on construction, compares digests in constant time, and
returns 401 with a bearer challenge for missing, malformed, or invalid credentials.
It works for both catalog requests and WebSocket handshakes. Tokens can restrict
agent access through `Principal.agent_ids`.

For OAuth/JWT or existing application authentication, supply your own dependency. Use
`HTTPConnection` to support both HTTP catalog requests and WebSocket handshakes:

```python
from fastapi import HTTPException
from starlette.requests import HTTPConnection
from harness.server import AgentServer, Principal


async def authenticate(connection: HTTPConnection) -> Principal:
    authorization = connection.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Bearer token required")
    user = await your_auth_service.verify(token)
    if user is None:
        raise HTTPException(401, "Invalid token")
    return Principal(id=user.id)


agents = AgentServer(store=store, authenticate=authenticate)
```

The dependency can use FastAPI subdependencies, dependency overrides, and `yield`
cleanup. The exported `AuthenticationDependency` type accepts synchronous or async
callables returning `Principal`, or generator dependencies yielding one `Principal`.
Invalid credentials must raise `HTTPException(401)`; returning `None` or an unrelated
type is a programming error. Credentials are validated by your application.
Avoid `Request`-only dependencies on WebSocket routes. The catalog and socket use the
same dependency. An HTTP middleware that authenticates only HTTP requests does not
protect WebSockets automatically.

Return `Principal(id=user.id, agent_ids=frozenset({"research"}))` to limit a caller's
visible agents. With `agent_ids=None`, all registered agents are available. Unknown
and forbidden agent IDs both return 404. The principal is fixed for the connection;
per-message token refresh/revocation is owned by the application.

Harness's bearer token is the HTTP/WebSocket transport credential. The ACP
`authMethods`/`authenticate` mechanism is separate. The client can keep using
`--token env:HARNESS_TOKEN` with your application's bearer authentication.

## Resources, models, and workspaces

Optional registration arguments are ordinary Python values:

```python
agents.add_agent(
    coding_definition,
    resources={"model.default": model, "workspace": workspace_backend},
    models=["openai:gpt-5-mini", "openai:gpt-5"],
    allowed_cwds=["/srv/workspaces/project"],
    default_cwd="/srv/workspaces/project",
)
```

Resources may be a mapping or a `ResourceSet`. Model strings bound through resources
or `definition.model` are advertised automatically; `models` adds selectable IDs.
A factory with a private model can expose selections explicitly through `models`.
Definitions retain the same native composition and per-turn configuration policy as
Python sessions and stdio ACP hosts.

The current directory at registration is the default allowed workspace root. All
paths are canonicalized, and `default_cwd` must be one of the allowed roots. ACP
session paths must remain within those roots; reopening uses the recorded path.
Workspace resources are server-side bindings, and a requested sandbox failure never
falls back to host execution. No client filesystem synchronization is implied.

For custom managed plugins, pass `registry=your_plugin_registry` to `AgentServer`.
The default registry contains the bundled capability catalog. Remote clients can
manage declared bindings but cannot add arbitrary local plugins or supply MCP
commands. Provider credentials stay in the FastAPI process's normal environment or
resource bindings.

## Client connection and routes

```sh
uv run hrns agents list --url http://127.0.0.1:8080
uv run hrns connect --url http://127.0.0.1:8080 --agent research
# With application-provided bearer authentication:
uv run hrns connect --url http://127.0.0.1:8080 --agent research --token env:HARNESS_TOKEN
```

| Route | Purpose |
| --- | --- |
| `GET /v1/agents?limit=50&cursor=...` | Visible agents, signed pagination cursor |
| `GET /v1/agents/{agent_id}` | One registered definition's descriptor |
| `WS /v1/agents/{agent_id}/acp` | ACP connection, subprotocol `harness.acp.v1` |

`agents.mount(app, prefix="/ai")` prefixes these routes. Connect Harness with
`--url http://127.0.0.1:8080/ai`. Existing application routes, exception handlers,
and lifespan remain application-owned; the server does not install a global health
route or replace exception handlers.

Catalog errors use `{error: {code, message, details}}`. Authentication errors follow
your dependency/application's handlers. WebSocket handshake failures use HTTP denial
responses where supported, otherwise application close codes. After upgrade,
malformed JSON-RPC closes with 1002, binary frames with 1003, oversized messages with
1009, and stalled outbound sends with 4512.

The transport validates one JSON-RPC object per UTF-8 text frame, with a 16 MiB limit.
Receive buffering applies backpressure; outbound writes wait for the socket with a
30-second timeout. Connections are limited to 32 per `AgentServer` and four per
principal. Configure the ASGI runner's WebSocket size/ping limits as shown above.
TLS and proxy trust settings belong to the application runner or reverse proxy;
the Harness client permits plaintext WebSockets only for loopback.
