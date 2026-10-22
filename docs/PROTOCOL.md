# Wire protocol

ACP v1 is the agent/client protocol. This document records the project's wire additions and
transport framing. It does not redefine ACP.

## Extension negotiation

Clients and agents may advertise `_meta.harness` on `initialize`:

```json
{"harness": {"version": 1, "methods": ["_harness/capabilities/list"]}}
```

Only mutually advertised methods are invoked. A Harness host advertises:

```json
{"harness": {"version": 1, "methods": [
  "_harness/capabilities/list",
  "_harness/plugins/list",
  "_harness/configuration/get",
  "_harness/configuration/update",
  "_harness/configuration/changed",
  "_harness/session/rename",
  "_harness/commands/list",
  "_harness/commands/invoke",
  "_harness/commands/cancel",
  "_harness/commands/changed",
  "_harness/presentation/updated",
  "_harness/presentation/get"
], "localBindingCreation": true}}
```

`version` is an integer major version. Unknown versions disable the extension; standard ACP
continues to work. `localBindingCreation` is `true` for local stdio hosts and `false` for
embedded hosted connections. `_harness/plugins/list` is advertised only when creation is
enabled.

## Methods

### `_harness/capabilities/list`

Request:

```json
{"sessionId": "abc", "cursor": null, "limit": 50}
```

Response is a `BindingListResponse`:

```json
{
  "schemaVersion": 1,
  "items": [ /* BindingDescriptor */ ],
  "nextCursor": null,
  "selectedRevision": 1,
  "activeRevision": 1,
  "policyRevision": 1
}
```

Descriptors describe the current selection: identity, name/description, JSON config schema,
non-secret current values/defaults, availability, requirements/conflicts, lifecycle,
presentation ids, and policy (`available`, `enabledByDefault`, `clientCanToggle`,
`editableFields`, `changeMode`). Descriptors never contain executable code, imports,
credentials, or live objects.

### `_harness/configuration/get`

Request: `{"sessionId": "abc"}`

Response is the complete `ConfigurationSnapshot`:

```json
{
  "schemaVersion": 1,
  "sessionId": "abc",
  "selectedRevision": 1,
  "activeRevision": 1,
  "policyRevision": 1,
  "applicationStatus": "applied",
  "bindings": {
    "style": {"enabled": true, "config": {"style": "plain"}}
  }
}
```

The snapshot is the current selected state. `selectedRevision` increments after each
committed edit; `activeRevision` identifies the composition used by the active turn. Because
updates wait for the per-session turn lock, both are equal when a prompt starts.

### `_harness/configuration/update`

Request:

```json
{
  "sessionId": "abc",
  "requestId": "client-edit-1",
  "expectedRevision": 1,
  "changes": [
    {"type": "enable", "bindingId": "web_search", "enabled": false},
    {"type": "configure", "bindingId": "style", "fields": {"style": "punchy"}}
  ]
}
```

Response is the committed `ConfigurationSnapshot` with an incremented
`selectedRevision`. The host validates the whole change set, rebuilds the native agent,
persists the selected bindings, and emits `_harness/configuration/changed`.

- A stale `expectedRevision` returns `REVISION_CONFLICT` with the current snapshot.
- Reusing a `requestId` with the same payload returns the committed snapshot.
- Reusing a `requestId` with a different payload returns `INVALID_CONFIGURATION`.
- `add` is accepted only when `localBindingCreation` is advertised.
- `new_session`-lifecycle bindings return `NEW_SESSION_REQUIRED`.

### `_harness/plugins/list`

Request: `{"cursor": null, "limit": 50}`. Response is a `PluginListResponse` with addable
plugin descriptors. The method is available only when local binding creation is enabled.

### `_harness/configuration/changed`

Server notification:

```json
{"schemaVersion": 1, "sessionId": "abc", "selectedRevision": 2, ...}
```

The client applies the snapshot to its plugins panel. It is a notification, not a request.

### `_harness/session/rename`

Request:

```json
{"sessionId": "abc", "title": "Release notes"}
```

Response: `{"sessionId": "abc", "title": "Release notes"}`. The agent persists the title and
may emit a standard `session_info_update`.

### `_harness/commands/list`

Request: `{"sessionId": "abc", "cursor": null, "limit": 100}`. Response:
`{"commands": [{"bindingId": "git", "commandName": "status", ...}], "selectedRevision": 2,
"supportedOperations": ["read_history", "start_turn"]}`. Only enabled bindings in the current
composition contribute commands. These commands are not advertised through standard ACP
`available_commands_update`; an ordinary ACP client would send them as prompts.

### `_harness/commands/invoke`

Request contains `sessionId`, `bindingId`, `commandName`, `requestId`, `expectedRevision`, and
exactly one of `text` or `arguments`. Response is a `CommandInvokeResponse` whose result
separates an optional `presentation` from at most one `conversation` action
(`{"type": "start_turn", "prompt": ...}`). The host checks session ownership, enabled binding,
revision, and arguments before the handler runs. A busy session returns `COMMAND_BUSY`; a
duplicate `requestId` with identical payload replays one result, conflicting reuse fails.

### `_harness/commands/cancel`

Request: `{"sessionId": "abc", "requestId": "..."}`. Cancels one known invocation, including
one waiting to run. Response: `{"cancelled": true|false, "finished": false}`. Cancellation is
not retried automatically and ambiguous side effects are reported as such.

### `_harness/commands/changed`

Notification with the current `sessionId`, `selectedRevision`, command descriptors and
supported operations. Sent after a successful configuration change replaces command
registrations.

### `_harness/presentation/updated`

Notification carrying bounded `ToolPresentationSnapshot` entries keyed by `sessionId` and
`toolCallId`, with a monotonic per-call `revision`, optional `bindingId`/`runId`, and optional
`input`, `output`, `progress` presentation payloads. It supplements ordinary ACP tool updates;
it never replaces their status or content.

### `_harness/presentation/get`

Request: `{"sessionId": "abc", "toolCallIds": ["call-1"]}`. Response returns final snapshots
reconstructed from committed native `ToolReturn` metadata. Used only for passive replay after
`session/load`; no tool, command, or model is executed.

## Standard configuration options

`session/new` and `session/load` return `configOptions` containing:

- `model` (select) when the deployment offers model ids;
- `effort` (select) when the selected model supports reasoning.

`session/set_config_option` accepts one of those ids and returns the complete option list.
Both selections apply from the next request. Managed bindings are edited through the
`_harness/configuration/update` extension, not through standard config options.

## Initial configuration

`session/new` may carry:

```json
{"_meta": {"harness": {"initialConfiguration": [
  {"type": "enable", "bindingId": "tickets", "enabled": true}
]}}}
```

The payload uses the protocol's discriminated `enable` / `configure` / `add` change shapes and
is validated with creation-time lifecycle rules before a session exists. `add` is accepted
only when the host advertises local binding creation. Invalid selections fail the request and
create no session.

## Errors

Application errors use JSON-RPC `-32000`:

```json
{"code": -32000, "message": "...", "data": {"code": "INVALID_CONFIGURATION", "details": {}}}
```

Stable codes include `POLICY_DENIED`, `INVALID_CONFIGURATION`, `CAPABILITY_UNAVAILABLE`,
`SESSION_CLOSED`, `WORKSPACE_UNAVAILABLE`, `RESOURCE_UNAVAILABLE`, and
`DEPLOYMENT_UNAVAILABLE`. Missing sessions and malformed parameters use invalid-params;
unsupported methods use method-not-found.

## Network transport

The project WebSocket subprotocol is `harness.acp.v1`:

- one JSON-RPC object per UTF-8 text frame;
- no batches, binary frames, or duplicate keys;
- maximum frame size 16 MiB;
- finite JSON numbers only;
- receive backpressure and a 30-second outbound send timeout;
- 32 connections per server, four per principal.

The bridge converts each frame to/from the official ACP SDK's newline-delimited stream. It does
not start a subprocess and does not translate JSON-RPC payloads.

## Client behavior

- Extension methods are called only when advertised by both sides.
- The plugins panel is editable when `_harness/configuration/update` is advertised;
  add controls require `_harness/plugins/list` plus local binding creation.
- The client never automatically resends an uncertain prompt after reconnect.
- Session load uses standard `session/load`; a failed or unsupported load is reported rather
  than silently starting a fresh conversation.
