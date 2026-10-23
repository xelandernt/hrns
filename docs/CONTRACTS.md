# Shared implementation contracts

`harness.protocol` exports typed extension DTOs, errors, framing, and pagination. Wire DTOs
accept snake_case and camelCase; `.to_wire()` emits JSON-safe camelCase excluding `None`.
Unknown fields are rejected. Secrets stay in runtime resources, never wire DTOs.

## Runtime composition

`compose_definition(...)` is the single composition path for Python and ACP. It returns a
native `Agent` plus an `AcpSessionConfig`; it does not execute turns. A capability edit
re-runs composition and replaces the live composition's contents in place, preserving the
adapter's `SessionDeps` identity. Fixed and managed capabilities remain native Pydantic AI
capabilities, including hooks, instructions, toolsets, and event ownership.

## Embedded FastAPI server

`AgentServer(store=..., authenticate=..., registry=...)` accepts a shared
`SQLiteSessionStore` or `PostgresSessionStore`. `add_agent(definition | BoundDefinition, resources=...,
models=..., allowed_cwds=..., default_cwd=...)` registers an `AgentDefinition`.
`mount(app, prefix="")` adds catalog and WebSocket ACP routes and composes storage lifespan
with the existing FastAPI lifespan.

There is no server configuration file or subprocess deployment catalog. Registration passes
native Python objects. Each connection gets a new `HarnessACPAgent` with a scoped store view;
disconnect closes its sessions. Application shutdown drains connections before closing
storage.

The optional authentication dependency returns `Principal(id, agent_ids=None)` for HTTP and
WebSocket requests. The default principal is `local` with access to every registered agent.
Identity and workspace bounds come from the application, never from ACP metadata.

## Storage scope

Session rows are keyed by session id and every query includes `(principal, deployment_id)`.
The deployment id is a namespace derived from the registered agent id and definition version.
SQLite and PostgreSQL use the same `acp_sessions` schema. A scoped store implements the
upstream `SessionStore` protocol; the adapter owns conversation state, while the host owns
cwd/title/effort/bindings/usage metadata.

Durability follows upstream: `save` failures are logged, not fatal; no transactional turn
commit or cross-connection ownership claim is provided. Last writer wins.

## ACP and transport

ACP v1 is the baseline. The network subprotocol `harness.acp.v1` carries one JSON-RPC object
per UTF-8 text frame, no batches or binary, at most 16 MiB. Shared framing rejects duplicate
keys, non-finite numbers, and ambiguous envelopes. The WebSocket bridge feeds the official
ACP SDK's newline-framed streams without starting a subprocess.

Extension list responses use `items` and optional `nextCursor`. The host advertises only
implemented methods: capabilities/list, configuration/get, session/rename. Model and effort
are standard ACP config options.

The client never automatically resends an uncertain prompt. Disconnect cancels active work
and releases the connection's sessions.

## Pins

Pydantic AI slim **2.57.0**, pydantic-ai-harness **0.57.0**, ACP SDK **0.11.1**, Textual
**8.2.8**. `uv.lock` records transitive versions. See [RUNTIME_API.md](RUNTIME_API.md),
[ACP_RUNTIME_CONTRACT.md](ACP_RUNTIME_CONTRACT.md), and [SERVER.md](SERVER.md).
