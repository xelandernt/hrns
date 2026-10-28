# ACP host behavior

`hrns-host` and `hrns-local` serve native Pydantic AI agents as standard ACP v1
endpoints. Both use the upstream `PydanticAIACPAgent`, which owns prompts, updates,
approvals, cancellation, history, transcript replay, and session locks.

```
ACP client ──stdio JSON-RPC──► hrns-host / hrns-local
                                   │
                                   └── compose_definition -> native Agent
                                       + session_config -> AcpSessionConfig
                                       + scoped SessionStore
                                       + _hrns configuration/metadata
```

## Standard ACP surface

| Method | Behavior |
| --- | --- |
| `initialize` | Negotiates v1; advertises load, list, delete, close, and `_hrns` v1 |
| `session/new` | Validates `cwd`, parses `_meta.hrns.initialConfiguration`, composes the initial agent |
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

## Client capabilities

ACP negotiates two independent capability sets in one handshake: the client sends
`clientCapabilities` on `initialize` and the agent answers with `agentCapabilities`. Neither
constrains the other, and one side having capabilities never stops the other from using its
own. A *client* capability is a grant for agent-to-client requests —
`session/request_permission`, `fs/read_text_file`, `fs/write_text_file`, the `terminal/*`
lifecycle, and `elicitation/create`.

hrns uses both directions. Host bindings act in the deployment workspace; *client-origin*
bindings ask the connected client to act instead. The two are distinguishable in every
descriptor, so a client can present them as different kinds of capability.

### What the terminal offers

The `hrns` terminal advertises `session.configOptions.boolean`, `plan`, and `elicitation`, and
answers `session/request_permission`, `elicitation/create`, `session/update`, and the `_hrns`
extension. Filesystem and terminal support are opt-in per connection:

| `--client-io` (or `clientIo` in a profile) | Advertised | Agent may |
| --- | --- | --- |
| `off` (default) | nothing | nothing; both methods answer method-not-found |
| `read` | `fs.readTextFile` | read files under the session `cwd` |
| `all` | adds `fs.writeTextFile` and `terminal` | also write files and run commands there |

Both are answered inside the session's working directory only: paths resolve against `cwd`,
symlinks are followed before the containment check, files larger than 2 MiB are refused rather
than truncated, writes are refused inside `.git` and for credential files, terminal output is
bounded at 1 MiB per terminal, and at most eight terminals stay open at once. Terminal support
means an agent runs commands as your user with no approval prompt: ACP has no permission
request for it, which is why it is off by default and lives on the connection profile rather
than on the agent.

The value is decided once, at `initialize`: the `--client-io` flag wins, then `clientIo` in the
profile, then the terminal's saved `/settings` value, and a change applies when you reconnect.
Nothing is advertised by default, and a handler that was not advertised answers
method-not-found rather than guessing.

The mode is a three-step ladder over the handlers this client implements, not a per-capability
switch: `all` unlocks filesystem writes and the terminal together. What a binding may require is
data, not code: `requiresClient` holds dotted paths into `clientCapabilities` (`terminal`,
`fs.read_text_file`, `elicitation.form`, ...), resolved against the `initialize` payload, and a
path the schema does not define is a registration error. Adding a client-side capability
therefore needs a plugin and, only if the family is new, a handler in `hrns/client/io.py` — the
availability and policy code never learns a capability's name.

### Client-origin bindings

`hrns.client_terminal` is the client-side counterpart of the coder's `shell`. It is offered as
the binding `client.terminal`, disabled by default, `origin: "client"`, `requiresClient:
["terminal"]`, lifecycle `next_turn`:

- It is **available only while the connected client advertises terminal support**; otherwise
  its descriptor carries an `availabilityReason` and an `enable` change fails with
  `CAPABILITY_UNAVAILABLE`. The host never silently substitutes host execution.
- It declares `shadows: ["shell"]`: while this binding is enabled the coder does not compose
  its `shell` tool, and its instructions name `run_command` instead. The coder's own `shell`
  setting is left exactly as configured, so disabling this binding brings the host shell back:
  the model always has exactly one way to run a command, and the other one is never lost.
- Its commands still need approval, because the host approves tool calls before they run; the
  client executes only what the model already asked for.

The bundled client deliberately does not offer a filesystem binding yet. `apply_changes`
batches edits with a preflight, credential guards, and content hashes, which
`fs/write_text_file` cannot express; replacing it would weaken edits rather than move them.

## Configuration

Managed configuration is live again and applied on the next turn boundary:

- `session/new` may carry `_meta.hrns.initialConfiguration` using the normal
  `enable`/`configure`/`add` change shapes. It is validated before a session is created.
- `_hrns/configuration/update` accepts the same change shapes for an open session,
  checks `expectedRevision`, commits a validated change set, rebuilds the session's native
  agent, and emits `_hrns/configuration/changed`.
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

Initialization advertises `_meta.hrns` version 1 with:

```json
{"hrns": {"version": 1, "methods": [
  "_hrns/capabilities/list",
  "_hrns/plugins/list",
  "_hrns/configuration/get",
  "_hrns/configuration/update",
  "_hrns/configuration/changed",
  "_hrns/session/rename",
  "_hrns/session/steer"
], "localBindingCreation": true}}
```

`_hrns/plugins/list` and `localBindingCreation: true` are present only for hosts that
permit adding bindings. A hosted connection advertises the remaining methods with
`localBindingCreation: false`.

| Method | Response |
| --- | --- |
| `_hrns/capabilities/list` | `BindingListResponse` for the current selection |
| `_hrns/plugins/list` | addable `PluginInfo` descriptors (local creation only) |
| `_hrns/configuration/get` | the complete `ConfigurationSnapshot` |
| `_hrns/configuration/update` | the committed snapshot after a validated edit |
| `_hrns/configuration/changed` | host notification carrying the committed snapshot |
| `_hrns/session/rename` | persists a title and emits `session_info_update` |
| `_hrns/session/steer` | injects a follow-up into the running turn at its next model request |

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
publishes cumulative session usage in `SessionInfoUpdate._meta.hrns.sessionUsage` with
`scope: "session"`, so clients can refresh token counts while tools and further model
requests are still running. The same response publishes a separate ACP `usage_update`
notification whose `used`/`size` are the context gauge for that single request (with the
per-request counters in `_meta.hrns.contextUsage`), so clients can show context-window
fill without treating cumulative counters as a gauge. The session payload also carries
`cost` (a decimal string, present only
when Pydantic AI priced the response) and a derived `cost_status`: `estimated` when the total
includes a price, `unavailable` when the model has no known price. A missing price is never
reported as free. Final usage is reconciled once and persisted in metadata; measured tokens
and priced work remain counted if the turn is cancelled or fails. The first prompt supplies a
fallback title; clients may rename a session through `_hrns/session/rename`. There is no
model-generated title tool.

## Stdio CLI

```text
hrns-host [DEFINITION] [options]
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
