# Harness protocol v1

Audience: third-party client implementers. This document describes the wire contract of the
`harness-protocol` package and the project-owned `_harness/*` ACP extension. The standard ACP v1
surface remains the baseline; the extension is optional.

> **Status.** The DTOs, constants, and helpers described here are implemented in
> `packages/harness-protocol` and exercised by `tests/test_protocol.py`. Host, client, and runtime
> integration is still in progress. Except for model validation and the framing/hash helpers, the
> behavior in this document is a contract obligation, not a verified runtime result. Nothing here
> claims ACP conformance or interoperability with any third-party implementation.

## 1. Layers, names, and constants

`_harness/*` is a project-owned method namespace, not an ACP standard namespace. ACP extension
methods start with `_`, so it is legal for an ACP peer to ignore the whole surface.

| Item | Value | Meaning |
| --- | --- | --- |
| ACP protocol | `1` | Standard ACP v1 baseline. ACP v2 is out of scope. |
| Extension version | `1` | `ExtensionAdvertisement.version`; independently versioned. |
| DTO schema version | `1` | `schemaVersion` on versioned request/response DTOs. |
| WebSocket subprotocol | `harness.acp.v1` | Exact `Sec-WebSocket-Protocol` value. |
| Maximum message | `16 * 1024 * 1024` bytes | `MAX_MESSAGE_BYTES`; UTF-8 encoded frame size. |
| Ping interval | 30 seconds | `PING_INTERVAL`. |
| Pong timeout | 30 seconds | `PING_TIMEOUT`. |
| Extension methods | `_harness/capabilities/list`, `_harness/plugins/list`, `_harness/configuration/get`, `_harness/configuration/update`, `_harness/configuration/changed` | `EXTENSION_METHODS`. |

All payloads are metadata. No extension DTO carries executable code, arbitrary imports, secrets, or
credentials. Credentials belong to host resources, never descriptors or configuration values.

## 2. Initialization and extension negotiation

The standard ACP `initialize` request/response is still required. The extension is advertised with
an `ExtensionAdvertisement` under the `harness` key of an ACP `_meta` object:

- Client advertisement: `initialize.params.clientCapabilities._meta.harness`.
- Agent advertisement: `initialize.result.agentCapabilities._meta.harness`.

For ACP implementations that expose request/response-level metadata, the equivalent locations
`initialize.params._meta.harness` and `initialize.result._meta.harness` are also accepted. A peer
should accept either placement and must not emit conflicting copies. The capability-object
placement is canonical.

Client `initialize` request (standard ACP fields shown for context):

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": 1,
    "clientCapabilities": {
      "_meta": {
        "harness": {
          "version": 1,
          "methods": [
            "_harness/capabilities/list",
            "_harness/plugins/list",
            "_harness/configuration/get",
            "_harness/configuration/update",
            "_harness/configuration/changed"
          ],
          "localBindingCreation": false
        }
      }
    },
    "clientInfo": {"name": "example-client", "version": "1.0.0"}
  }
}
```

Agent `initialize` response for a **local** host that permits new bindings:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": 1,
    "agentCapabilities": {
      "loadSession": true,
      "_meta": {
        "harness": {
          "version": 1,
          "methods": [
            "_harness/capabilities/list",
            "_harness/plugins/list",
            "_harness/configuration/get",
            "_harness/configuration/update",
            "_harness/configuration/changed"
          ],
          "localBindingCreation": true
        }
      }
    },
    "agentInfo": {"name": "harness-host", "version": "0.1.0"},
    "authMethods": []
  }
}
```

An agent that does not permit local binding creation omits `_harness/plugins/list` from `methods`
and sets `localBindingCreation` to `false` (or omits it; the default is `false`). The standard ACP
fields above are illustrative; their full rules come from ACP, not this document.

Negotiation rules:

1. `version` must be the integer `1`. Missing, malformed, or non-1 metadata means the extension is
   unavailable for that connection.
2. A client may call a method only when the agent advertised it **and** the client understands it.
3. An agent may send `_harness/configuration/changed` only when the client advertised it.
4. `localBindingCreation` is meaningful only on the agent advertisement. It is true only when the
   host permits creating new bindings from its registered plugin catalog, and it requires
   `_harness/plugins/list` in `methods`.
5. Failure to negotiate the extension never fails standard ACP initialization. Both peers continue
   with standard ACP behavior (section 9).

### 2.1 Initial configuration at `session/new`

A native client may include initial selections in
`session/new.params._meta.harness.initialConfiguration`. The value is an array of the same
discriminated change objects used by `_harness/configuration/update`, without `requestId` or
`expectedRevision`:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "session/new",
  "params": {
    "cwd": "/workspace",
    "mcpServers": [],
    "_meta": {
      "harness": {
        "initialConfiguration": [
          {"type": "enable", "bindingId": "web_search", "enabled": true},
          {"type": "configure", "bindingId": "web_fetch", "fields": {"timeoutSeconds": 30}},
          {"type": "add", "bindingId": "mcp.github", "pluginId": "harness.mcp", "config": {"server": "github"}, "enabled": true}
        ]
      }
    }
  }
}
```

The host validates these changes against deployment defaults, host policy, dependencies, and
schemas before creating the session. This is the only point at which `new_session` settings can be
chosen. An ordinary ACP client that sends no extension receives deployment defaults. If
`localBindingCreation` is not enabled, the host must reject `add` (typically `POLICY_DENIED`).

## 3. Wire conventions

- Wire field names are camelCase; Python fields are snake_case. The Pydantic models accept both
  through aliases and emit camelCase with `.to_wire()`.
- The versioned DTOs use `extra="forbid"`: unknown fields, including unsolicited imports or
  executable metadata, are rejected.
- `.to_wire()` emits JSON-safe camelCase and **omits `None` fields**. Explicit `null` is accepted
  only where a field is nullable. Values inside `config` and `fields` maps are not dropped; a
  nullable configuration field may therefore be set to `null` with `{"proxy": null}`.
- DTO `schemaVersion` is exactly `1`. It is independent of the extension version and of the ACP
  protocol version.
- `bindingId` and `AddChange.pluginId` match `^[a-zA-Z0-9_.:-]+$`, 1–200 characters.
- `requestId` is 1–200 characters. `ConfigurationUpdate.changes` has 1–100 entries.
- List `limit` is 1–100, default 50.
- Every frame is one complete JSON-RPC 2.0 object. Batching, binary frames, duplicate JSON keys,
  `NaN`, and infinities are invalid.

### 3.1 Changes are discriminated by `type`, not `action`

`BindingChange` is a discriminated union on the field **`type`**. There is no `action` field and no
fallback if `type` is missing or unknown. The three v1 shapes are:

```json
{"type": "enable", "bindingId": "web_search", "enabled": false}
```

```json
{"type": "configure", "bindingId": "web_fetch", "fields": {"timeoutSeconds": 30, "proxy": null}}
```

```json
{"type": "add", "bindingId": "mcp.github", "pluginId": "harness.mcp", "config": {"server": "github"}, "enabled": true}
```

`enable` toggles an existing binding. `configure` supplies field replacements: fields not listed
are unchanged; an explicit `null` sets a nullable field to null. `add` creates a new binding and is
local-only (section 4.2). `AddChange.config` defaults to `{}` and `enabled` defaults to `true`.

A transaction is validated as a whole before anything is committed. It is atomic: either all
changes commit or none do. Enabling a binding does not silently enable its dependencies; the host
returns the missing requirements (`DEPENDENCY_CONFLICT`) so the client can submit one explicit
transaction. Conversely, disabling a binding that enabled dependents require is rejected unless
the same transaction disables those dependents. Duplicate or ambiguous changes for the same
binding in one transaction are invalid. `add` never accepts policy, credentials, or code; the host
assigns policy and lifecycle from its registry and deployment configuration.

### 3.2 List responses use `items` and `nextCursor`

Both list methods return the versioned envelope `{schemaVersion, items, nextCursor?}`. They do not
return `bindings` or `plugins` keys.

| Response | Fields |
| --- | --- |
| `BindingListResponse` | `schemaVersion`, `items: BindingDescriptor[]`, `nextCursor?`, `selectedRevision`, `activeRevision`, `policyRevision` |
| `PluginListResponse` | `schemaVersion`, `items: PluginInfo[]`, `nextCursor?` |

`nextCursor` is opaque. Echo it unchanged to request the next page; do not parse or construct it.
Its absence means the last page, regardless of how many items were returned. An invalid cursor,
unknown cursor shape, or out-of-range limit is `INVALID_CONFIGURATION`. Revisions are current at
response time and are repeated on every binding-list page.

## 4. Extension methods

### 4.1 `_harness/capabilities/list`

Returns the session's offered binding descriptors plus its current revisions. `sessionId` is
required by this method even though the shared `ListRequest` Python model makes it optional for
reuse by `plugins/list`.

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "_harness/capabilities/list",
  "params": {
    "schemaVersion": 1,
    "sessionId": "sess_01J8Z3K4",
    "limit": 50
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "schemaVersion": 1,
    "items": [
      {
        "schemaVersion": 1,
        "bindingId": "web_search",
        "pluginId": "example.web_search",
        "name": "Web search",
        "description": "Search the public web.",
        "version": "1.2.0",
        "configSchema": {"type": "object", "properties": {"provider": {"type": "string"}}},
        "config": {"provider": "duckduckgo"},
        "defaults": {"provider": "duckduckgo"},
        "enabled": true,
        "activeEnabled": true,
        "policy": {
          "available": true,
          "enabledByDefault": true,
          "clientCanEnable": true,
          "clientCanDisable": true,
          "editableFields": ["provider"],
          "changeMode": "next_turn"
        },
        "changeMode": "next_turn",
        "stateMode": "run",
        "requires": [],
        "conflicts": [],
        "presentation": ["generic"]
      }
    ],
    "nextCursor": "eyJ2IjogMSwgIm9mZnNldCI6IDJ9",
    "selectedRevision": 4,
    "activeRevision": 3,
    "policyRevision": 2
  }
}
```

`enabled` and `config` are the accepted **selected** state. `activeEnabled` is the current
**active-turn** state and may differ. `policy.changeMode` is authoritative; the top-level
`changeMode` is a validated convenience mirror and cannot override it. The descriptor contains no
secrets; `configSchema` describes only editable, non-secret fields.

### 4.2 `_harness/plugins/list` and local binding creation

Lists registered plugins that the host permits a client to turn into new bindings. This method is
advertised and served only when the host has `localBindingCreation: true`. A normal ACP client
should never call it on a host that did not advertise it; such a call is an unnegotiated method and
is rejected with the standard `-32601` error.

Request (sessionless; `sessionId` is not part of this method):

```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "_harness/plugins/list",
  "params": {"schemaVersion": 1, "limit": 50}
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "result": {
    "schemaVersion": 1,
    "items": [
      {
        "schemaVersion": 1,
        "pluginId": "example.web_search",
        "version": "1.2.0",
        "description": "Search the public web.",
        "configSchema": {"type": "object", "properties": {"provider": {"type": "string"}}},
        "changeMode": "next_turn",
        "stateMode": "run",
        "runtimeCompatible": true,
        "managed": true,
        "presentation": "generic",
        "requirements": []
      }
    ],
    "nextCursor": "eyJ2IjogMSwgIm9mZnNldCI6IDJ9"
  }
}
```

Rules:

- Local installation/registration is an operator action. A client may `add` an already installed,
  registered plugin; it cannot upload code, pass an import path, install a package, or specify a
  policy.
- The host assigns the new binding's policy, lifecycle, and configuration schema. `bindingId` must
  be new within the session; `pluginId` must identify a registered plugin.
- A remote or hosted agent advertises only the bindings already offered by its deployment. Remote
  clients may enable, disable, or configure those bindings; they may not add bindings or see the
  server's installation catalog.
- The distinction is host configuration, not transport. A WebSocket connection does not by itself
  forbid local creation, and a stdio connection does not by itself permit it.
- `PluginInfo` is metadata only: identity, version, schema, lifecycle, compatibility, and
  presentation hints. It never contains credentials, plugin code, or arbitrary imports.

### 4.3 `_harness/configuration/get`

Returns the complete non-secret selected snapshot and revision state. There is no separate request
DTO; the v1 request params contain only `sessionId`.

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "_harness/configuration/get",
  "params": {"sessionId": "sess_01J8Z3K4"}
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "result": {
    "schemaVersion": 1,
    "sessionId": "sess_01J8Z3K4",
    "selectedRevision": 4,
    "activeRevision": 3,
    "policyRevision": 2,
    "applicationStatus": "pending",
    "bindings": {
      "web_search": {"enabled": true, "config": {"provider": "duckduckgo"}},
      "web_fetch": {"enabled": true, "config": {"timeoutSeconds": 20}},
      "shell": {"enabled": true, "config": {}}
    }
  }
}
```

`bindings` is the complete map keyed by `bindingId`; disabled bindings remain present with their
retained configuration. `activeRevision` may be absent or null before the first turn captures an
active revision.

### 4.4 `_harness/configuration/update`

Applies one atomic configuration transaction. `expectedRevision` is the `selectedRevision` the
client last observed. The transaction is rejected if it is stale.

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "method": "_harness/configuration/update",
  "params": {
    "schemaVersion": 1,
    "sessionId": "sess_01J8Z3K4",
    "requestId": "cfg-01J8Z4M2Q7",
    "expectedRevision": 4,
    "changes": [
      {"type": "enable", "bindingId": "web_search", "enabled": false},
      {"type": "configure", "bindingId": "web_fetch", "fields": {"timeoutSeconds": 30, "proxy": null}}
    ]
  }
}
```

Success response is the complete updated selected snapshot:

```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "result": {
    "schemaVersion": 1,
    "sessionId": "sess_01J8Z3K4",
    "selectedRevision": 5,
    "activeRevision": 3,
    "policyRevision": 2,
    "applicationStatus": "pending",
    "bindings": {
      "web_search": {"enabled": false, "config": {"provider": "duckduckgo"}},
      "web_fetch": {"enabled": true, "config": {"timeoutSeconds": 30, "proxy": null}},
      "shell": {"enabled": true, "config": {}}
    }
  }
}
```

The response acknowledges the accepted selected state; it does not mean the change is active in a
running turn. See section 5. A change whose target mode is `new_session` is rejected with
`NEW_SESSION_REQUIRED`; create a new session with `initialConfiguration` instead. A transaction
that changes nothing because the selected state already matches is an idempotent no-op and should
return the existing snapshot without inventing a new revision.

### 4.5 `_harness/configuration/changed`

An agent-sent JSON-RPC notification. It has no `id` and receives no response. `params` is the
complete `ConfigurationSnapshot` itself; there is no `snapshot` wrapper. It is sent when the
selected snapshot changes, when a new active revision is applied, when application fails, and when
`policyRevision` changes.

```json
{
  "jsonrpc": "2.0",
  "method": "_harness/configuration/changed",
  "params": {
    "schemaVersion": 1,
    "sessionId": "sess_01J8Z3K4",
    "selectedRevision": 5,
    "activeRevision": 5,
    "policyRevision": 2,
    "applicationStatus": "applied",
    "bindings": {
      "web_search": {"enabled": false, "config": {"provider": "duckduckgo"}},
      "web_fetch": {"enabled": true, "config": {"timeoutSeconds": 30, "proxy": null}},
      "shell": {"enabled": true, "config": {}}
    }
  }
}
```

The notification is complete state, not a delta. A client replaces its snapshot for the matching
`sessionId`; it ignores notifications for sessions it does not control. The agent must not send this
notification unless the client advertised the method. Without the extension, use standard ACP
configuration updates instead (section 9).

## 5. Selected and active configuration

Each session has two distinct configuration states:

- **Selected** is the accepted, persisted state. `selectedRevision` increases monotonically when a
  state-changing transaction commits. `ConfigurationSnapshot.bindings`, descriptor `enabled`, and
  descriptor `config` describe this state. ACP `configOptions.currentValue` represents the same
  accepted selected state.
- **Active** is the immutable revision captured before a turn starts. `activeRevision` identifies
  it; descriptor `activeEnabled` reports the binding's state in it. Before the first turn,
  `activeRevision` may be absent or null.

`applicationStatus` values:

| Value | Meaning |
| --- | --- |
| `applied` | The selected revision is active (normally `activeRevision == selectedRevision`). |
| `pending` | The selected state has been accepted but the next turn has not captured it yet. This is normal when a change is made during a running turn. |
| `failed` | Preparing the selected revision failed. The selected state is retained for correction; a turn must not start with the older composition. |

When selected and active differ, clients should show both, for example "Shell off for the next
turn; still active in the current turn." A configuration change is not cancellation; only the stop
action interrupts current work. A running turn continues with its original active revision. A
session allows one controlling connection and one prompt turn at a time; a second attempt is
`SESSION_BUSY`. Different sessions run concurrently.

`policyRevision` changes when host policy changes (availability, directional permissions,
editability, or lifecycle). Clients should refresh descriptors/snapshots when it changes. A policy
change that invalidates a saved selection blocks new work and returns the required changes rather
than silently granting old permissions.

Lifecycle rules:

- `changeMode` is read from `policy.changeMode`; the top-level mirror is informational only.
- `next_turn` changes may be accepted during a turn but activate at the next turn boundary.
- `new_session` settings are inspectable but not editable in an existing session. Updating one is
  `NEW_SESSION_REQUIRED`; create a new session with `initialConfiguration`.
- `available: false` bindings cannot be enabled and are shown with `availabilityReason`.
  `clientCanEnable`/`clientCanDisable` govern actual transitions; requests that already match the
  selected state are no-ops. Locked capabilities are not presented as editable ACP config options.
- Disabling omits the capability's future contributions; it does not delete persisted state.
  `stateMode` (`run`, `session`, `external`) describes state ownership, not protocol visibility.
  A disable may be blocked with `CAPABILITY_BUSY` until the binding's owned background jobs are
  drained.
- `enabledByDefault` is a deployment default, not the current selected state.

## 6. Revisions, request IDs, canonical hashes, and retries

`ConfigurationUpdate` is idempotent only through its `requestId`. It is not a general exactly-once
mechanism for prompts or session creation.

- **Scope.** The host claims `(principal, sessionId, requestId)` atomically in the session store.
- **Expected revision.** `expectedRevision` must equal the current `selectedRevision` at commit.
  A mismatch is `REVISION_CONFLICT` with `reason: "staleRevision"`, the expected and current
  revisions, and the latest snapshot. The client refreshes and can retry after choosing a new
  `requestId` if the body changes.
- **Canonical payload hash.** The host computes
  `canonical_hash(ConfigurationUpdate.model_validate(params).to_wire())`. The hash is not sent on
  the wire. `canonical_hash` is SHA-256 over UTF-8 JSON with sorted keys, compact separators, ASCII
  escaping, and `NaN`/infinity rejected. Validating through the model first normalizes aliases and
  defaults, so semantically identical v1 payloads serialize identically.
- **Retry outcomes.**
  - Same `requestId`, same hash, already completed: return the original committed snapshot;
    do not commit again. That result may be older than the current session state, so call
    `configuration/get` if the latest state is needed.
  - Same `requestId`, same hash, still in progress: `REVISION_CONFLICT` with
    `reason: "requestInProgress"`; retry later with the same body.
  - Same `requestId`, different hash: `REVISION_CONFLICT` with `reason: "requestIdReuse"`; the
    client must use a new `requestId`.
- **Retention.** Retry records are kept for the session's retained lifetime and removed when the
  session is deleted. A `requestId` rejected before being claimed may be reusable, but clients
  should use a fresh ID whenever any field changes.

For the update example above, the canonical payload hash is:

```text
0b5df851f884f3ff482757365ffee50b6d920a5eb34756fd6375a0419c421dde
```

An implementation can reproduce it with (where `params` is the request's `params` object):

```python
from harness_protocol import ConfigurationUpdate, canonical_hash

update = ConfigurationUpdate.model_validate(params)
payload_hash = canonical_hash(update.to_wire())
```

Ordinary prompts and `session/new` are not automatically retried after an ambiguous network
failure. JSON-RPC request IDs are correlation IDs, not exactly-once execution guarantees. Reconnect,
initialize again, and inspect the session before submitting new work.

## 7. Errors

Use standard ACP/JSON-RPC errors where applicable. Project application errors use JSON-RPC
server-error code `-32000` with a stable `error.data.code` and a structured `error.data.details`
object:

```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "error": {
    "code": -32000,
    "message": "Selected revision 3 is stale",
    "data": {
      "code": "REVISION_CONFLICT",
      "details": {
        "reason": "staleRevision",
        "expectedRevision": 3,
        "currentRevision": 4,
        "requestId": "cfg-01J8Z4M2Q7"
      }
    }
  }
}
```

`message` is human-readable and not stable; branch on `data.code`. `details` should identify the
binding IDs, field paths, revision, or request ID relevant to the failure. Do not add a custom
root-level `_meta` to the JSON-RPC error object; ACP reserves no extension point there. Do not put
secrets in errors.

| `data.code` | Typical meaning |
| --- | --- |
| `POLICY_DENIED` | Host policy or directional permission forbids the transition; locked/mandatory binding. |
| `INVALID_CONFIGURATION` | Schema/field validation, invalid cursor or limit, empty/oversized transaction, unknown params. |
| `REVISION_CONFLICT` | Stale `expectedRevision`, in-progress duplicate, or `requestId` reuse with a different body. |
| `DEPENDENCY_CONFLICT` | Missing requirements, conflicts, or disabling a required dependency without its dependents. |
| `CAPABILITY_UNAVAILABLE` | Binding or plugin is not available in this deployment. |
| `CAPABILITY_BUSY` | Disable is blocked by owned background work that has not drained. |
| `NEW_SESSION_REQUIRED` | The change targets a `new_session` setting. |
| `SESSION_BUSY` | Another turn or controlling connection holds the session. |
| `WORKSPACE_UNAVAILABLE` | The session's workspace is missing or cannot be restored. |
| `DEPLOYMENT_UNAVAILABLE` | The pinned deployment revision is unavailable. |

Transport codec errors raised by `decode_frame` use `HarnessError` codes `INVALID_FRAME` and
`MESSAGE_TOO_LARGE`. Unknown JSON-RPC methods use the standard `-32601` error. A semantic extension
request that is well-formed JSON-RPC but fails validation is normally `-32000` with
`INVALID_CONFIGURATION`; malformed JSON-RPC itself may use standard parse/invalid-request/invalid-
params errors.

Catalog HTTP APIs, where used, use the separate shape
`{"error": {"code": "...", "message": "...", "details": {}}}` via `HarnessError.to_http()`. They are
not a second prompt execution API.

## 8. Transport

### 8.1 stdio

Standard ACP stdout framing is newline-delimited UTF-8 JSON: exactly one complete JSON-RPC object
per line, no batching. Stdout is reserved for protocol messages; diagnostics go to stderr. Local
profiles launch the host directly without shell interpolation. `harness bridge` preserves ACP
payloads while translating local newline framing to WebSocket messages and back.

### 8.2 WebSocket

Hosted agents use WSS with subprotocol `harness.acp.v1`, TLS, and a deployment-issued bearer token
supplied during the HTTP upgrade. Each WebSocket **text** message contains exactly one complete
UTF-8 JSON-RPC object. Binary frames and batching are not supported.

| Constraint | Contract |
| --- | --- |
| Message size | At most 16 MiB of UTF-8 encoded JSON per frame. |
| Oversize | Server closes with WebSocket status `1009`; client reports a size failure. |
| Ping/pong | Ping every 30 seconds; require a pong within 30 seconds. |
| Outbound buffering | Bounded, with disconnect of a persistently stalled consumer. The protocol does not fix a numeric buffer size. |
| Frame validation | `decode_frame` rejects duplicate keys, `NaN`/infinity, non-2.0 envelopes, ambiguous request/response objects, invalid IDs, and oversized messages. |

`encode_frame` emits compact JSON and validates its own output. JSON-RPC IDs may be strings or
integers (booleans are invalid). Preserve JSON-RPC directionality, notifications, initialization,
and session lifecycle unchanged. Chunk assistant output before serialization and reject attachments
that would exceed the frame limit before sending. This is a project-defined transport, not the draft
ACP WebSocket transport; a future standard transport may be added alongside it. A disconnect
requests cancellation of the active interactive turn under the session rules; it does not make
prompts or session creation exactly-once.

## 9. Standard ACP fallback

If either peer lacks the extension, or the version is unsupported, the extension is unavailable
for the connection. The client then uses only standard ACP:

- Standard ACP `initialize`, sessions, prompts, streaming updates, tool calls, permissions, plans,
  cancellation, and advertised session lifecycle features remain fully usable.
- Simple capability toggles are exposed through standard `configOptions`. Emit a boolean option
  only when the client advertised `session.configOptions.boolean`; otherwise emit an on/off
  `select`. Locked or unavailable bindings are not presented as editable options.
- Use `session/set_config_option` for a single option change. The agent returns the complete
  `configOptions` list after the change, as ACP requires. Standard changes are serialized by the
  same session lock and validated against current state.
- The client does not invent plugin inventories, dependencies, batch edits, or unsupported
  controls from tool names. An ordinary ACP agent that has no project extension is rendered using
  its advertised standard features.
- A framework-native agent used by an ordinary ACP client receives defaults and simple standard
  options. Rich descriptors, atomic multi-binding edits, `add`, and revision-aware retry semantics
  require the negotiated extension.
- Standard ACP session configuration changes and extension updates must agree on the selected
  state. `configOptions.currentValue` represents the accepted selected state; the extension
  snapshot adds active revision, policy revision, application status, and rich descriptors.

When the extension is negotiated, the host should continue to expose a usable standard surface for
other clients. The client should not call an extension method that was not mutually advertised.

## 10. Versioning and compatibility

These versions evolve independently:

- ACP `protocolVersion` (`1` here).
- The project extension major version (`1`, in `ExtensionAdvertisement.version`).
- Each DTO `schemaVersion` (`1`).
- The WebSocket subprotocol (`harness.acp.v1`).
- Presentation payload `schemaVersion` (a positive integer carried by `PresentationPayload`).

A breaking wire change requires a new negotiated major version. v1 peers must not infer support
from a method name or silently downgrade. A client that sees an unsupported extension version treats
the extension as absent and continues with standard ACP. Unknown `_harness/*` methods are never
invoked; if called they receive `-32601`. Unknown presentation renderer IDs or schema versions fall
back to the readable `fallback` content.

The v1 models use `extra="forbid"`. A v1 receiver therefore must not be assumed to ignore new
fields. Additive fields require an explicitly negotiated version or a receiver that tolerates them;
they are not silently part of v1. Unknown optional presentation data may be ignored with the
standard fallback, but that does not relax DTO validation for extension requests and responses.

## 11. Contract versus runtime status

Verified in `harness-protocol` and `tests/test_protocol.py`:

- Pydantic DTO fields, camelCase aliases, strict unknown-field rejection, defaults, and the
  `type`-discriminated change union.
- Nullable field handling inside `configure.fields`, list envelopes, and revision fields.
- `HarnessError` JSON-RPC/HTTP shapes, `decode_frame`, `encode_frame`, `canonical_hash`, and
  `paginate` limits/cursors.
- The constants listed in section 1.

Contract obligations for host/client/runtime implementers, not verified by this document:

- Placement and acceptance of initialization metadata; advertisement filtering.
- Applying `initialConfiguration`, persisting selected/active revisions and retry records, and
  emitting complete `_harness/configuration/changed` notifications.
- Session locking, atomic dependency-aware transactions, policy enforcement, and job draining.
- WebSocket ping/pong, bounded buffering, close behavior, bearer authentication, and stdio bridge
  translation.
- Standard ACP fallback behavior and agreement between `configOptions` and extension snapshots.
- Interoperability with any third-party ACP client or agent.

This document specifies the project's v1 contract. It is not a conformance certificate, not a
claim that the host/client/runtime are complete, and not evidence that external implementations
accept these shapes.

## 12. Implementer checklist

1. Initialize with standard ACP; read the peer's `_meta.harness` advertisement and negotiate only
   mutually supported extension methods.
2. If the extension is absent or unsupported, use standard ACP config options and do not expose
   project-only controls.
3. Read catalogs from `items`, follow `nextCursor` only while present, and treat cursors as opaque.
4. Send changes using `type` values `enable`, `configure`, or `add`; never send `action`.
5. Snapshot before updating, pass the observed `selectedRevision` as `expectedRevision`, and use a
   stable `requestId` for exact retries.
6. Handle `-32000` by `data.code`; refresh on `REVISION_CONFLICT`, and use a new `requestId` if the
   body changes.
7. Display selected and active state separately when revisions differ; `new_session` changes need a
   new session.
8. Allow `add` and `plugins/list` only when the agent advertised `localBindingCreation: true` and
   `_harness/plugins/list`; never upload code or expect remote plugin installation.
9. Enforce the 16 MiB message limit, text-only one-object frames, and the 30-second ping/pong
   timeout on WebSocket connections.
