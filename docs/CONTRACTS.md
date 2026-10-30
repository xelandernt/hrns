# Shared implementation contracts

`hrns.protocol` exports typed extension DTOs, errors, framing, and pagination. Wire DTOs
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
native Python objects. Each connection gets a new `HrnsACPAgent` with a scoped store view;
disconnect closes its sessions. Application shutdown drains connections before closing
storage.

The optional authentication dependency returns `Principal(id, agent_ids=None)` for HTTP and
WebSocket requests. The default principal is `local` with access to every registered agent.
Identity and workspace bounds come from the application, never from ACP metadata.

## Storage scope

Session rows are keyed by session id and every query includes `(principal, deployment_id)`.
The deployment id is the logical agent namespace; each row's `definition_version` pins the
immutable revision used to compose it.
SQLite and PostgreSQL use the same `acp_sessions` schema. A scoped store implements the
upstream `SessionStore` protocol; the adapter owns conversation state, while the host owns
cwd/title/effort/bindings/usage metadata.

Every HRNS-produced ACP `session/update` carries a public `_meta.hrns.update` object:

```json
{"schemaVersion": 1, "updateId": "server-id", "revision": 42}
```

`updateId` is stable and session-scoped. `revision` is a monotonically increasing durable
session position. The store reserves revision blocks transactionally before live delivery;
unused positions may create gaps, but positions are never reused after cancellation, failure,
disconnect, or restart. Replay preserves the same identity and revision. Public
`SessionUpdateLedger` and `order_session_updates` helpers reject identity/revision conflicts,
drop identical duplicates, and reconstruct committed order without arrival counters or payload
hashes.

For an already-live session, `session/load` subscribes the requesting connection before taking
the session turn lock and capturing the replay snapshot. Updates before the cut are therefore
in the transcript, updates after it arrive live, and overlap is safely identifiable. A replay
send, transcript validation, position reservation, or durable save failure aborts the load or
operation with a structured HRNS error; partial replay is not reported as complete. The HRNS
client buffers replay/live overlap until `session/load` returns, then publishes it in revision
order and discards an incomplete buffer on failure.

Hosted sessions add
`acp_session_leases` (owner, fence, expiry): a second live worker gets `SESSION_HELD`, an
expired lease is taken over with the next fence, and a save carrying a stale fence fails
with `SESSION_CONFLICT`. `acp_prompt_queue` records prompts before execution, marks a
cancelled/abandoned attempt `uncertain`, and links an explicit retry to the original.
Connections to one deployment share one host instance: `FanoutClient` broadcasts
`session/update` only to connections subscribed to that session and routes
response-bearing client calls to the connection that started the turn. Session updates
have no fallback after an unsubscribe; only local stdio may use its sole client, and an
in-flight client-origin call is not re-authorized mid-turn. A
second worker is refused with `SESSION_HELD` rather than attached; cross-worker routing is
the remaining distributed step. Queued prompts can be enqueued, drained in sequence
order, inspected, and explicitly retried after a restart. Session scope is the logical
agent, so a version bump does not orphan old sessions; the stored definition version pins
composition.

Session-scoped HRNS extension notifications are non-durable invalidation/presentation hints.
Authoritative extension state is loaded through its typed snapshot request
(`configuration/get`, `commands/list`, or `presentation/get`), so those notifications are not
part of the replay journal.

## ACP and transport

ACP v1 is the baseline. The network subprotocol `hrns.acp.v1` carries one JSON-RPC object
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
