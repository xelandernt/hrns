# Architecture

```text
Python application ──────────────────────────────┐
                                                ▼
Textual / headless client ── ACP stdio ──► harness-host / harness-local
         │                                       │
         └── harness.acp.v1 WebSocket ──► FastAPI AgentServer
                                                 │
                                                 ▼
                                   compose_definition(...)
                                   native Pydantic AI Agent
                                   + fixed + managed capabilities
                                   + local workspace path
                                                 │
                                                 ▼
                                   PydanticAIACPAgent (upstream)
                                   execution, history, approvals,
                                   cancellation, transcript replay
                                                 │
                                                 ▼
                                   SQLite / PostgreSQL
                                   acp_sessions (StoredSession + metadata)
```

## Module ownership

| Module | Owns | Deliberately independent of |
| --- | --- | --- |
| `harness.protocol` | Extension DTOs, errors, framing, pagination | AI runtime, terminal, server |
| `harness.config` | Shared local settings | AI runtime, terminal, server |
| `harness.runtime` | Definitions, registry, policy, resources, native composition, persistence | Terminal and server |
| `harness.runtime.host` | ACP adapter integration, host policy, scoped storage, CLI | Terminal |
| `harness.client` | ACP connections, profiles, terminal, headless runner | Native agent code and provider SDKs |
| `harness.server` | FastAPI registration, authentication, catalog, in-process ACP | Terminal |

Installing the `client` extra does not install Pydantic AI. The `server` extra includes the
runtime and executes definitions in the FastAPI process. Each connection gets its own
`HarnessACPAgent`; the server owns one explicit shared store.

## Execution ownership

`PydanticAIACPAgent` owns every ACP session: prompt execution, streaming updates, tool
approval pauses, cancellation, session locks, conversation history, transcript replay, and
model selection. Harness does not run a second turn loop and does not keep a separate
transcript journal.

`compose_definition` initially builds the native agent and its `AcpSessionConfig` per session:

- definition `AgentSpec` or factory, plus fixed capabilities;
- enabled managed bindings instantiated from the plugin registry;
- resources resolved from the deployment and the client's working directory;
- an `EffortState` capability whose model settings are evaluated per request so effort can
  change while a session stays open;
- a local workspace backend rooted at the session's `cwd`, when the definition declares a
  workspace resource.

`session/new` accepts `_meta.harness.initialConfiguration` (enable/configure/add where policy
permits). An open session can be edited through `_harness/configuration/update`: the host
validates the change set, rebuilds the native agent, and mutates the existing `Composition`
in place so the adapter's next run uses the new capabilities, toolsets, and workspace.
Changes are serialized behind a per-session turn lock and apply at the next turn boundary.
`new_session`-lifecycle bindings still require a new session. Model and reasoning effort are
standard ACP config options and apply from the next request.

## State and persistence

The upstream `StoredSession` is persisted through the upstream `SessionStore` protocol. One
`acp_sessions` row holds native model messages, the client-visible update transcript, the
selected model, and host metadata: cwd, title, effort, selected bindings, usage totals, and
timestamps. Reads and writes go through a `ScopedSessionStore` bound to one principal and one
registered deployment, so another scope cannot load or overwrite a session.

The store is deliberately not a second orchestration engine. `save` is called after each
committed turn; a save failure is logged and the next successful save catches up. There is no
transactional turn commit, cross-connection ownership claim, turn table, revision log, or
background-job ledger. Simultaneous writers are last-write-wins. Existing databases are
opened without conversion; only the `acp_sessions` table is created or used.

## Resources and lifecycle

A workspace resource is a local path in this revision. `resolve_workspace_value` maps it to a
native `LocalWorkspaceBackend` and the adapter applies it to every run. No managed lease,
sandbox, or background-job subsystem is currently provided. ACP session close cancels the
active turn through the adapter; resource cleanup beyond the local backend is owned by the
deployment.

## Protocol versions

ACP v1 is the baseline. `_harness/*` methods are version-one project extensions negotiated
through `_meta.harness`:

- `_harness/capabilities/list` — current binding descriptors;
- `_harness/plugins/list` — addable plugins when local creation is enabled;
- `_harness/configuration/get` — the session's current configuration snapshot;
- `_harness/configuration/update` — validated next-turn capability edits;
- `_harness/configuration/changed` — notification carrying a committed snapshot;
- `_harness/session/rename` — a persistent title.

Standard model and effort options remain editable throughout. The `harness.acp.v1` WebSocket
subprotocol carries one JSON-RPC object per text frame with a 16 MiB limit.

See [contracts](CONTRACTS.md), [ACP behavior](ACP.md), [server operation](SERVER.md), and
[catalog coverage](CATALOG.md).
