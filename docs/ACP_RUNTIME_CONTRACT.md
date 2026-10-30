# ACP runtime contract

Status: **implemented on the pinned upstream adapter**. ACP execution is owned by
`pydantic_ai_harness.experimental.acp.PydanticAIACPAgent`; hrns supplies composition,
policy, scoped persistence, and a small metadata surface.

## 1. Composition entry point

```python
from hrns.runtime.composition import compose_definition

composition = await compose_definition(
    definition,
    registry,
    resources=resources,
    session_id=session_id,
    principal=principal,
    deployment_id=deployment_id,
    initial_configuration=initial_changes,  # or saved_bindings=record.bindings
    model_settings=model_settings,
    effort=effort,
    effort_support=effort_support,
)
config = composition.config
```

`compose_definition` returns a native agent and an `AcpSessionConfig`. The ACP host passes
that config to the adapter's `session_config` callback. Local CLI, stdio host, and FastAPI
server all share this path.

## 2. Adapter hooks used

| Hook | Use |
| --- | --- |
| `session_config` | Compose initially per session; choose `deps`, capabilities, workspace |
| `session_store` | Scoped save/load of upstream `StoredSession` |
| `models` + `model_resolver` | Standard model selection and configured providers |
| `permission_policy` | Default tool-name + canonical-argument scope |
| `session_config` effort capability | Per-request reasoning effort via native model settings |
| public `list_sessions`/`delete_session` overrides | Host metadata surface |
| public `prompt`/`set_config_option` overrides | Title/usage metadata and effort option |
| `_ComposedAgent` run dispatch | Selects the live composition and re-applies current capabilities, toolsets, workspace, and deps after a capability edit |

The host does not read `_sessions`, `_conn`, or any other private adapter state. It does not
override private execution methods and does not implement a second turn loop.

## 3. Session behavior

- `session/new` validates `cwd` against host roots, rejects `additionalDirectories` and
  client MCP servers, parses `_meta.hrns.initialConfiguration`, and composes the initial agent.
  The session is staged in memory with no row: it is written on its first prompt or an
  explicit rename, so a session that is opened and never used is never listed.
- `session/load` subscribes before capturing a live replay cut, validates every public update
  position, and replays the transcript with its original server identity and revision.
- `session/prompt` delegates to the adapter. Before the turn runs, the host writes the staged
  session (or keeps the existing row) and records a fallback title (first prompt); after a
  committed turn it accumulates usage.
- `session/cancel` and `session/close` delegate to the adapter; close cancels and discards
  live conversation state.
- `session/list` and `session/delete` use the scoped host metadata table. Delete closes a live
  session before removing its row.
- Model selection uses the adapter's standard `model` option. Reasoning effort is an
  additional standard `effort` option.
- `_hrns/configuration/update` validates a complete change set, checks
  `expectedRevision`, and records request-id idempotency in memory. It rebuilds the native
  agent and mutates the existing `Composition` in place, preserving the `SessionDeps` object
  the adapter passes to each run.
- Changes are serialized behind a per-session prompt lock, so they take effect at the next
  turn boundary. `next_turn` bindings can be enabled/disabled/configured/added; `new_session`
  bindings still require a new session.
- `_hrns/plugins/list` is advertised only when local binding creation is enabled.
  `_hrns/configuration/changed` notifies the client of a committed snapshot.
- Revisions are in-memory per open session, not durable.

## 4. Persistence contract

`ScopedSessionStore` implements the upstream protocol for one `(principal, deployment_id)`:

```python
async def save(session_id: str, session: StoredSession) -> None: ...
async def load(session_id: str) -> StoredSession | None: ...
```

One `acp_sessions` row stores:

- native `ModelMessage` history (Pydantic JSON);
- ACP `session/update` transcript entries;
- selected model;
- cwd, title, effort, selected binding state, usage totals, timestamps.
- the next non-reusable session update revision.

Reads and writes include the scope in every SQL predicate. A session id from another principal
or deployment returns `None` and cannot be overwritten.

The wrapper also stages a composed session (`stage`/`staged`/`discard`) and writes it on
`materialize`. An empty `save` for a staged session is dropped: the adapter persists an empty
session at `session/new` so a client could reopen it, which is exactly how unused sessions
used to accumulate. A staged session is written by its first prompt, by a rename, or by its
first non-empty save, whichever comes first; a draft that is never used is discarded with the
connection.

HRNS overrides the adapter's best-effort save behavior: a committed transcript save failure is
a structured `SESSION_DURABILITY_FAILED` failure instead of success-shaped delivery. Each
transcript update carries `_meta.hrns.update = {schemaVersion, updateId, revision}`. SQLite and
PostgreSQL reserve revision blocks transactionally, so revisions may have gaps but are never
reused. The transcript JSON remains the journal; there is no separate turn table.

## 5. Feature decisions

| ID | Decision |
| --- | --- |
| D1 | Live next-turn managed configuration. `session/new` selects the initial set; `_hrns/configuration/update` applies validated edits at the next turn boundary. `new_session` lifecycle bindings still require a new session. |
| D2 | Live reasoning effort through a native capability returning per-request model settings. |
| D3 | Host metadata: list, delete, title, usage. Automatic model-generated titles removed. |
| D4 | Local workspace path only; no managed lease/job subsystem. |
| D5 | Native composition only; `SessionManager`/`Session` and runtime event journals removed. |
| D6 | Upstream save/load semantics; no custom durability engine. |

## 6. Error mapping

`HrnsError` becomes JSON-RPC `-32000` with `error.data = {code, details}`. Malformed
parameters use invalid-params; missing methods use method-not-found. Stable codes include
`POLICY_DENIED`, `INVALID_CONFIGURATION`, `CAPABILITY_UNAVAILABLE`, `SESSION_CLOSED`,
`WORKSPACE_UNAVAILABLE`, `DEPLOYMENT_UNAVAILABLE`, and `RESOURCE_UNAVAILABLE`.
