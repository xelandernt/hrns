# Security model

Harness separates *product policy* from *code execution isolation*. It enforces session ownership, capability permissions, workspace roots, transport limits and credential boundaries. It is **not** a sandbox for malicious installed Python plugins or model providers.

## Trust boundaries

| Boundary | Trusted side | Untrusted input |
| --- | --- | --- |
| Client → local host | Launched agent process | ACP messages from the client |
| Client → server | Configured deployment catalog | Bearer tokens, HTTP parameters, WebSocket frames |
| Server → worker | Server-owned environment | Nothing: the worker receives identity from its parent |
| Agent → workspace | Host-selected workspace backend | Model-generated paths and commands |
| Plugin → host | Installed Python code | Operator configuration and model inputs |

Installed plugins and provider SDKs execute as host Python code. Process separation keeps them out of the client and server packages; it does not confine a malicious plugin. Operators should install reviewed packages and treat deployment environments as privileged.

## Authentication and ownership

- Hosted clients present deployment-issued bearer tokens over TLS. Configuration stores only SHA-256 token hashes, never plaintext tokens.
- A token maps to one principal and an explicit allow-list of immutable deployment IDs. Catalog output is filtered by that list.
- The server passes `HARNESS_PRINCIPAL`, `HARNESS_DEPLOYMENT_ID`, `HARNESS_ALLOWED_CWDS` and `HARNESS_STORE` to the worker it spawns. Client-supplied ACP metadata cannot replace them.
- Session access is checked against `(principal, deploymentId)` for list, load, prompt, configure and delete. Session IDs are opaque identifiers, not bearer tokens.
- One worker controls a stored session at a time. A crashed worker's claim is reclaimed only when the previous owner is known dead; a live owner is never stolen.

## Workspace and command boundaries

- A workspace is an explicit host resource. Hosted workers accept a cwd only if it is inside `HARNESS_ALLOWED_CWDS`, canonicalized to defeat `..` and symlink escapes.
- The server never interprets a client path as permission to read an arbitrary host directory. Reopening a session preserves its recorded workspace identity.
- A requested sandbox that cannot be constructed fails with `WORKSPACE_UNAVAILABLE`; it never falls back to host execution.
- Local workspace and SSH workspace are labeled non-isolated. Isolated command execution requires an explicitly installed upstream backend (Smol, E2B, Modal, Sprites, Bubblewrap).
- Workspace isolation applies to workspace-aware tools. A plugin that opens files or subprocesses directly remains host code.

## Capability policy

- Host policy is enforced in the runtime before composition, independently of the UI. Disabled capabilities are omitted entirely: their tools, instructions, hooks and model settings do not reach the next turn.
- Directional permissions apply to transitions. Mandatory bindings cannot be disabled by a client even if it bypasses the terminal and sends protocol messages directly.
- A remote client can only select bindings offered by its deployment. It cannot upload, import, or install plugin code, and hosted workers do not advertise local binding creation or plugin inventories.
- Disabling a binding does not delete its persisted state. Destructive state reset is a separate explicit action.

## Transport and denial of service

- WebSocket messages are one UTF-8 JSON-RPC object per text frame, with a 16 MiB limit, no batching and no binary frames.
- Outbound buffering is bounded; a persistently stalled consumer is disconnected instead of growing memory without limit.
- Ping/pong runs at 30-second intervals with a 30-second timeout. Oversized frames close with status 1009.
- JSON parsing rejects duplicate keys, non-finite numbers, deep nesting, invalid Unicode, ambiguous request/response envelopes and malformed cursors. These become protocol errors rather than uncaught exceptions.
- Worker count and per-principal connections are bounded. Disconnect tears down the worker process group with a SIGTERM/SIGKILL grace sequence.
- Attachments are bounded and explicitly selected. They are sent as content; the agent does not receive a client filesystem grant. A path in attachment metadata is display information only.
- Headless runs never approve by default. A permission request without a configured policy exits with `interaction_required` and retains the session.

## Secrets

- Portable definitions, capability descriptors, transcripts and catalog responses carry no credentials.
- Provider credentials remain in the runtime host environment or an explicitly configured secret provider. Hosted workers inherit only the configured environment allow-list.
- Client profiles store secret *references* (`env:`, `keyring:`), not inline values by default. Resolved values are kept in memory and are not logged.
- The PostgreSQL DSN is server configuration, passed only in the worker environment; it is never exposed in catalog data or ACP messages.

## Known limitations

- No malicious-plugin confinement.
- No exactly-once external effects or automatic replay of uncertain prompts after a crash.
- TLS is expected at the server or a trusted terminator; loopback development may use plain HTTP/WS.
- Sandbox providers have their own lifecycle, isolation and billing properties; consult the coverage matrix and provider documentation.
- The project does not provide tenant billing, a plugin marketplace, live agent migration or collaborative session control.
