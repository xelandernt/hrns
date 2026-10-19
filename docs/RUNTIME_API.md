# Runtime API (published contract)

Status: **stable for integration**. This is the programmatic core owned by the runtime
component; ACP host, catalog, server, and Python callers program against exactly this
surface. Wire DTOs come from `harness_protocol`; the runtime never defines a second wire
model.

```python
from harness_runtime import (
    # definition + plugins
    AgentDefinition, DefinitionBinding, AgentSpec, DefinitionManifest,
    PluginRegistry, PluginDescriptor, CompatibilityConstraints,
    ResourceRequirement, ResourceSet,
    # runtime + sessions
    HarnessRuntime, SessionManager, Session,
    # re-exported protocol models used in signatures
    CapabilityPolicy, EnableChange, ConfigureChange, AddChange,
    BindingChange, ConfigurationSnapshot, BindingDescriptor, PluginInfo,
    RuntimeEvent, HarnessError,
)
```

Nothing here imports ACP, Textual, or the server; `harness_runtime.acp` / `.host` /
`.catalog` / `.starters` import this core and not the other way around.

---

## 1. Opening sessions

```python
manager = SessionManager(
    registry,                      # PluginRegistry
    store=None,                    # SessionStore (SQLite WAL) or None → host default db
    definitions=None,              # optional iterable[AgentDefinition]
    allow_local_binding_creation=False,  # host-advertised 'add' permission
    # --- optional managed workspace service (CONCEPT §13) ---
    workspaces=None,               # configured WorkspaceManager, or...
    workspace_providers=(),        # ...explicit WorkspaceProvider list (manager built here)
    workspace_binding=None,        # WorkspaceBinding(provider, mode, name) for new sessions
)
manager.register_definition(definition)          # duplicate (agent_id, version) rejected

opener = manager.open_session(
    definition,                    # AgentDefinition
    resources=ResourceSet({...}),  # resolved host resources; default empty
    store=None,                    # optional per-session SessionStore override
    principal="local",
    deployment_id="local",
    initial_configuration=None,    # list[BindingChange] applied against defaults before open
    session_id=None,               # explicit opaque id; generated otherwise
    resume=None,                   # stored summary/record (or id) to resume instead
)
session = await opener             # imperative form
# or
async with manager.open_session(...) as session: ...
```

`open_session` returns a dual-mode opener: it is awaitable (→ `Session`) and an async
context manager whose exit closes the session. Opening validates resources, plugins,
policy defaults, and the initial configuration **before** any session row or revision is
written. Initial selections receive the same lifecycle exception for `new_session`
settings, but host directional policy still applies: they cannot enable or disable a
binding the client would not be allowed to transition later. Definition revisions are
immutable: registering a second object under an existing `(agent_id, version)` with
different behavior is rejected rather than silently replacing the pin. Initial configuration uses the same change shapes and policy rules as later
transactions, with lifecycle validation appropriate to creation (`new_session` settings may
be chosen here). Invalid initial selections raise; the session is not created.

Resume after restart, preserving the definition pin and history:

```python
session = await manager.open_saved_session(
    principal="local",
    session_id=session_id,
    resources=resources,           # must be supplied again; nothing live is pickled
    deployment_id=None,           # when given, must match the stored pin
)
```

- Ownership is `(principal, deployment_id)`. A mismatched principal gets the exact error
  `POLICY_DENIED` with `details.reason='ownership'` (session IDs are not authorization
  tokens).
- Exclusive control is a store claim. A live owner is never stolen; a claim whose owner
  process died (crash/restart) is reclaimed automatically, and `stale_after` allows bounded
  reclaim of rows without PID information.
- If the pinned `(agent_id, definition_version)` is not registered on this host,
  `DEPLOYMENT_UNAVAILABLE` is raised; persisted transcript remains readable through the
  store for export.
- Only one live `Session` object may control a session in one process. A second
  `open_saved_session` for an already-open session raises `SESSION_BUSY`.

Other manager operations:

```python
await manager.list_sessions(principal)      # list[SessionSummary], filtered by principal
await manager.delete_session(principal, session_id)
# removes conversation, owned state rows, retry records; schedules resource cleanup
manager.store                               # the default SessionStore
await manager.close()                       # close live sessions + owned store
```

`SessionSummary` fields: `session_id, principal, deployment_id, agent_id,
definition_version, title, status, created_at, last_active_at, cwd, transcript`.
`cwd` is recorded at creation (workspace resource or conventional `workspace` binding), so
a host can reload the authoritative working directory; `transcript` is populated by
`store.load_record()` for replay. Host-facing store aliases also exist:
`load_record(session_id)`, `list_records(principal=..., deployment_id=...)`,
`delete_record(session_id) -> bool`

## 2. Session surface

| Member | Behavior |
| --- | --- |
| `session.session_id` | opaque stable id |
| `session.principal`, `.deployment_id`, `.agent_id`, `.definition_version` | pins |
| `session.snapshot` | current `ConfigurationSnapshot` (selected state; `active_revision`/`application_status` reflect the running turn) |
| `session.closed` | `True` after `close()` |
| `await session.run(prompt, ...)` | one turn; returns the native `AgentRunResult` (structured output preserved) |
| `session.stream(prompt, ...)` | async iterator of `RuntimeEvent`; ends after exactly one `turn_complete` or `error` |
| `await session.update_configuration(changes, request_id, expected_revision)` | atomic transaction; returns the new snapshot |
| `await session.cancel()` | requests cancellation of the active turn; the session stays busy until it settles |
| `await session.set_model(model, *, expected_revision=None, request_id=None)` | selects the model used by subsequent turns (session preference, not a revision) |
| `session.catalog` | live `PolicyCatalog`; `descriptors(active_revision=..., selected_revision=...)` for ACP adapters |
| `session.jobs` | `SessionJobManager` for explicitly registered work that outlives a turn |
| `session.register_job(binding_id, awaitable, ...)` | start owned work; disabled bindings are rejected |
| `session.register_external_job(binding_id, cancel=..., ...)` | track provider work until cancelled/accepted or completed |
| `await session.get_binding_state(binding_id, schema_version=...)` | retained capability state; survives disable/enable |
| `await session.set_binding_state(binding_id, state, schema_version=...)` | persist JSON state; a schema change is rejected |
| `await session.clear_binding_state(binding_id)` | explicitly forget state (never done by a toggle) |
| `session.workspace_lease` | managed `WorkspaceLease` bound to this session, when one was acquired |
| `await session.workspace_record()` | durable `WorkspaceRecord` identity, when retained |
| `await session.close(cancel_running=True)` | cancel/drain active work, release the managed workspace handle, release ownership, retain history |
| `session.capabilities(cursor=None, limit=50)` | `(list[BindingDescriptor], next_cursor)` |
| `session.plugins(cursor=None, limit=50)` | `(list[PluginInfo], next_cursor)`; `POLICY_DENIED` unless host allows local binding creation |
| `await session.transcript(turn_id=None)` | persisted renderable events (JSON dicts) |
| `await session.history()` | native `list[ModelMessage]` for inspection/export |

### 2.1 `run` / `stream` options

Both accept:

- `prompt: str | Sequence[UserContent] | list[dict]` — ACP prompt block mappings
  (`{"type": "text"|"image"|"audio"|"resource_link"|"resource", ...}`) are converted to
  native content parts; images/audio become `BinaryContent` so they remain model-visible.
- `output_type=None` — per-run structured output override where the native agent allows it
- `deps=None`, `model=None`, `instructions=None`, `retries=None`, `usage_limits=None`
- `deferred_tool_results=None` — resume with approvals/external results from a prior
  `deferred_tool_requests` event
- `turn_id=None` — caller-chosen stable id; generated otherwise
- `event_stream_handler=None` — optional additional native observer callback

A turn captures the **active revision** (immutable `ConfigurationSnapshot`) before
execution starts. The agent is newly constructed each turn from:

1. the definition's fixed behavior — `Agent.from_spec(spec, ...)` for manifest definitions,
   or the factory's returned agent for Python definitions; and
2. every enabled managed binding, instantiated fresh from its plugin factory in stable
   topological order, **exactly once per run**.

`Session` passes managed bindings to `agent.run(capabilities=[...])` /
`agent.run_stream_events(capabilities=[...])` unless the definition factory declares the
`managed_capabilities` parameter, in which case the factory binds them at
`Agent(capabilities=[...])` construction (required by self-delegating capabilities such as
`Coder` + `SubAgents`). Disabled bindings contribute nothing at all.

Errors:

- concurrent prompt → `SESSION_BUSY`
- closed session → `SESSION_CLOSED`
- selected composition cannot be prepared → `CAPABILITY_UNAVAILABLE` /
  `INVALID_CONFIGURATION`; the turn is **not** started, status becomes `failed`, and the
  selected state is retained for correction
- cancellation → `run` raises the native `RunCancelled` after recording the outcome;
  `stream` emits an `error` event with `details.cancelled=true` then stops
- other model/tool failures: `run` raises natively after the turn is marked failed and
  partial history is persisted; `stream` emits an `error` event then stops

### 2.2 Stream event types

Every event is `RuntimeEvent(type: str, data: dict)` (protocol model, `.to_wire()`).

| `type` | `data` |
| --- | --- |
| `text_delta` | `{text, part_index, kind: "text"|"thinking", thought: bool}` |
| `tool_call` | `{tool_call_id, tool_name, args, kind: "function"|"output"}` |
| `tool_update` | `{tool_call_id, tool_name, status: "pending"|"in_progress"|"completed"|"failed", outcome: "success"|"failed"|"denied"|"interrupted"|"retry", content, output, content_type}` |
| `usage` | `{requests, tool_calls, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}` |
| `deferred_tool_requests` | `{approvals: [{tool_call_id, tool_name, args}], calls: [...]}` |
| `turn_complete` | `{turn_id, run_id, conversation_id, active_revision, output, output_type, usage, deferred_tool_requests?}` |
| `error` | `{code, message, details, turn_id}` |

`output` in `turn_complete` is JSON-safe. For structured outputs `output_type` is the
qualified class name; callers needing the object use `run()`. A `usage` event precedes each
`turn_complete`. `status` on `tool_update` uses ACP's vocabulary; `outcome` preserves the
native `ToolReturnPart.outcome` (including `retry` for retry prompts). A turn that pauses on
deferred tools emits `deferred_tool_requests` and includes the same payload under
`turn_complete.deferred_tool_requests`; `run()` returns the native `DeferredToolRequests`
output, and the next `run(None, deferred_tool_results=...)`/`stream(None, ...)` resumes it.

### 2.3 Background work and disabling

Work that intentionally survives a turn must be registered with its owning binding
(`session.register_job` / `register_external_job`). Disabling a binding cancels and drains
that binding's jobs. If cancellation cannot be confirmed, the transaction fails with
`CAPABILITY_BUSY`; the binding stays enabled and the selected state is not acknowledged.
While a turn is active, disabling does not interrupt the current turn: jobs are drained at
the next turn boundary. External provider work remains tracked until its cancellation is
acknowledged or the caller marks it complete; submitting a toggle cannot undo external
effects already applied. `session.jobs.snapshot()` reports active owners and revisions.

## 3. Configuration transactions

```python
snapshot = await session.update_configuration(
    changes=[EnableChange(binding_id="shell", enabled=False),
             ConfigureChange(binding_id="web", fields={"max_results": 5})],
    request_id="client-chosen-idempotency-key",
    expected_revision=session.snapshot.selected_revision,
)
# The ACP adapter historically used positional (changes, expected_revision, request_id);
# that order remains accepted for compatibility.
```

- All changes validate atomically against current host policy, schemas, dependencies, and
  conflicts; either every change commits or none does.
- `expected_revision` is mandatory for the extension path; a stale value raises
  `REVISION_CONFLICT` with `details.snapshot` = latest snapshot. `None` skips the check
  (programmatic use only).
- Persistence of the new selected revision happens before acknowledgement. The running
  turn keeps its active revision; the new composition applies to the next turn.
  `ConfigurationSnapshot.bindings` always carries the *selected* state; `active_revision`
  and each descriptor's `active_enabled` reveal what the running turn still uses.
- Idempotent no-ops do not create a revision; they return the current snapshot.
- Retry identity is `(principal, session_id, request_id)` with a canonical payload hash:
  same body → original committed snapshot (even when the retry still carries its original
  `expected_revision`), different body → `INVALID_CONFIGURATION`; the same body while a
  claim is in progress → retryable `REVISION_CONFLICT`. The claim is consulted before the
  expected-revision comparison so completed retries are always idempotent.
- Directional permissions apply to actual transitions. `add` requires the host to advertise
  local binding creation (registry flag); its policy is assigned by the host.
- `new_session` bindings cannot be changed in an open session (`NEW_SESSION_REQUIRED`);
  they may be selected in `initial_configuration` at creation.

Error codes (from `HarnessError.code`): `POLICY_DENIED`, `INVALID_CONFIGURATION`,
`REVISION_CONFLICT`, `DEPENDENCY_CONFLICT`, `CAPABILITY_UNAVAILABLE`, `CAPABILITY_BUSY`,
`NEW_SESSION_REQUIRED`, `SESSION_BUSY`, `SESSION_CLOSED`, `WORKSPACE_UNAVAILABLE`,
`RESOURCE_UNAVAILABLE`, `DEPLOYMENT_UNAVAILABLE`.

## 4. Definitions

### Python

```python
from pydantic_ai import Agent

def build_agent(*, resources, active_config, managed_capabilities):
    return Agent(
        resources.get("model.default"),
        name="assistant",
        capabilities=[*managed_capabilities],       # required for self-delegation
    )

definition = AgentDefinition(
    agent_id="example.assistant",
    name="Example assistant",
    definition_version="1.0.0",
    factory=build_agent,
    bindings=[
        DefinitionBinding(binding_id="web", plugin_id="example.web", enabled=True),
    ],
    resources=[ResourceRequirement(name="model.default", kind="model")],
)
```

Factory keyword parameters are both optional and detected from the signature:
`resources`, `active_config`, `model`, `managed_capabilities`, `context` (a frozen
`AgentFactoryContext`), `principal`, `deployment_id`. A zero-argument factory is legal.
Factories that omit `managed_capabilities` receive managed bindings per run.

### Manifest (JSON/YAML)

```yaml
schema_version: 1
agent_id: example.researcher
name: Researcher
definition_version: "1.2.0"
model: openai:gpt-5
spec:                      # upstream AgentSpec; fixed behavior
  instructions: Research carefully.
  capabilities:
    - Thinking
bindings:
  - binding_id: web
    plugin_id: example.web
    config: {max_results: 5}
    enabled: true
    # Definition-level policy is preserved through YAML/Python export.
    policy:
      available: true
      enabled_by_default: true
      client_can_enable: true
      client_can_disable: true
      editable_fields: [max_results]
      change_mode: next_turn
resources:
  - {name: model.default, kind: model, required: true}
```

Load with `parse_manifest_data(dict)`, `parse_manifest_text(text, fmt=...)`, or
`manifest_from_file(path)`; convert to `AgentDefinition` via
`AgentDefinition(agent_id=..., spec=manifest.spec, bindings=..., resources=...)`.
Export with `definition.to_manifest()`; non-portable fields (factory, fixed Python
capabilities) raise `INVALID_CONFIGURATION` with `details.non_portable`.

## 5. Plugins

```python
descriptor = PluginDescriptor(
    plugin_id="example.web",
    version="0.3.0",
    description="Web search capability",
    config_model=WebConfig,                 # pydantic model → JSON schema for editable fields
    factory=build_web_capability,           # (validated_config, BindingContext) -> native capability
    requirements=["browser"],               # other binding IDs (or definition resource names)
    conflicts=["offline"],
    change_mode="next_turn",                # or "new_session" (default)
    state_mode="run",                       # run | session | external
    presentation=["web.search"],
    default_policy=CapabilityPolicy(
        available=True, enabled_by_default=False,
        client_can_enable=True, client_can_disable=True,
        editable_fields={"max_results"},
        change_mode="next_turn",
    ),
)
registry = PluginRegistry(allow_local_binding_creation=False)
registry.register(descriptor)
registry.register_import("example.lazy", "example.module:factory", version="1.0.0")
registry.discover()            # harness.capabilities entry points, lazy import refs only
registry.instantiate(descriptor, config, BindingContext(...))
```

`BindingContext` (frozen) carries `binding_id, plugin_id, session_id, principal,
deployment_id, binding_config, resources, active_configuration`. Factories must return a
native Pydantic AI capability; the runtime does not wrap capabilities into tools or
re-implement their behavior. Duplicate plugin ids are configuration errors. Discovery
imports nothing until a binding is composed. `registry.catalog()` → `list[PluginInfo]`.

## 5a. Managed workspace lifecycle

`harness_runtime.workspaces` owns workspace identity and lifetime; the runtime integrates it
without implementing a second sandbox abstraction.

- Configure either a completed `WorkspaceManager` (`workspaces=`) or explicit
  `workspace_providers=`; the latter builds a manager whose records are persisted in this
  session store. Managed sessions must use the manager's store; a per-session `store=`
  override is rejected when it would separate workspace identity from its manager. A prebuilt manager with an empty in-memory record store adopts the session
  store's callbacks; a manager wired by the host to its own durable store is left as-is.
- `workspace_binding=WorkspaceBinding(provider=..., mode=..., name=...)` selects the
  deployment provider for **new** sessions that declare a workspace requirement. The provider
  name is host configuration, never client input. Named workspaces imply `persistent`
  retention.
- Before a turn, the session acquires the lease and binds `lease.workspace` as the
  `workspace` resource; the native ``agent.run(workspace=...)`` path receives it unchanged.
  The identity record is stored in the session database (`workspace_records`).
- **No fallback:** when a managed workspace is configured and acquisition fails, or a
  persisted managed identity is missing, the session refuses to run with a host path or an
  empty environment. Errors are `WORKSPACE_UNAVAILABLE` with a concrete `details.reason`.
- `session.close()` calls the workspace provider's release/stop callback and retains the
  identity record (`released_at` set); a release failure keeps the session open and is
  retryable with `WORKSPACE_RELEASE_FAILED`.
- `manager.delete_session()` destroys the managed environment through the provider and only
  then removes the session rows and workspace identity. Without a configured manager, a
  session that owns a workspace is retained with `WORKSPACE_DELETE_UNSUPPORTED`.
- Reopening selects the provider/mode/name from the persisted record, verifies binding
  compatibility through the workspace manager, and raises `WORKSPACE_UNAVAILABLE` rather
  than creating a replacement when the saved workspace is gone.
- `await manager.cleanup_workspaces()` runs the workspace service's idle TTL sweep (persistent
  workspaces are only deleted explicitly).
- `SessionStore` exposes `get_workspace_record`, `save_workspace_record`,
  `delete_workspace_record`, `list_workspace_records`, and `workspace_record_callbacks()` for
  hosts that build the manager themselves.

## 6. Resources

`ResourceSet` is a named bag of resolved host objects (model, workspace, memory, store,
service). Requirements are declared on the definition (`ResourceRequirement`) and checked
before open and before each turn. `resource_set.get_workspace()` raises
`WORKSPACE_UNAVAILABLE`; other missing required kinds raise `RESOURCE_UNAVAILABLE`. A
workspace resource is passed through natively to `agent.run(workspace=...)` when present.
Secrets belong here, never in manifests or wire DTOs.

## 7. Persistence

`SessionStore` is SQLite in WAL mode by default (PostgreSQL is a server-side alternative
behind the same interface). It persists, transactionally:

- session ownership/pins/timestamps, status;
- every selected configuration revision (bindings JSON, policy revision, source) and
  committed transaction/retry records keyed by `(session_id, request_id)`;
- the authoritative working directory (`cwd`) and a managed-workspace marker for reload;
- turns (`turn_id`, index, active revision, status, prompt, output, usage, timestamps);
- native model message history (`ModelMessagesTypeAdapter` JSON, one row per message);
- explicitly persisted capability state, keyed by session and binding with its schema version;

- a renderable transcript (`text_delta` rows are coalesced per text block; tool events are
  journaled as they occur);
- session control rows for ownership claims;
- one retained workspace identity per managed session (`workspace_records`).

Recorded turn outcomes: `running`, `completed`, `failed`, `cancelled`. A crash leaves a
`running` row, surfaced as interrupted on reload; prompts are never retried automatically.
Closing retains history; deleting removes it. Nothing live (agents, capabilities, clients,
callables) is ever pickled or persisted.

## 8. Direct Python example

```python
async with manager.open_session(definition, resources=resources) as session:
    result = await session.run("Summarize the concept document.")
    print(result.output)

    async for event in session.stream("Now list the risks."):
        if event.type == "text_delta":
            print(event.data["text"], end="")

    snapshot = await session.update_configuration(
        [EnableChange(binding_id="web", enabled=False)],
        request_id="demo-1",
        expected_revision=session.snapshot.selected_revision,
    )
    print(snapshot.application_status, snapshot.selected_revision)
```


## 9. Host integration notes

- `harness_runtime.host` owns `HostBundle` (`definition, registry, manager, store_path,
  meta`) and its default `build_local_bundle(setup)` uses entry-point discovery for the
  explicitly offered plugin catalog. Because `PluginRegistry` resolves bundled catalog
  descriptors as a lazy lookup fallback, definitions may reference bundled upstream plugin
  ids even when the host did not register them for local binding creation.
- `harness_runtime.session.build_host_bundle(setup)` is the fully catalog-backed
  alternative for `harness-host --runtime-module harness_runtime.session`: it composes
  `harness_runtime.catalog.build_catalog_registry()` (bundled entries explicitly
  registered, so they are also offered for local add) plus installed
  `harness.capabilities` entry points. Deployments that offer catalog capabilities for
  client-side add should use it, or construct `SessionManager` over a catalog registry
  directly as `examples.py` does.
- Host session resources may bind a plain `workspace` path and a `model` value. The runtime
  aliases `workspace`/`model` to definition requirements named more specifically
  (`workspace.main`, `model.default`) and maps a path workspace to the native
  `LocalWorkspaceBackend`; rich workspace objects pass through unchanged.
- The ACP adapter should fetch descriptors from `session.catalog.descriptors(...)` (or the
  paginated `session.capabilities()`) and use `session.snapshot` for the selected state.
- `harness_runtime.acp` maps runtime events to ACP updates; `tool_update.status` follows
  ACP's vocabulary and `text_delta.thought` marks reasoning deltas.
