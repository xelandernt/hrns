# ACP runtime contract (adapter requirements)

Status: **published for integration**. The ACP host in
`harness_runtime.acp` / `harness_runtime.host` consumes exactly the surface below, which
is the surface documented in [RUNTIME_API.md](RUNTIME_API.md). This file records the
adapter-facing details, the host-owned state that the runtime does not carry, and the
gaps observed while implementing the adapter.

The host never re-implements policy, composition, revisions, persistence, or event
translation; it only maps the runtime's decisions onto ACP v1 and the project
`_harness/*` extension.

## 1. Composition entry point

`harness-host` (and tests) compose these pieces:

```python
from harness_runtime import AgentDefinition, PluginRegistry, SessionManager, SessionStore
from harness_runtime.host import HostBundle

manager = SessionManager(
    registry,
    store=SessionStore(path_or_none),
    definitions=[definition],
    allow_local_binding_creation=not hosted,
)
bundle = HostBundle(definition=definition, registry=registry, manager=manager,
                    store_path=resolved_sqlite_path_or_none,
                    meta=optional_host_metadata_store)
agent = build_agent(bundle, setup)          # -> HarnessHostAgent (acp.Agent)
```

A deployment module may instead expose `build_host_bundle(setup: HostSetup) ->
HostBundle`; the CLI selects it with `--runtime-module module`. The built-in
`harness_runtime.host.build_local_bundle` is the default. `HostSetup` fields:
`definition` (import reference or manifest path), `store_path`, `model`, `models`,
`resources`, `hosted`, `principal`, `deployment_id`, `allowed_cwds`,
`local_binding_creation`.

Definition sources are `package.module:attribute`, `package.module` (attribute
`definition`), or a JSON/YAML manifest path. A definition module may expose a
deployment registry hook — `build_registry()` or `registry` — returning
a `PluginRegistry`; the host registers installed `harness.capabilities` entry points on
top and the runtime's bundled catalog remains resolvable lazily through `lookup()`.

## 2. Manager surface used

| Call | Use in the host |
| --- | --- |
| `manager.open_session(definition, resources=…, principal=…, deployment_id=…, initial_configuration=…, resume=None)` | `session/new` (and `session/load` via `resume`/`open_saved_session`) |
| `manager.open_saved_session(principal, session_id, resources=…, deployment_id=…)` | `session/load` |
| `manager.list_sessions(principal)` → `SessionSummary` list | `session/list` (host filters `deployment_id` and `summary.cwd`) |
| `manager.delete_session(principal, session_id)` | `session/delete` |
| `manager.close()` | stdio EOF teardown (drains sessions and releases the store/control claims) |

`open_session` is awaited directly (the host does not keep the dual-mode opener) and the
returned `Session` is closed on `session/close`, `session/delete`, reload, or EOF.

`SessionSummary.cwd` is used for `SessionInfo.cwd`; it is populated by the runtime from
the definition's workspace resource. The host binds the ACP-requested `cwd` as the
definition workspace resource (`resources[requirement.name] = cwd` for `kind ==
"workspace"`) unless the deployment bound a richer workspace under that name.

## 3. Session surface used

| Member | Use |
| --- | --- |
| `session.session_id` | ACP session id |
| `session.snapshot` → `ConfigurationSnapshot` | revisions/status and configuration responses |
| `session.capabilities(cursor, limit)` → `(descriptors, next_cursor)` | `_harness/capabilities/list` and standard options |
| `session.catalog.descriptors()` | complete descriptor list for standard config options and change-mode gating |
| `session.plugins(...)` | not used for the ACP method (no session id in `plugins/list`); the host pages `registry.catalog()` instead |
| `session.update_configuration(changes, request_id=…, expected_revision=…)` | extension update and standard `session/set_config_option` |
| `session.stream(prompt, model=…, deferred_tool_results=…)` → `RuntimeEvent` iterator | `session/prompt` |
| `session.cancel()` | `session/cancel` |
| `session.close()` | close/reload |
| `session.transcript()` | `session/load` replay |

Required event vocabulary (as emitted by `_events.py`): `text_delta` (`text`, `kind`,
`thought`), `tool_call` (`tool_call_id`, `tool_name`, `args`), `tool_update`
(`tool_call_id`, `status` ∈ completed/failed, `output`/`content`), `usage`,
`deferred_tool_requests` (`approvals[]`, `calls[]`), `turn_complete` (`usage`,
`deferred_tool_requests?`), `error` (`code`, `message`, `details`, with
`details.cancelled` for cancellation).

The host maps these to ACP `agent_message_chunk` / `agent_thought_chunk`,
`tool_call` / `tool_call_update`, `usage_update` (only when a context window size is
reported), `error` responses, and `PromptResponse.usage`. Presenters reuse the public
`pydantic_ai_harness.experimental.acp` helpers (`ToolCallPresentation`,
`default_coding_presenter`, `chain_presenters`); path absolutization is reimplemented in
the adapter because the upstream helper is not exported.

## 4. Host-owned state

The runtime does not carry two ACP concerns, so the host keeps a durable sidecar keyed by
session id (one atomic JSON file per session under `<store>.acp-sessions/`; in-memory
when the store is `:memory:` or absent):

* the canonical `cwd` of the ACP session (also recorded in `SessionRecord.cwd` by the
  runtime, and used from there when present);
* the client-selected model id (`session/set_config_option` `model`).

This sidecar is the only durable host-owned state. No live agents, capabilities, or
client objects are stored.

The host also serializes configuration transactions behind an active prompt (including
approval-resume segments) with the same per-session turn lock. The runtime captures one
selected revision per *segment*, so without this gate a transaction that landed while an
approval dialog was open would apply to the continuation of the same logical ACP prompt.
Deferred transactions are acknowledged after the prompt; a client must not gate its
permission answer on a pending configuration response.

## 5. Required semantics that the host relies on

1. `initial_configuration` is validated against the same policy controller as later
   transactions, with creation-time lifecycle rules; an invalid selection creates no
   session.
2. `update_configuration` with `expected_revision` raises `REVISION_CONFLICT` with
   `details.snapshot` = latest snapshot; request ids are idempotent.
3. A running turn keeps its active revision; changes apply to the next turn; the
   snapshot reports `active_revision`/`application_status`.
4. `stream` accepts `prompt=None` plus `deferred_tool_results` to resume a turn after
   approvals.
5. Cancellation ends the stream with an `error` event whose `details.cancelled` is true
   and leaves the session usable for the next prompt; `cancel()` may be called when no
   turn is active.
6. `close()`/`manager.close()` release the store and session control so another process
   can load the session (stale control claims block reload).
7. `transcript()` rows are `{seq, turn_id, kind, data}` with `kind` in
   `text_delta` / `thinking_delta` / `tool_call` / `tool_update` / `error` /
   `deferred_tool_requests` / custom kinds.

## 6. Gaps observed in the published runtime (0.1)

These are integration notes for the runtime owner; the host works around them where it
can and is ready to drop the workaround.

1. **Deferred approvals require an agent-level output type.** `Session.stream` does not
   add `DeferredToolRequests` to the output types of a factory/spec agent, so an agent
   whose tool `requires_approval=True` fails with "`DeferredToolRequests` is not among
   output types". The host implements the full ACP approval loop
   (`deferred_tool_requests` → `session/request_permission` →
   `DeferredToolResults` → resume), but a definition must bake
   `output_type=[str, DeferredToolRequests]` (or the runtime must append it when
   configured). A definition should declare both output types when it uses deferred approvals.
2. **Approval continuation is a new runtime turn.** The host resumes a paused prompt
   with `Session.stream(None, deferred_tool_results=...)`, which the runtime implements as
   a fresh turn capturing the (unchanged, because the host serializes configuration) active
   revision. Run-scoped capability state and the turn record from before the approval
   pause are therefore not preserved across segments of one logical ACP prompt. A runtime
   continuation that keeps the same turn/run state would let the host drop this caveat.
3. **Per-run model is applied twice for factories that declare `model`.** `_build_agent`
   passes `options.model` to the definition factory *and* `_run_kwargs` passes it as a
   Pydantic AI per-run override. With a factory alias that is not a Pydantic AI model
   name this fails (`Unknown model: …`). The host treats model ids as
   Pydantic-AI-resolvable values advertised explicitly with `--models`, never as internal
   definition aliases.
4. ~~**Committed transaction retries and expected revisions.**~~ **Resolved:** the
   runtime consults the request-id claim before the expected-revision check, so a
   completed retry returns its original committed snapshot. The hosted end-to-end test
   asserts this, including that a replay does not cancel owned jobs twice.
5. **`SessionSummary.cwd` migration.** Stores created before the `cwd` column rely on the
   host sidecar for `list`/`load`; both are supported.
6. **`plugins/list` scope.** There is no session-less runtime API for the registered
   plugin catalog; the host uses `registry.catalog()` directly and gates it on host
   policy. Fine for this revision, but a manager-level catalog accessor would avoid
   reaching around the session.
7. **Host-owned model selection.** The ACP-selected model remains in the sidecar rather
   than the runtime configuration snapshot. Session cwd reloads from the authoritative
   runtime record when the sidecar is unavailable (for example with PostgreSQL).

## 7. Error mapping

Every `HarnessError` becomes a JSON-RPC server error `-32000` with
`error.data = {"code": <stable code>, "details": {…}}` (exactly `HarnessError.to_rpc()`).
Missing sessions, malformed params, and method gaps use standard ACP errors
(`-32602` invalid params, `-32601` method not found). The host never puts application
metadata at the JSON-RPC error root.

Codes surfaced by the host: `POLICY_DENIED`, `INVALID_CONFIGURATION`,
`REVISION_CONFLICT`, `DEPENDENCY_CONFLICT`, `CAPABILITY_UNAVAILABLE`,
`NEW_SESSION_REQUIRED`, `SESSION_BUSY`, `SESSION_CLOSED`, `WORKSPACE_UNAVAILABLE`,
`RESOURCE_UNAVAILABLE`, `DEPLOYMENT_UNAVAILABLE`, and `TURN_FAILED` for an unexpected
turn failure that has no more specific runtime error.
