# Security model

Harness separates *product policy* from *code execution isolation*. It enforces session ownership, capability permissions, workspace roots, transport limits and credential boundaries. It is **not** a sandbox for malicious installed Python plugins or model providers.

## Trust boundaries

| Boundary | Trusted side | Untrusted input |
| --- | --- | --- |
| Client → local host | Launched agent process | ACP messages from the client |
| Client → server | Registered definitions and application authentication | Credentials, HTTP parameters, WebSocket frames |
| Agent → workspace | Host-selected workspace backend | Model-generated paths and commands |
| Plugin → host | Installed Python code | Operator configuration and model inputs |

Installed plugins and provider SDKs execute as host Python code. Network agents share the FastAPI process; stdio agents execute in a launched process. Neither arrangement confines malicious plugins. Install reviewed packages and treat agent environments as privileged.

## Authentication and ownership

- An optional FastAPI dependency authenticates HTTP catalog requests and WebSocket handshakes and returns a stable `Principal`. The application owns credential validation; dependencies should support `HTTPConnection` for both transports.
- Without that dependency all callers share `Principal("local")` and its session access. `Principal.agent_ids` can restrict catalog visibility and connections; `None` permits every registered agent.
- Agent registration fixes identity, resource bindings and workspace roots in Python. ACP metadata cannot replace them.
- Session access is checked against principal plus the registered agent ID/definition version for list, load, prompt, configure and delete. Session IDs are opaque identifiers, not bearer tokens.
- One connection controls a stored session at a time. A crashed owner's claim is reclaimed only when the previous owner is known dead; a live owner is never stolen.

## Workspace and command boundaries

- A workspace is an explicit host resource. Network ACP hosts accept a cwd only if it is inside the registered `allowed_cwds`, canonicalized to defeat `..` and symlink escapes.
- The server never interprets a client path as permission to read an arbitrary host directory. Reopening a session preserves its recorded workspace identity.
- A requested sandbox that cannot be constructed fails with `WORKSPACE_UNAVAILABLE`; it never falls back to host execution.
- Local workspace and SSH workspace are labeled non-isolated. Isolated command execution requires an explicitly installed upstream backend (Smol, E2B, Modal, Sprites, Bubblewrap).
- Workspace isolation applies to workspace-aware tools. A plugin that opens files or subprocesses directly remains host code.

## Capability policy

- Host policy is enforced in the runtime before composition, independently of the UI. Disabled capabilities are omitted entirely: their tools, instructions, hooks and model settings do not reach the next turn.
- Directional permissions apply to transitions. Mandatory bindings cannot be disabled by a client even if it bypasses the terminal and sends protocol messages directly.
- A remote client can only select bindings offered by its deployment. It cannot upload, import, or install plugin code, and network hosts do not advertise local binding creation or plugin inventories.
- Disabling a binding does not delete its persisted state. Destructive state reset is a separate explicit action.

## Transport and denial of service

- WebSocket messages are one UTF-8 JSON-RPC object per text frame, with a 16 MiB limit, no batching and no binary frames.
- Outbound buffering is bounded; a persistently stalled consumer is disconnected instead of growing memory without limit.
- Configure ASGI runner ping/pong intervals and timeouts (the example uses 30 seconds). Oversized frames close with status 1009.
- JSON parsing rejects duplicate keys, non-finite numbers, deep nesting, invalid Unicode, ambiguous request/response envelopes and malformed cursors. These become protocol errors rather than uncaught exceptions.
- Connections are bounded to 32 per server and four per principal. Disconnect cancels connection-local work and releases claims without closing another connection or the shared store. There is no process group to terminate; arbitrary blocking Python/plugin code shares the app process.
- Attachments are bounded and explicitly selected. They are sent as content; the agent does not receive a client filesystem grant. A path in attachment metadata is display information only.
- Headless runs never approve by default. A permission request without a configured policy exits with `interaction_required` and retains the session.

## Secrets

- Portable definitions, capability descriptors, transcripts and catalog responses carry no credentials.
- Provider credentials remain in the runtime host environment or an explicitly configured secret provider. Network agents use application-owned Python resource bindings and environment values.
- Client profiles store secret *references* (`env:`, `keyring:`), not inline values by default. Resolved values are kept in memory and are not logged.
- The PostgreSQL DSN belongs to the supplied store object; it is never exposed in catalog data or ACP messages.

## Known limitations

- No malicious-plugin confinement.
- No exactly-once external effects or automatic replay of uncertain prompts after a crash.
- TLS is expected at the server or a trusted terminator; loopback development may use plain HTTP/WS.
- Sandbox providers have their own lifecycle, isolation and billing properties; consult the coverage matrix and provider documentation.
- The project does not provide tenant billing, a plugin marketplace, live agent migration or collaborative session control.
