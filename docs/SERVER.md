# Embed agents in FastAPI

`AgentServer` registers native `AgentDefinition` objects on an existing FastAPI application
and serves ACP over WebSockets. Agents execute inside that process through the same
`HrnsACPAgent` and `compose_definition` path as local stdio. There is no server
configuration file, worker command, token registry, or separate server CLI.

## Register and mount

```sh
uv sync --locked --extra server
```

```python
from fastapi import FastAPI
from hrns.runtime import AgentDefinition, SQLiteSessionStore
from hrns.server import AgentServer

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
through the validated `_hrns/configuration/update` transaction. Edits apply at the next
turn boundary. `new_session`-lifecycle bindings still require a new session. Model and
reasoning effort are standard ACP options and can change from the next request.

Run with your normal ASGI runner:

```sh
uv run uvicorn my_app:app --host 127.0.0.1 --port 8080 \
  --ws-max-size 16777216 --ws-ping-interval 30 --ws-ping-timeout 30
```

`examples/fastapi_app.py` is a complete application.

Remote registrations reject client capabilities by default. To accept them for an agent,
use `agents.add_agent(definition, accepts_client_capabilities=True)`. ACP initialization
advertises the choice as `_meta.hrns.acceptsClientCapabilities`. Client plugins must still
be offered by the definition, supported by the connected client, and permitted by their
individual policies. The Plugins tab shows Server plugins and Client plugins separately;
refused client plugins remain visible but greyed out. Server and client plugins can run
independently, including the server shell alongside the client terminal.

## Storage and lifespan

```python
from hrns.runtime import SQLiteSessionStore

agents = AgentServer(store=SQLiteSessionStore("./sessions.sqlite3"))
```

PostgreSQL (`hrns[postgres]`):

```python
import os
from hrns.runtime.postgres import PostgresSessionStore

agents = AgentServer(store=PostgresSessionStore(os.environ["DATABASE_URL"]))
```

Mounting composes a router lifespan with the application's existing lifespan. It initializes
the database, closes active connections at shutdown, then closes the store. Give the server a
store instance dedicated to its lifetime.

Sessions are scoped by principal and by the logical agent id. Each row records the
`definition_version` that created it, so a version bump does not orphan the conversation;
loading resolves that pinned revision's definition, workspace roots, resources, and model
default. cwd, title, effort, selected bindings, usage, native history, and the ACP
transcript persist in the shared store. Keep the principal stable to reopen conversations. Live adapters and in-flight turns are not restored after a restart; uncertain
prompts are never resent automatically. Durability follows the upstream save/load contract:
save failures are logged and the next successful save catches up.

## Optional authentication

Without a dependency, every caller is `Principal("local")`. Static bearer tokens:

```python
import os
from hrns.server import AgentServer, BearerAuth, Principal

agents = AgentServer(
    store=store,
    authenticate=BearerAuth({os.environ["HRNS_TOKEN"]: Principal("alice")}),
)
```

For application auth, return `Principal(id=...)` from a FastAPI dependency that accepts
`HTTPConnection` (so it works for both HTTP and WebSocket handshakes). Raise
`HTTPException(401)` for invalid credentials. `Principal.agent_ids` restricts visible agents;
unknown and forbidden IDs both return 404. The principal is fixed for the connection.

## Resources, models, and workspaces

```python
from hrns.runtime import MODEL, WORKSPACE

bound = coding_definition.bind(
    {MODEL: model, WORKSPACE: "/srv/workspaces/project"}
)
agents.add_agent(
    bound,
    models=["openai:gpt-5-mini", "openai:gpt-5"],
    allowed_cwds=["/srv/workspaces/project"],
    default_cwd="/srv/workspaces/project",
)
```

Resources are bound to typed slots (`MODEL`, `WORKSPACE`, `ANSWERS`, `PLAN`, or a slot a
plugin registers): a misspelled slot is an import-time error rather than a `None` at run time,
and a value of the wrong type is rejected at registration. A plain string keyed mapping is
still accepted for configuration that arrives as data (`--resource NAME=VALUE`, settings
files); it resolves to the slot owning that name. Model strings bound through resources or
`definition.model` are advertised automatically; `models` adds selectable IDs. Definitions
use the same native composition as Python and stdio.

Capabilities declare their resource needs; the definition derives the matching requirements.
`definition.bind(...)` rejects missing or wrongly typed values before the server registers the
agent, and composition re-checks them before the plugin factory runs. `add_agent` still accepts
a bare definition plus `resources=`, but it performs the same bind internally, so a coder
definition without a workspace fails at registration rather than at `session/new`.

The current directory at registration is the default allowed workspace root. Paths are
canonicalized; `default_cwd` must be one of the roots. ACP session paths must remain inside
those roots, and reopening uses the recorded path. A bound workspace backend wins over the
session's `cwd`, so a deployment may bind a remote or sandboxed backend while clients send an
ordinary path; workspace execution otherwise falls back to a local path, and sandbox/lease
management is deployment-owned and never silently executes on the host.

Pass `registry=your_registry` for custom plugins. The default registry contains the bundled
`hrns.coder`, `hrns.todo` and `hrns.skills` plugins and installed
`hrns.capabilities` entry points; a
definition still has to bind and enable a plugin before a session can use it. Embedded
connections can edit existing next-turn bindings; remote clients cannot add local plugins or
supply MCP commands.

## Per-session access, capability gate, and policy

Sessions are owner-private by default. The embedding application can change that without
an ACP round trip:

```python
from hrns.runtime import SessionAccess

await agents.set_session_access(
    principal, "coding", session_id,
    access=SessionAccess.SHARED,
    collaborators=frozenset({"bob"}),
)
await agents.set_session_access(
    principal, "coding", session_id, access=SessionAccess.ARCHIVED
)
```

`ARCHIVED` sessions still load and replay, but every mutation — prompt, steering,
configuration, commands, rename, approvals, close, delete — is rejected with
`SESSION_READ_ONLY`. The `_hrns/session/access` extension exposes the same operation to an
authorized client.

`set_session_capabilities(principal, agent_id, session_id, True | False | None)` overrides
the deployment's `accepts_client_capabilities` default for one session. `None` restores the
deployment default. Composition binds client-origin tools only when the resulting gate is
open; advertising a wider mode from the client cannot widen it. A change to a live
session takes effect on its next `session/load` (or new session), when composition is
rebuilt.

Pass `session_policy=...` to `AgentServer` for custom rules. A policy implements
`decide(context, action)` and returns `PolicyDecision.allow()`, `.deny(...)`, or
`.read_only_denial()`; it may be async. Private/shared/archived semantics are the default.

## Authoritative embedding close and delete

Embedding applications close or delete a session through the same authoritative actor and
durable lifecycle used by hosted ACP:

```python
closed = await agents.close_session(principal, "coding", session_id)
deleted = await agents.delete_session(principal, "coding", session_id)
```

Both calls authorize the supplied `Principal` against the agent scope and session policy
before writing lifecycle state. A foreign and a nonexistent session both raise
`ApiError(code="NOT_FOUND")`; archived/read-only policy raises `FORBIDDEN`; another live
worker raises `CONFLICT` without creating a lifecycle operation. Lifecycle hook failures
raise `ApiError(code="LIFECYCLE_FAILED")`.

Once a close/delete intent is durable, the session admits no new prompt, configuration,
command, approval, load, or administrative mutation. The active ordered operation is
cancelled and finishes as `uncertain`, because a model or tool boundary may already have
been crossed. Operations that were queued but never claimed become `cancelled` and never
run. Every subscribed viewer receives the same `hrns/session/terminated` notification
before the actor forgets the session and removes all subscriptions.

`close_session()` retains the session row, native message history, ACP transcript, metadata,
usage, and terminal queue ledger so a later `session/load` creates a fresh live session.
It releases the active lease/fence and forgets every in-memory composition and turn handle.
`delete_session()` performs the same shutdown, then atomically removes the session row,
all non-lifecycle queue rows, and the lease/fence. It retains only the completed delete
receipt needed to make a retry after a lost response return the same
`SessionLifecycleResult`; that receipt is not runnable or reloadable session state.

Close/delete retries reuse the same durable `operation_id`. `destroy` receives that id as
`SessionLifecycleContext.operation_id`; resource managers must use it as their idempotency
key when an external side effect crosses a process-failure boundary. A failed `destroy`
leaves the operation retryable and keeps admission closed. Cancellation or process loss
leaves a claimed durable operation rather than implying success.

After startup, reconcile interrupted lifecycle operations with the public store still open:

```python
async with agents.lifespan(app):
    reconciled = await agents.reconcile_session_lifecycle()
```

Reconciliation reclaims only an expired lease. If another worker still owns a live lease,
it raises `CONFLICT` and performs no partial close/delete; retry after that owner releases
or loses the lease.

## Deployment revisions

`add_agent()` may be called before or after `mount()`. Each call registers an immutable
revision keyed by `(agent_id, definition_version)`. New sessions resolve the latest visible
revision; older revisions stay retained and can be resolved by version with
`server.get_revision(agent_id, principal, version=...)`. `await server.unregister_agent(id)` hides every revision without deleting it and
persists the decision; `await server.set_agent_visible(id, version, visible)` changes one
revision. Registering a duplicate version raises `DeploymentConflict`; an unknown pinned
version raises `MissingDeployment`.

Session scope is the logical agent, not the revision. A session records the
`definition_version` that created it, and loading resolves that retained definition, so
a version bump does not orphan existing sessions. A fully hidden agent disappears from
the catalog and refuses `session/new` with `DEPLOYMENT_UNAVAILABLE`, but its existing
sessions can still be loaded through the WebSocket route.

Revision metadata is stored in `acp_deployments`. After a restart the catalog restores
the metadata (visibility and retention) but not native Python objects: the application
re-registers each revision it wants served or kept loadable. A metadata-only revision
is retained but is not served, and it never shadows an older native revision: catalog
and connection resolution pick the newest native revision. Use
`server.retained_revisions(agent_id)` to see what the durable catalog expects. A session
pinned to a revision that has no native object fails with `DEPLOYMENT_UNAVAILABLE`
instead of silently running a different revision.

Stores written by pre-namespace builds are unsupported: the session namespace changed
without a compatibility path, so open a fresh database instead of pointing this build at
an old store.

## Multiple viewers and the durable queue

Connections to the same agent deployment in one worker share one authoritative
`HrnsACPAgent`. Session updates are broadcast only to connections subscribed to that
session; a connection subscribes on `session/new`, `session/load`, or before its first
prompt. Session updates have no fallback after an unsubscribe; only local stdio may use
its sole client before a subscription exists. Configuration and command catalog change
notifications are delivered to the same session subscribers, not only to the initiating
connection. Changing a session's access drops viewers the new decision no longer admits,
so a collaborator removed by a private/shared transition stops receiving the stream
immediately. The owner and still-admitted collaborators keep their existing
subscriptions, including when the change comes from the embedding API with no request
connection; reapplying an identical access and collaborator set is a no-op. Client-origin
calls during an in-flight turn still route to the connection that started the turn and
are not re-authorized until the turn ends or the viewer reloads. Permission requests are
policy-guarded before each ask. A second worker is
refused with `SESSION_HELD` until the first worker's lease expires, then takes ownership
with a new fence and marks that owner's in-flight prompts `uncertain`.

Prompts are persisted in `acp_prompt_queue` before they run. The extension surface is:

| Method | Purpose |
| --- | --- |
| `_hrns/queue/enqueue` | Persist a text prompt without executing it |
| `_hrns/queue/run` | Execute ordered prompts and configuration mutations once each |
| `_hrns/queue/list` | Inspect operation kinds, states, attempts, origins, and parent links |
| `_hrns/queue/retry` | Create and execute a new attempt linked to an `uncertain` one |

Inline prompts and configuration mutations join the same durable per-session sequence.
Configuration waits for the active turn boundary, and a cancelled or abandoned active
prompt becomes `uncertain` instead of being replayed automatically.

## Revocation and lifecycle

Each `Principal` may carry a `credential_id`; `BearerAuth` derives a stable one per token.
`await agents.revoke_credential(credential_id)` marks the credential revoked, closes every
matching socket, and waits for those handler tasks before returning. The credential cannot
open a new connection: the handshake is denied with `401 UNAUTHORIZED`.
`revoke_principal(id)` closes all of a user's connections without invalidating future
authentication.

Pass `lifecycle=...` for external resources. Implement any of `create`, `resume`,
`suspend`, `destroy`, and `queue_changed` (sync or async). `create` and `resume` run on
`session/new` and `session/load`; a queue drain requires the session to be live, so load
first. Queue idle invokes suspend; close/delete invokes destroy
once per durable lifecycle operation. A hook failure surfaces as `LIFECYCLE_FAILED` and no
prompt runs without its resource. Use `SessionLifecycleContext.operation_id` to deduplicate
an external destroy across an unknown process-loss boundary.

## Routes and client connection

```sh
uv run hrns agents list --url http://127.0.0.1:8080
uv run hrns connect --url http://127.0.0.1:8080 --agent AGENT_ID
```

| Route | Purpose |
| --- | --- |
| `GET /v1/agents?limit=50&cursor=...` | Visible agents, signed pagination cursor |
| `GET /v1/agents/{agent_id}` | One registered definition's descriptor |
| `WS /v1/agents/{agent_id}/acp` | ACP connection, subprotocol `hrns.acp.v1` |

`agents.mount(app, prefix="/ai")` prefixes these routes. Catalog errors use
`{error: {code, message, details}}`. WebSocket framing accepts one JSON-RPC object per UTF-8
text frame with a 16 MiB limit. Receive buffering applies backpressure; outbound writes wait
with a 30-second timeout. Connections are limited to 32 per server and four per principal.
Configure the ASGI runner's WebSocket size/ping limits as shown above. TLS and proxy trust
belong to the application runner or reverse proxy.
