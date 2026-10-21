# Shared implementation contracts

`harness.protocol` exports typed configuration changes and snapshots, errors,
capability/plugin descriptors, and runtime events. Wire DTOs accept snake_case and
camelCase; `.to_wire()` emits JSON-safe camelCase excluding None. Unknown fields
are rejected. Secret objects belong in runtime resources, never wire DTOs.

## Embedded FastAPI server

`AgentServer(store=..., authenticate=..., registry=...)` accepts an explicit SQLite
`SessionStore` or `PostgresSessionStore`. `add_agent(definition, ...)` registers an
ordinary `AgentDefinition` and optional Python resource bindings, selectable models,
and workspace roots. `mount(app, prefix="")` adds catalog and WebSocket ACP routes
and composes storage lifecycle with the existing FastAPI lifespan.

There is no server configuration file or subprocess deployment catalog. Registration
passes native Python objects directly. The server executes definitions in its own
process and uses a connection-local `SessionManager` with the shared database.
Disconnect closes that connection's sessions and releases their claims. Application
shutdown drains connections before closing storage.

The optional FastAPI authentication dependency returns `Principal(id, agent_ids=None)`.
It runs for HTTP and WebSocket requests and may depend on `HTTPConnection`. The default
identity is `local`, with access to every registered agent. Authenticated identity and
workspace bounds come from the application, never client ACP metadata.

Session ownership is scoped by principal plus a namespace derived from the registered
agent's ID and definition version. SQLite and PostgreSQL persist conversation state,
configuration, transcript, and ACP metadata (cwd/model/effort). The database enforces
exclusive session control; a live owner is never stolen.

## Runtime and ACP

The runtime owns inference composition, policy, revisions, persistence, ownership,
and transcript journaling. `HarnessHostAgent` translates ACP requests to this same
controller for both stdio hosts and in-process network connections.

Remote connections disable local binding creation and arbitrary client MCP setup.
Declared managed capability bindings still support policy-checked changes between
turns. Workspace roots are canonicalized and enforced by the ACP host. A sandbox
failure never falls back to local execution.

Local stdio clients may continue launching `harness-host` or `harness-local` as
independent processes. The stdio CLI's host environment is separate from the embedded
server API; network registration does not require `HARNESS_*` environment variables.

## Transport

ACP v1 is the baseline. The project-defined network subprotocol is
`harness.acp.v1`: one JSON-RPC object per UTF-8 text frame, no batches or binary, at
most 16 MiB. Shared framing helpers reject duplicate keys, non-finite numbers and
ambiguous envelopes. The WebSocket bridge translates frames to the official ACP
SDK's newline-framed streams without spawning a process.

Extension list responses use `items` and optional `nextCursor`. Capability lists also
carry `selectedRevision`, `activeRevision` and `policyRevision`. The authoritative
binding policy is `BindingDescriptor.policy`; the top-level `changeMode` mirrors it.
`_harness/plugins/list` is advertised only when local binding creation is permitted.

The client never automatically resends an uncertain prompt. Configuration transactions
use a stable request ID and expected revision; a changed payload under the same ID is
rejected. Disconnect cancels active work and releases session control after cleanup.

## Pins

Pydantic AI slim **2.57.0**, official Harness **0.57.0**, official ACP SDK **0.11.1**
(Harness's supported `<0.12` range), Textual **8.2.8**. `uv.lock` records transitive
versions. [SERVER.md](SERVER.md) documents the public embedding API;
[RUNTIME_API.md](RUNTIME_API.md) and [ACP_RUNTIME_CONTRACT.md](ACP_RUNTIME_CONTRACT.md)
document runtime integration.
