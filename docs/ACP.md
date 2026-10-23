# ACP host behavior

`harness-host` and `harness-local` serve native Pydantic AI agents as standard ACP v1
endpoints. Both use the upstream `PydanticAIACPAgent`, which owns prompts, updates,
approvals, cancellation, history, transcript replay, and session locks.

```
ACP client ──stdio JSON-RPC──► harness-host / harness-local
                                   │
                                   └── compose_definition -> native Agent
                                       + session_config -> AcpSessionConfig
                                       + scoped SessionStore
                                       + _harness configuration/metadata
```

## Standard ACP surface

| Method | Behavior |
| --- | --- |
| `initialize` | Negotiates v1; advertises load, list, delete, close, and `_harness` v1 |
| `session/new` | Validates `cwd`, parses `_meta.harness.initialConfiguration`, composes the initial agent |
| `session/prompt` | Streams text/thoughts/tools and returns stop reason plus usage |
| `session/cancel` | Cancels the active turn through the adapter |
| `session/load` | Restores native history/transcript from the scoped store and replays updates |
| `session/list` | Lists this principal/deployment's sessions and titles |
| `session/delete` | Closes live work and removes the stored session |
| `session/close` | Cancels/drains work and discards live conversation state |
| `session/set_config_option` | `model` and `effort` selections |
| `session/set_mode`, `session/fork`, `session/resume` | Not advertised |
| `authenticate` | No authentication method is advertised |

The installed ACP SDK defines `session/delete` but does not route it from
`build_agent_router`; the host registers that standard route explicitly.

## Configuration

Managed configuration is live again and applied on the next turn boundary:

- `session/new` may carry `_meta.harness.initialConfiguration` using the normal
  `enable`/`configure`/`add` change shapes. It is validated before a session is created.
- `_harness/configuration/update` accepts the same change shapes for an open session,
  checks `expectedRevision`, commits a validated change set, rebuilds the session's native
  agent, and emits `_harness/configuration/changed`.
- Changes are serialized behind a per-session turn lock. A prompt in progress finishes with
  its captured composition; the edit applies to the next prompt.
- `next_turn` bindings can be toggled, configured, or added. `new_session`-lifecycle bindings
  still fail with `NEW_SESSION_REQUIRED`.
- `add` is available only when the host advertises local binding creation. Local stdio hosts
  enable it; embedded hosted connections leave it disabled.
- Revisions and request-id idempotency are kept in memory for the live session. They are not
  stored in the database; reload starts a fresh revision sequence.
- If a disabled capability previously contributed function tools, the host keeps error-only
  declarations for those names from native history. Calling one returns `ToolFailed`; this
  prevents DeepSeek from leaking raw DSML markup when a tool disappears mid-conversation.
- `session/set_config_option` offers `model` when the deployment advertises models, and
  `effort` when the selected model can reason. Both apply from the next request. Effort is
  implemented by a native capability whose `get_model_settings` reads the current session
  state, so it changes without rebuilding the agent.

Model selection is persisted in the upstream `StoredSession`; effort and selected bindings
are host metadata.

## Configuration extension

Initialization advertises `_meta.harness` version 1 with:

```json
{"harness": {"version": 1, "methods": [
  "_harness/capabilities/list",
  "_harness/plugins/list",
  "_harness/configuration/get",
  "_harness/configuration/update",
  "_harness/configuration/changed",
  "_harness/session/rename",
  "_harness/session/steer"
], "localBindingCreation": true}}
```

`_harness/plugins/list` and `localBindingCreation: true` are present only for hosts that
permit adding bindings. A hosted connection advertises the remaining methods with
`localBindingCreation: false`.

| Method | Response |
| --- | --- |
| `_harness/capabilities/list` | `BindingListResponse` for the current selection |
| `_harness/plugins/list` | addable `PluginInfo` descriptors (local creation only) |
| `_harness/configuration/get` | the complete `ConfigurationSnapshot` |
| `_harness/configuration/update` | the committed snapshot after a validated edit |
| `_harness/configuration/changed` | host notification carrying the committed snapshot |
| `_harness/session/rename` | persists a title and emits `session_info_update` |
| `_harness/session/steer` | injects a follow-up into the running turn at its next model request |

Descriptors describe identity, config schema, current non-secret values, availability,
requirements/conflicts, lifecycle, and policy. No descriptor contains executable code or
credentials.

## Permissions and approvals

A tool that requires approval pauses the run through Pydantic AI deferred tool results. The
upstream adapter:

1. announces the tool call and sends `session/request_permission`;
2. offers allow once/always and reject once/always;
3. resumes with `ToolDenied` on rejection so the model observes it;
4. remembers `always` decisions for the session, scoped to tool name plus canonical
   arguments;
5. ends a dismissed/cancelled approval as `cancelled` and marks outstanding calls failed.

External tool execution (`DeferredToolRequests.calls`) is unsupported and fails with an
application error naming the tools.

## Sessions, cwd, and reload

The ACP `cwd` is canonicalized before use. In hosted mode it must be equal to or below one of
the deployment's allowed roots; symlinks are resolved first. `additionalDirectories` and
client-supplied MCP servers are rejected.

The session's `cwd` is the deployment's workspace resource when the definition declares one.
Reopening preserves the recorded directory and ignores the client's requested `cwd`. A
recorded path outside the current roots fails closed.

Ownership is `(principal, deployment_id)`. `session/load` and `session/delete` check the
scoped metadata row; a foreign session is never leaked. Delete closes active work first.

## Usage and titles

Per-turn usage comes from `PromptResponse.usage`. After each model response, the host also
publishes cumulative session usage in `SessionInfoUpdate._meta.harness.sessionUsage` with
`scope: "session"`, so clients can refresh token counts while tools and further model
requests are still running. The same response publishes a separate ACP `usage_update`
notification whose `used`/`size` are the context gauge for that single request (with the
per-request counters in `_meta.harness.contextUsage`), so clients can show context-window
fill without treating cumulative counters as a gauge. The session payload also carries
`cost` (a decimal string, present only
when Pydantic AI priced the response) and a derived `cost_status`: `estimated` when the total
includes a price, `unavailable` when the model has no known price. A missing price is never
reported as free. Final usage is reconciled once and persisted in metadata; measured tokens
and priced work remain counted if the turn is cancelled or fails. The first prompt supplies a
fallback title; clients may rename a session through `_harness/session/rename`. There is no
model-generated title tool.

## Stdio CLI

```text
harness-host [DEFINITION] [options]
  --definition SOURCE | --manifest PATH
  --store PATH
  --model NAME | --models a,b
  --resource NAME=VALUE
  --allowed-cwd PATH
  --principal ID | --deployment-id ID
  --runtime-module MODULE
  --hosted
  --log-level LEVEL
```

Stdout is reserved for JSON-RPC; diagnostics go to stderr. The host exits cleanly on stdin
EOF and releases the store.

## Errors

Application failures use JSON-RPC `-32000` with `error.data = {code, details}`. Stable codes
include `POLICY_DENIED`, `INVALID_CONFIGURATION`, `CAPABILITY_UNAVAILABLE`,
`SESSION_CLOSED`, `WORKSPACE_UNAVAILABLE`, `RESOURCE_UNAVAILABLE`, and
`DEPLOYMENT_UNAVAILABLE`. Missing sessions and malformed parameters use invalid-params;
unsupported methods use method-not-found.

## Known limitations

- Bindings declared `new_session` still require a new session; `next_turn` bindings can be
  edited live.
- Configuration revisions are in-memory per live session; a reload starts at revision 1.
- Images/audio prompt content is not advertised; embedded text resources are.
- Structured outputs are not converted to a standard ACP representation.
- Client-supplied MCP servers are rejected.
- Workspace execution is a local path; sandbox/lease management is deployment-owned.
- Durability follows the upstream save/load contract (see `ACP_RUNTIME_CONTRACT.md`).
