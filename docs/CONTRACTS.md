# Shared implementation contracts

`harness_protocol` is available now. It exports:

- `HarnessError(code, message, details=None)` with `to_rpc()` and `to_http()`.
- `EnableChange(type='enable', binding_id, enabled)`, `ConfigureChange(type='configure', binding_id, fields)`, `AddChange(type='add', binding_id, plugin_id, config={}, enabled=True)`.
- `changes_adapter.validate_python(list)` for the discriminated union.
- `BindingState(enabled, config={})` and `ConfigurationSnapshot(session_id, selected_revision=0, active_revision=None, policy_revision=1, application_status='pending', bindings={})`.
- `ConfigurationUpdate(session_id, request_id, expected_revision, changes)`.
- `BindingDescriptor`, `PluginInfo`, `CapabilityPolicy`, `RuntimeEvent(type,data)`.
- `SUBPROTOCOL='harness.acp.v1'`, `MAX_MESSAGE_BYTES=16MiB`, `PING_INTERVAL=PING_TIMEOUT=30`.
- `decode_frame`, `encode_frame`, `canonical_hash`, `paginate(items,cursor=None,limit=50)`.

Wire DTOs accept snake_case and camelCase. `.to_wire()` emits JSON-safe camelCase excluding None. Unknown fields are rejected. Secret objects belong in runtime resources, never these DTOs.

### Review clarification

Extension list responses use versioned `BindingListResponse` and `PluginListResponse`, both with **`items`** and optional `nextCursor`. The binding list also carries `selectedRevision`, `activeRevision`, and `policyRevision`. Clients and hosts should share these models rather than guess `bindings`/`plugins` response keys. `BindingDescriptor.policy.changeMode` is authoritative; the top-level `changeMode` is a validated convenience mirror and cannot override policy. All transport/version/method constants come from `harness_protocol`; hosts filter `_harness/plugins/list` out of their advertisement when local binding creation is prohibited.

## Worker environment

The server supplies `HARNESS_PRINCIPAL`, `HARNESS_DEPLOYMENT_ID`, `HARNESS_ALLOWED_CWDS` (JSON string array), `HARNESS_STORE`, and `HARNESS_HOSTED=1`. These values are trusted only as process configuration and must never be replaced from ACP metadata. Agent profiles launch workers directly, without shell interpolation.

### Launch contract (harness-server ↔ harness-host)

For **each authenticated WebSocket connection** `harness-server` executes the
deployment's immutable `command` argv directly with `exec` semantics:

- No shell, no command interpolation, no environment expansion in argv.
- A fresh process group/session per worker (`start_new_session`), so the
  server can signal the whole group.
- Working directory: the deployment's `cwd` (absolute after config load), or
  the server's working directory when `cwd` is not configured.
- stdout and stdin carry the protocol; stderr is diagnostics only (below).
- The server never forwards client-supplied metadata into argv, cwd, or
  environment.

One worker serves exactly one connection and may own several ACP sessions
for that connection. There is no multiplexing across principals or
deployments.

### Environment (server-owned)

The worker environment is built from scratch. Host environment variables are
**not** inherited unless explicitly listed (default: `PATH`, `HOME`,
`TMPDIR`; configurable through `server.worker_env_passthrough`). The
contract variables below are always set by the server and cannot be
overridden by clients, deployment resource values, or passthrough:

| Variable | Value | Meaning |
| --- | --- | --- |
| `HARNESS_PRINCIPAL` | configured principal id | The authenticated caller this connection belongs to. |
| `HARNESS_DEPLOYMENT_ID` | immutable deployment id | The pinned deployment revision whose environment this is. |
| `HARNESS_ALLOWED_CWDS` | JSON array of absolute paths | The only workspace roots the worker may use for this connection. |
| `HARNESS_STORE` | absolute SQLite file path | Server-provided database for this deployment revision (`<store_path>/<deploymentId>.sqlite3`). Workers of the same revision share it under WAL. |
| `HARNESS_HOSTED` | `"1"` | Marks the worker as hosted rather than local/stdio. |

Deployment resource bindings add further variables:

- literal values from `[deployment.env]` (operator configured);
- values copied from the server process environment named by
  `env_from_environment` (for secrets, e.g. `MODEL_API_KEY`). A missing
  referenced variable makes the spawn fail before the worker starts.

`HARNESS_*` names are reserved: deployment configuration that tries to set
one is rejected at load, and passthrough entries in that namespace are
ignored. The server's own values always win.

Example worker environment (values illustrative):

```json
{
  "HARNESS_PRINCIPAL": "alice",
  "HARNESS_DEPLOYMENT_ID": "coder.r1",
  "HARNESS_ALLOWED_CWDS": ["/srv/workspaces/coder", "/srv/workspaces/example"],
  "HARNESS_STORE": "/srv/harness/store/coder.r1.sqlite3",
  "HARNESS_HOSTED": "1",
  "MODEL_API_KEY": "<from server environment>",
  "SANDBOX_BACKEND": "e2b"
}
```

#### What the host must do with it

1. Accept only session workspaces (`cwd` in ACP `session/new`, load, resume)
   that resolve inside one of `HARNESS_ALLOWED_CWDS`. Reject others with
   `WORKSPACE_UNAVAILABLE`; never silently substitute another directory.
2. Record sessions under `(HARNESS_PRINCIPAL, HARNESS_DEPLOYMENT_ID)` and
   check ownership on every list/load/configure/prompt/delete operation
   before touching state.
3. Treat `HARNESS_DEPLOYMENT_ID` as the pinned revision. Never launch a
   different environment or rewrite sessions onto another deployment.
4. Use `HARNESS_STORE` as the only durable database for hosted sessions.
5. Never trust ACP `_meta`, client-provided paths, or "environment" fields
   from the wire as identity or policy. They are presentation data.

The server enforces capacity, authentication, acceptable workspace roots
(for advertisement) and framing; the host enforces session ownership and cwd
resolution. Both checks are required: neither side is allowed to assume the
other did it.

### Stdio protocol framing

- **stdin**: newline-delimited JSON-RPC from the server. Each line contains
  exactly one JSON object (no batches, no binary).
- **stdout**: newline-delimited JSON-RPC to the server. Each line must be one
  complete UTF-8 JSON object. stdout is reserved for protocol traffic;
  anything else is a protocol violation and the server terminates the worker
  after reporting a transport error to the client.
- **stderr**: any bytes. Never protocol.
- Maximum line/frame size: 16 MiB (UTF-8 bytes, excluding the trailing
  newline). Tokens or messages larger than that are transport errors.
- Blank lines are tolerated and dropped; they must not be used as keepalives.
- The server forwards frames verbatim after validating JSON-object framing,
  so JSON-RPC ids, notifications, and error payloads pass through unchanged
  in both directions.

### Connection lifecycle

1. Server authenticates the bearer token, resolves the principal,
   authorizes the deployment id, checks capacity, then spawns the worker.
2. The worker should begin serving immediately; there is no separate startup
   handshake. ACP `initialize` arrives as the first client frame.
3. When the client disconnects (or the server shuts down), the server:
   - closes the worker's stdin (EOF);
   - waits `worker_term_grace_seconds` (default 10 s) for a clean exit;
   - sends `SIGTERM` to the worker's whole process group;
   - waits `worker_kill_grace_seconds` (default 5 s);
   - sends `SIGKILL` to the process group.
4. On EOF the worker should cancel any active interactive turn, flush what it
   can, persist session state, and exit. It must not block forever.
5. Any child processes the worker starts belong to the connection's process
   group, and the server kills leftover group members after disconnect. Do
   not detach long-lived children that must outlive the connection.

The host owns the cancellation policy: the server does not send ACP
`session/cancel` on the worker's behalf because it does not parse messages.
EOF plus the bounded termination sequence is the server's only shutdown
signal.

### Hosted mode differences

With `HARNESS_HOSTED=1` the host must:

- disable local-only features: no client plugin installation, no client
  command execution, no local MCP command launch from client input;
- advertise only the deployment's offered bindings/capabilities;
- resolve credentials exclusively from the server-provided environment;
- mark returned paths as remote paths (they are not client paths);
- refuse attempts to read host files outside the workspace roots.

Local stdio deployments start `harness-host` themselves and do not set these
variables; no server is involved.

### Diagnostics

- stderr is drained continuously so the worker can never deadlock on a full
  pipe.
- The server retains only a bounded tail (`server.stderr_tail_bytes`,
  64 KiB default) for operator diagnostics. Content is **never** forwarded to
  clients, embedded in ACP frames, or logged verbatim by default.
- Workers must not print secrets to stdout. Even though stderr is not
  exposed, avoid dumping credentials anywhere.

### Compliance checklist for runtime/host integration

- [ ] Startup from argv only; no shell, no embedded commands.
- [ ] One JSON-RPC object per stdout line; no logging to stdout.
- [ ] Read stdin until EOF; exit bounded and promptly on disconnect.
- [ ] Validate session `cwd` against `HARNESS_ALLOWED_CWDS`.
- [ ] Enforce `(principal, deploymentId)` session ownership.
- [ ] Use `HARNESS_STORE` for hosted persistence.
- [ ] Ignore client metadata for identity, policy, and workspace roots.
- [ ] Exit as a group; do not detach surviving children.

## Pins

Pydantic AI slim **2.57.0**, official Harness **0.57.0**, official ACP SDK **0.11.1** (Harness's supported `<0.12` range), Textual **8.2.8**. The final `uv.lock` records all transitive versions.

## Runtime-to-ACP contract

The runtime owner publishes `RUNTIME_API.md`; the ACP owner publishes any additional requirements in `ACP_RUNTIME_CONTRACT.md`. Host defaults to the same programmatic policy path, not a second implementation.
