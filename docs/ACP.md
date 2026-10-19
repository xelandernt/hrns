# ACP host behavior

`harness-host` serves one Harness agent deployment as a standard **ACP v1** agent over
stdio. Framework-native agents therefore work in any ACP client, and the project's
terminal can drive independent ACP agents with the same interaction model.

```
ACP client ──stdio JSON-RPC──► harness-host ──► SessionManager ──► Pydantic AI agent
                                    │                              + native capabilities
                                    └── _harness/* extension       + bound workspace/store
```

The process boundary is intentional: native plugins, model providers, and the runtime
never become client requirements. See [RUNTIME_API.md](RUNTIME_API.md) for the runtime
programmatic surface and [ACP_RUNTIME_CONTRACT.md](ACP_RUNTIME_CONTRACT.md) for the
adapter requirements and observed gaps.

## Standard ACP surface

| Method | Behavior |
| --- | --- |
| `initialize` | Protocol v1 negotiation; advertises load/list/delete/close session capabilities and the `_harness` extension |
| `session/new` | Validates `cwd` against host policy, applies `_meta.harness.initialConfiguration`, returns standard config options |
| `session/prompt` | Streams text, thoughts, tool activity, usage; bridges deferred tool approvals; returns stop reason and per-prompt usage |
| `session/cancel` | Cancels the active turn through the runtime's cancellation token; the prompt answers `cancelled` and the session stays usable |
| `session/load` | Reopens a stored session (pinned definition) and replays its persisted transcript |
| `session/list` | Lists this principal's/deployment's sessions with their stored working directory |
| `session/delete` | Deletes conversation, retry records, owned metadata; schedules runtime cleanup |
| `session/close` | Cancels/drains work and releases live resources, retaining the transcript |
| `session/set_config_option` | Standard capability toggles and model selection (see below) |
| `session/set_mode`, `session/fork`, `session/resume` | Not advertised; the SDK answers method-not-found |
| `authenticate` | No auth methods are advertised; an authenticate request is rejected as auth-required |

The installed SDK 0.11.1 defines `session/delete` in its method table but does not route
it from `build_agent_router`; the host registers that standard route explicitly, so an
advertised deletion capability is honest.

## Configuration options

Only simple, actual transitions are exposed as standard options:

* **Capability toggle** — `binding:<bindingId>` (boolean when the client advertises
  `clientCapabilities.session.configOptions.boolean`, otherwise a select with `on`/`off`).
  A binding appears only when it is `next_turn`, available, and the client may make the
  transition it would make now (enabled ⇒ `clientCanDisable`; disabled ⇒
  `clientCanEnable`). Mandatory bindings, unavailable bindings, and `new_session`
  bindings are not editable standard options; they are visible through the extension,
  with their policy and reasons.
* **Model** — `model` select, advertised only when the deployment explicitly offers
  models (`--model` / `--models`). The selection applies to the next turn and is stored
  with the session. A definition's internal `model` alias is never advertised implicitly.
* **new-session settings** — inspectable via `_harness/capabilities/list`; changing them
  on an open session returns `NEW_SESSION_REQUIRED`. They may be chosen at creation via
  `initialConfiguration`.

Every accepted change returns the complete option list, as ACP requires. Standard
option changes are serialized by the runtime's configuration transaction path and emit
`_harness/configuration/changed` when the client negotiated that notification.

### `initialConfiguration`

`session/new` may carry:

```json
{"_meta": {"harness": {"initialConfiguration": [
  {"type": "enable", "bindingId": "tickets", "enabled": true}
]}}}
```

The payload uses the same discriminated `enable` / `configure` / `add` change shapes as
the extension transaction and is validated by the same runtime policy controller with
creation-time lifecycle rules (so `new_session` settings are legal here). An invalid
selection fails the request and creates no session. Hosted workers reject `add` changes:
remote clients may only enable or configure bindings the deployment offers.

## Project extension (`_harness/`)

Initialization negotiates major version 1 through `_meta.harness`:
`{"version": 1, "methods": [...]}`. The agent advertises:

```json
{"_meta": {"harness": {"version": 1,
  "methods": ["_harness/capabilities/list", "_harness/configuration/get",
               "_harness/configuration/update", "_harness/configuration/changed",
               "_harness/plugins/list"],
  "localBindingCreation": true}}}
```

`_harness/plugins/list` and `localBindingCreation` are advertised only by hosts that
permit creating new bindings from the registered local catalog; a hosted worker omits
them. Clients invoke methods only when both sides advertised them; the
`configuration/changed` notification is sent only to clients that listed it.

| Method | Request | Response |
| --- | --- | --- |
| `_harness/capabilities/list` | `{sessionId, cursor?, limit?}` | `BindingListResponse`: `{schemaVersion, items, nextCursor?, selectedRevision, activeRevision?, policyRevision}` |
| `_harness/plugins/list` | `{cursor?, limit?}` | `PluginListResponse`: `{schemaVersion, items, nextCursor?}` (local hosts only) |
| `_harness/configuration/get` | `{sessionId}` | the complete `ConfigurationSnapshot` |
| `_harness/configuration/update` | `{sessionId, requestId, expectedRevision, changes}` | the complete updated `ConfigurationSnapshot` |
| `_harness/configuration/changed` (notification, agent→client) | the complete `ConfigurationSnapshot` | — |

List responses use the shared protocol DTOs and therefore carry their entries in
`items`. The bundled terminal consumes that canonical envelope; no client-identifying
compatibility aliases are emitted. `_harness/configuration/*` responses are exactly the
snapshot DTO — no host-specific fields are added, because snapshots are validated with the
strict shared model.
Application status is in the snapshot; model selection is exposed through the standard
`configOptions` list.

Binding descriptors carry identity, name/description, JSON configuration schema,
non-secret current values and defaults, availability reason, requires/conflicts,
lifecycle (`changeMode`) and state mode, presentation ids, and the policy
(`available`, `enabledByDefault`, `clientCanEnable`, `clientCanDisable`,
`editableFields`). Descriptors never contain executable code, imports, or credentials.

Transactions are atomic: all changes validate against current policy, schemas,
dependencies, and conflicts before any state is persisted. Idempotent selections are
no-ops. Retry identity is `(principal, sessionId, requestId)` with a canonical payload
hash; a stale `expectedRevision` returns `REVISION_CONFLICT` with the latest snapshot.

A prompt is one logical turn even when it spans approval-resume segments. Configuration
transactions (standard option changes and extension updates) issued while that prompt is
running or waiting on an approval dialog are serialized behind it and acknowledged after
the prompt completes, so the selected revision cannot change under a continuation and a
change always applies to the next turn.

## Permissions and deferred approvals

A tool marked `requires_approval` pauses the runtime turn through Pydantic AI's deferred
tool results. The host:

1. announces the tool call (`tool_call`, status pending) when the runtime signals it;
2. sends ACP `session/request_permission` with allow once/always and reject
   once/always options;
3. on approve, promotes the call to `in_progress`; on reject, resumes the run with
   `ToolDenied` so the model observes the refusal;
4. remembers `always` decisions for the rest of the ACP session, scoped to the tool name
   and canonical arguments (a different argument set asks again);
5. on a dismissed dialog (or `session/cancel` while waiting) ends the prompt as
   `cancelled`, marks outstanding tool calls failed, and settles the deferred segment
   with denials so the conversation history has no unresolved tool calls and the session
   remains usable.

External-tool execution (`DeferredToolRequests.calls`) is not supported over ACP; the
prompt fails with `CAPABILITY_UNAVAILABLE` naming the tools.

> Runtime requirement: the definition's agent must include `DeferredToolRequests` in its
> supported output types for approvals to surface (see the runtime-contract gap note).

## Sessions, working directories, and reload

The ACP `cwd` is validated and canonicalized (`realpath`) before any session work. In a
hosted worker it must be equal to or below one of `HARNESS_ALLOWED_CWDS`; symlinks are
resolved first, so a link that leaves the allowed roots is rejected with
`WORKSPACE_UNAVAILABLE`. `additionalDirectories` is not advertised and is rejected if
sent.

`cwd` is bound as the definition's workspace resource when the definition declares one;
the runtime records it with the session and converts a path binding to the native local
workspace backend. Reopening a session preserves the recorded working directory — the
client's `cwd` on `session/load` is ignored, never adopted. A stored session outside the
current allowed roots fails closed with `WORKSPACE_UNAVAILABLE`.

`session/load` replays the complete persisted transcript — user prompts, agent
text/thought chunks, tool starts and terminal statuses — before responding. A fresh client
has no local copy of earlier prompts, so the host emits standard `user_message_chunk`
updates. The bundled terminal treats those as replayed messages rather than new
submissions. An interrupted turn is replayed as a readable agent notice
(`⚠ Interrupted turn: …`) so a standard client can distinguish it from a completed turn. Sessions are owned by `(principal, deploymentId)`; a foreign session is
reported as unavailable, never leaked.
On stdio EOF the host closes live sessions and the manager so ownership claims are
released and another worker (or a restarted host) can load them.

## Hosted workers

The server launches a worker per ACP connection with trusted process configuration:

| Variable | Meaning |
| --- | --- |
| `HARNESS_HOSTED=1` | Hosted mode |
| `HARNESS_PRINCIPAL` | Authenticated principal |
| `HARNESS_DEPLOYMENT_ID` | Immutable deployment id |
| `HARNESS_ALLOWED_CWDS` | JSON array of canonical workspace roots |
| `HARNESS_STORE` | Durable session store path |

CLI flags for these values are rejected in hosted mode — identity is never taken from
ACP metadata. Hosted mode additionally prohibits local binding creation (`add`), hides
the plugin inventory, and rejects client-supplied MCP servers with `POLICY_DENIED`
(remote MCP endpoints are authorized by the deployment, not by the client). Local
sessions also reject client-supplied MCP servers in this revision: MCP is integrated
through declared definition resources, not arbitrary client commands.

## CLI

```text
harness-host [DEFINITION] [options]

  DEFINITION                    package.module:attribute, package.module, or manifest path
  --definition SOURCE           alternative spelling
  --manifest PATH               YAML/JSON definition manifest
  --store PATH                  SQLite store (WAL) enabling load/list/delete
  --model NAME                  default model id for this deployment
  --models a,b                  model ids offered to clients (adds the `model` option)
  --resource NAME=VALUE         named definition resource (repeatable)
  --allowed-cwd PATH            local workspace root (repeatable)
  --principal ID                local principal (default: local)
  --deployment-id ID            local deployment id (default: local)
  --runtime-module MODULE       deployment module exposing build_host_bundle(setup)
  --no-local-add                do not advertise local binding creation
  --hosted                      hosted worker; identity comes from HARNESS_* env
  --log-level LEVEL             stderr logging (stdout is protocol only)
```

Examples:

```bash
# The bundled deterministic demo (no credentials)
harness-host harness_runtime.examples:demo_definition --store ~/.local/share/harness/demo.sqlite3

# A Python definition from an installed package
harness-host my_agents.research:definition --store ./sessions.sqlite3 --models openai:gpt-5,openai:gpt-5-mini

# A declarative manifest
harness-host --manifest ./agents/researcher.yaml --store ./sessions.sqlite3
```

Stdout is reserved for newline-delimited JSON-RPC; all diagnostics go to stderr.
`harness-host` shuts down cleanly on stdin EOF.

## Errors

Application errors use JSON-RPC `-32000` with `error.data = {code, details}` and a
readable message. Stable codes include `POLICY_DENIED`, `INVALID_CONFIGURATION`,
`REVISION_CONFLICT`, `DEPENDENCY_CONFLICT`, `CAPABILITY_UNAVAILABLE`,
`NEW_SESSION_REQUIRED`, `SESSION_BUSY`, `SESSION_CLOSED`, `WORKSPACE_UNAVAILABLE`,
`RESOURCE_UNAVAILABLE`, `DEPLOYMENT_UNAVAILABLE`, and `TURN_FAILED`. Missing sessions and
malformed parameters use standard ACP invalid-params errors; unsupported methods use
method-not-found.

## Known limitations

* Client-supplied MCP servers are rejected (local: `INVALID_CONFIGURATION`; hosted:
  `POLICY_DENIED`) until the runtime exposes an explicit MCP resource binding.
* `additionalDirectories`, session modes, fork, and resume are not advertised or
  supported; the recovery path is `session/load`.
* Model selection is a host-owned option over the runtime's per-run model override; the
  runtime has no selected-model field in its snapshot or store yet. The value is returned
  in standard `configOptions`, not inside configuration snapshots.
* Configuration transactions queue behind an active prompt (see above); a client must
  answer `session/request_permission` independently rather than gating it on a pending
  configuration response.
* Structured outputs are not converted into a standard ACP representation; streamed
  text and tool activity are. Native structured results remain available to Python
  callers of the runtime.
* Usage summaries are attached to prompt responses; a context-window `usage_update` is
  emitted only when the runtime reports a window size.
* The host binds the ACP `cwd` only through a definition workspace resource; richer
  workspace/sandbox bindings are deployment resources supplied to `open_session` and
  must match the same name.

## Verification

`tests/test_acp_host.py` drives the adapter in-process (workspace policy, options,
transactions, presenters, permissions, cancellation, reload/delete) against the real
runtime with a deterministic `FunctionModel` deployment in
`tests/test_acp_runtime_double.py`. `tests/test_acp_subprocess.py` runs the actual
`harness-host` subprocess through the official ACP SDK client for initialize/new/prompt/
update/cancel/load/list/delete, permissions, hosted policy, and transcript replay across
a restart. No provider credentials are used.
