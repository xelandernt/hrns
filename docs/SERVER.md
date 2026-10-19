# harness-server

`harness-server` hosts deployed agents: it publishes an authenticated catalog
of immutable deployment revisions and proxies their ACP JSON-RPC traffic over
a bounded WebSocket transport. Agent execution happens in a dedicated
environment per deployment, behind a `harness-host` stdio worker launched for
each authenticated connection.

The server deliberately depends only on Starlette, Uvicorn, and Pydantic. It
does not import `harness-runtime` or Textual, and it does not parse ACP
methods — ACP semantics, sessions, and session ownership stay in the worker.
The worker-side half of this boundary is specified in
[CONTRACTS.md](CONTRACTS.md).

- [Running the server](#running-the-server)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [WebSocket ACP transport](#websocket-acp-transport)
- [TLS](#tls)
- [Worker lifecycle](#worker-lifecycle)
- [Limits and failure behavior](#limits-and-failure-behavior)
- [Security model](#security-model)
- [Tests](#tests)
- [Operational limitations](#operational-limitations)

## Running the server

`create_app(config)` is the public embedding entry point; the CLI wires it to
Uvicorn with the transport contract baked in.

```bash
# Validate a catalog without binding a port
harness-server --config examples/deployments.toml --check

# Serve the catalog and ACP transport
harness-server --config examples/deployments.toml --host 127.0.0.1 --port 8080

# Equivalent when the console script is not on PATH yet
python -m harness_server --config examples/deployments.toml
```

CLI flags:

| Flag | Meaning |
| --- | --- |
| `--config PATH` | Deployment TOML (required). |
| `--host HOST` | Bind address; overrides `server.host`. |
| `--port PORT` | Bind port; overrides `server.port`. |
| `--root-path PATH` | ASGI root path when a proxy serves the app under a subpath. |
| `--log-level LEVEL` | `debug`, `info`, `warning`, `error`, `critical`. |
| `--check` | Validate the configuration and exit 0 without serving. |
| `--version` | Print the distribution version. |

Invalid configuration exits with status 2 and a readable message. The runner
always uses:

- `ws_max_size = 16 MiB` (the maximum ACP frame),
- `ws_ping_interval = 30 s` and `ws_ping_timeout = 30 s`,
- `proxy_headers = true` with `forwarded_allow_ips` from the configuration,
- optional in-process TLS from `server.ssl_certfile` / `server.ssl_keyfile`.

### Embedding

`create_app` accepts a validated `HarnessServerConfig`, a TOML path, or an
equivalent mapping. `load_config` performs validation, path resolution and
lockfile verification:

```python
import uvicorn
from harness_server import create_app, load_config
from harness_server.cli import install_log_filters

config = load_config("deployments.toml")
install_log_filters()  # optional: silences the Uvicorn denial-response quirk
uvicorn.run(
    create_app(config),
    host=config.server.host,
    port=config.server.port,
    ws_max_size=16 * 1024 * 1024,
    ws_ping_interval=30.0,
    ws_ping_timeout=30.0,
)
```

Tested against Starlette 1.8, Uvicorn 0.54, Pydantic 2.14, and Python 3.12
(the package requires Python 3.11+).

The server is a single process by design: capacity counters and live-worker
bookkeeping are in memory. Do not run multiple Uvicorn workers against
different catalogs behind one hostname.

The runner installs one narrow logging filter for a known Uvicorn quirk:
Uvicorn logs an `ERROR` when an ASGI WebSocket **denial response** (used here
for 400/401/403/404/503 upgrade rejections) completes, even though the HTTP
response was delivered correctly. `install_log_filters()` is exported for
embedding code that uses `uvicorn.run` directly.

## Configuration

See [examples/deployments.toml](../examples/deployments.toml) for a complete,
valid example. The file is TOML:

```toml
[server]
host = "127.0.0.1"
port = 8080
require_tls = true
forwarded_allow_ips = ["127.0.0.1"]
store_path = "var/harness-store"

[[deployment]]
id = "coder.r1"
name = "Coder"
version = "1.2.0"
description = "Coder agent: filesystem, shell, planning."
command = ["/opt/harness/envs/coder-1.2.0/bin/harness-host"]
cwd = "/opt/harness/deployments/coder-1.2.0"
allowed_cwds = ["/srv/workspaces/coder", "/srv/workspaces/demo"]
default_cwd = "/srv/workspaces/coder"
env_from_environment = ["MODEL_API_KEY"]

[deployment.env]
SANDBOX_BACKEND = "e2b"

[deployment.lockfile]
path = "locks/coder-1.2.0.lock"
sha256 = "..."
```

The `[deployment.lockfile]` table is a nested spelling of the flat
`lockfile_path` / `lockfile_sha256` fields used by the shipped example; both
forms validate identically.

### `[server]`

| Field | Default | Meaning |
| --- | --- | --- |
| `host` | `127.0.0.1` | Bind address used by the CLI. |
| `port` | `8080` | Bind port used by the CLI. |
| `require_tls` | `true` | Reject non-TLS peers except loopback (see [TLS](#tls)). |
| `forwarded_allow_ips` | `["127.0.0.1"]` | Peers trusted to set `X-Forwarded-Proto`; `["*"]` trusts all (only behind a trustworthy proxy). |
| `store_path` | `var/harness-store` | Directory created at startup. Workers receive `<dir>/<deploymentId>.sqlite3` as `HARNESS_STORE` (one shared WAL database per immutable revision). Relative paths resolve against the config file. |
| `store_url` | unset | Optional PostgreSQL URI DSN (`postgresql://...` or `postgres://...`) supplied to every worker as `HARNESS_STORE`. It takes precedence over `store_path`; the deployment environment must install `harness-runtime[postgres]`. Session rows remain scoped by principal and deployment id. The server itself never imports the database driver. Libpq keyword DSNs are rejected here because the generic `HARNESS_STORE` selector only recognises URI prefixes; pass those directly to `PostgresSessionStore` instead. |
| `log_level` | `info` | Default Uvicorn level. |
| `root_path` | `""` | Default ASGI root path. |
| `max_worker_count` | `32` | Concurrent worker processes for the whole server. |
| `max_connections_per_principal` | `4` | Concurrent worker connections per principal. |
| `worker_env_passthrough` | `["PATH","HOME","TMPDIR"]` | Server environment variables copied into workers. Names in the `HARNESS_` namespace are ignored. |
| `worker_term_grace_seconds` | `10.0` | Grace after stdin EOF before `SIGTERM`. |
| `worker_kill_grace_seconds` | `5.0` | Grace after `SIGTERM` before `SIGKILL`. |
| `outbound_queue_limit` | `1024` | Frames buffered toward a slow consumer. |
| `outbound_stall_timeout_seconds` | `30.0` | How long a full queue may stay stuck before disconnecting. |
| `stderr_tail_bytes` | `65536` | Bounded stderr tail retained for diagnostics (never forwarded). |
| `ssl_certfile` / `ssl_keyfile` | unset | Optional in-process TLS; both or neither. |

### `[[deployment]]`

One entry per **immutable revision**. `id` is the only stable handle clients
see; create a new id for every rebuilt environment and keep old revisions
listed while sessions may still reference them.

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Immutable revision id, `[a-z0-9][a-z0-9._-]{0,127}` (e.g. `coder.r1`). |
| `name` | yes | Human-readable name shown in the catalog. |
| `version` | yes | Definition version for display; the id, not this string, pins the environment. |
| `description` | `""` | Catalog description. |
| `recommended` | `false` | Marks the revision a client should choose for new sessions; older revisions stay addressable for pinned sessions. |
| `command` | yes | Worker argv, executed directly without a shell. Absolute paths must exist and be executable; bare names resolve through `PATH`. |
| `cwd` | unset | Worker working directory (absolute after load). Relative paths resolve against the config file and are not required to exist until spawn. |
| `allowed_cwds` | `[]` | Absolute workspace roots advertised to the client and exported as `HARNESS_ALLOWED_CWDS`. |
| `default_cwd` | unset | Must be one of `allowed_cwds`; advertised as `defaultWorkspace`. |
| `env` | `{}` | Literal resource bindings for the worker. `HARNESS_*` keys are reserved and rejected. |
| `env_from_environment` | `[]` | Server environment variables copied into the worker at spawn. Missing names fail the connection before the worker starts. |
| `lockfile_path` | unset | Resolved lockfile for the revision. Must be readable at load. Relative to the config file. |
| `lockfile_sha256` | unset | Expected SHA-256 of the lockfile; a mismatch aborts configuration loading. |

For a shared PostgreSQL store:

```toml
[server]
store_url = "postgresql://harness:secret@db.example/harness?sslmode=require"
```

The DSN is passed only through the server-controlled worker environment and is never
included in catalog responses or ACP metadata. Keep it out of logs; use a secret manager
or a protected configuration file. All revisions share the database, but session access is
still keyed by `(principal, deploymentId)`.

The loader verifies cross-references: unique deployment and principal ids,
no duplicate token hash across principals, and every
`allowed_agent_ids` entry must name a configured deployment.
`[deployment.<id>]` table syntax is also accepted as a shorthand for
`[[deployment]]`.

### `[[principal]]`

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable principal id, same character rules as deployment ids. |
| `token_hashes` | yes | Lowercase SHA-256 hex digests of bearer tokens. Never store plaintext tokens. |
| `allowed_agent_ids` | yes | Immutable deployment revisions this principal may see and connect to. |

Generate a hash:

```bash
printf '%s' 'your-token' | shasum -a 256
```

Tokens must be sent as `Authorization: Bearer <token>` on both HTTP requests
and the WebSocket upgrade. Unknown, malformed, or non-bearer credentials get
a `401` envelope with `WWW-Authenticate: Bearer`.

## HTTP API

All responses are JSON. Errors always use one envelope:

```json
{"error": {"code": "INVALID_REQUEST", "message": "limit must be between 1 and 100",
           "details": {"param": "limit", "min": 1, "max": 100}}}
```

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | no | Liveness probe (`{"status":"ok","service":"harness-server",...}`). |
| `GET` | `/v1/agents` | bearer | Paginated catalog of revisions available to the caller. |
| `GET` | `/v1/agents/{agentId}` | bearer | Descriptor for one immutable revision. |
| `GET` | `/v1/agents/{agentId}/acp` + WebSocket upgrade | bearer | ACP transport (see below). |

### `GET /v1/agents`

Query parameters:

- `limit` — integer 1..100, default **50**.
- `cursor` — opaque cursor returned by the previous page. Cursors are
  signed with a per-process secret; they are not offsets clients can spell.
  A cursor from a previous server process, or a tampered cursor, yields
  `400 INVALID_REQUEST` with `details.param = "cursor"`. Reusing the cursor
  that produced the final page returns that same final page again, not an error.

Response:

```json
{
  "items": [ { "agentId": "coder.r1", "...": "..." } ],
  "nextCursor": "eyJvIjoxLCJ2IjoxfQ.9F0…"
}
```

`nextCursor` is `null` on the last page. The list is filtered to the
principal's `allowed_agent_ids` and ordered as configured.

### Agent descriptor

```json
{
  "agentId": "coder.r1",
  "name": "Coder",
  "version": "1.2.0",
  "description": "Coder agent: filesystem, shell, planning.",
  "defaultWorkspace": "/srv/workspaces/coder",
  "allowedWorkspaces": ["/srv/workspaces/coder", "/srv/workspaces/demo"],
  "lockfile": {"name": "coder-1.2.0.lock", "sha256": "..."},
  "connection": {
    "url": "/v1/agents/coder.r1/acp",
    "subprotocol": "harness.acp.v1",
    "maxMessageBytes": 16777216
  }
}
```

`lockfile` is `null` when the deployment has no lockfile metadata; otherwise
it carries the lockfile name and content digest (never the server's absolute
path). Agents that exist but are not allowed for the caller return exactly
the same `404 NOT_FOUND` envelope as unknown ids, so the catalog cannot be
probed.

### Error codes

| HTTP | Code | When |
| --- | --- | --- |
| 400 | `INVALID_REQUEST` | Malformed query parameter, invalid/tampered cursor, missing WebSocket subprotocol. |
| 401 | `UNAUTHORIZED` | Missing, unknown, or non-bearer credentials. |
| 403 | `FORBIDDEN` | Reserved for authorization failures; not produced by the catalog today. |
| 403 | `TLS_REQUIRED` | Plain HTTP/WS from a non-loopback peer while TLS is required. |
| 404 | `NOT_FOUND` | Unknown route, unknown agent, or agent not allowed for the principal. |
| 405 | `METHOD_NOT_ALLOWED` | Wrong HTTP method. |
| 500 | `INTERNAL_ERROR` | Unexpected server failure (details are not leaked). |
| 503 | `SERVER_BUSY` | Worker or per-principal connection limit reached (`details.reason` is `worker_limit` or `principal_limit`; includes `Retry-After`). |
| 503 | `WORKER_SPAWN_FAILED` | The deployment worker could not be started (missing executable, inaccessible cwd, missing referenced environment variable). |

## WebSocket ACP transport

```
GET /v1/agents/{agentId}/acp
Sec-WebSocket-Protocol: harness.acp.v1
Authorization: Bearer <token>
```

The upgrade is rejected **before acceptance** with the HTTP error envelope
when TLS policy, authentication, agent authorization, subprotocol
negotiation, capacity, or worker spawn fails. Uvicorn and the test client
expose this as a WebSocket denial response (HTTP status + JSON body). If an
ASGI server lacks the denial-response extension, the server completes the
upgrade and immediately closes with the mapped application code (see below).

Once accepted:

- One **text** message carries exactly one complete JSON-RPC object. Batches
  (JSON arrays), non-object JSON, binary frames, and malformed JSON are
  rejected.
- The maximum frame size is **16 MiB** (UTF-8 bytes). Oversized frames close
  the connection with status `1009`; Uvicorn also enforces the same limit at
  the protocol layer.
- Frames preserve JSON-RPC directionality, ids, and payload values in both
  directions. Inbound text is validated and re-serialized as one compact line
  for the newline-framed stdio worker, so insignificant JSON whitespace cannot
  split a message.
- Assistant output must already be chunked by the agent; the server does not
  split messages.
- Ping frames are sent every 30 s with a 30 s pong timeout, so dead clients
  are detected without protocol traffic.
- Outbound frames are buffered up to `outbound_queue_limit` per connection.
  A consumer that does not drain for `outbound_stall_timeout_seconds` is
  disconnected rather than growing server memory without bound.

Close codes used after the upgrade:

| Code | Meaning |
| --- | --- |
| `1002` | Protocol violation: invalid JSON, non-object, JSON-RPC batch, or a worker frame that is not one object. |
| `1003` | Binary frame from the client. |
| `1007` | Invalid UTF-8 from the worker. |
| `1009` | Frame larger than 16 MiB, in either direction. |
| `1011` | Unexpected server-side failure while relaying. |
| `4511` | The worker process exited or closed its input stream. |
| `4512` | Bounded buffering overflow: the client (outbound) or worker (inbound) stopped consuming frames. |
| `4401` / `4403` / `4404` / `4455` / `4503` | Fallback close codes for handshake denials when the ASGI server cannot send an HTTP denial: unauthorized, TLS required, unknown/forbidden agent, missing subprotocol, server busy. |

### Client example

```python
import asyncio, json
import websockets

async def main(token: str) -> None:
    async with websockets.connect(
        "wss://harness.example.com/v1/agents/coder.r1/acp",
        subprotocols=["harness.acp.v1"],
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=16 * 1024 * 1024,
    ) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": 1},
        }))
        print(await ws.recv())

asyncio.run(main("..."))
```

Older `websockets` releases spell the option `extra_headers`; Node, Go, and
native clients simply set the same request headers.

### Ownership and workspaces

The server owns authentication, the catalog, and the
`HARNESS_ALLOWED_CWDS` policy input. It does **not** interpret ACP
`session/new` or `session/load` frames. The `harness-host` worker validates
the requested `cwd` against `HARNESS_ALLOWED_CWDS`, and sessions are owned by
`(principal, deploymentId)` in the host's store. `allowedWorkspaces` and
`defaultWorkspace` in the descriptor tell the client which values the host
will accept. A client's local path is never a permission to read a server
directory.

## TLS

Production deployments terminate TLS in front of the server (a reverse
proxy or load balancer) and forward to Uvicorn over loopback:

```
client --wss--> TLS terminator --ws/http--> harness-server (127.0.0.1:8080)
```

Terminator requirements:

1. Forward `Authorization`, `Sec-WebSocket-Protocol`, and the WebSocket
   upgrade headers/body unchanged.
2. Set `X-Forwarded-Proto: https` on both HTTP and WebSocket requests whose
   client connection used TLS. The server only trusts this header from peers
   listed in `server.forwarded_allow_ips`.
3. Allow WebSocket upgrades (do not buffer responses) and do not impose a
   frame limit below 16 MiB.

`require_tls = true` (the default) rejects plaintext requests from
non-loopback peers with `403 TLS_REQUIRED`. Loopback clients are exempt so
local development with `ws://127.0.0.1:8080` keeps working without a
terminator; unset `require_tls` only for trusted internal networks. As part
of the same policy, clients should always use `wss:` in production.

For single-binary deployments, `ssl_certfile` and `ssl_keyfile` enable TLS in
Uvicorn directly; the CLI forwards them. Either way, the server never
terminates trust on a header alone: the socket scheme or a trusted
`X-Forwarded-Proto` both count.

## Worker lifecycle

For each authenticated WebSocket connection:

1. Capacity is reserved synchronously (no race between concurrent connects).
2. The worker is spawned from the deployment `command` argv (`exec`, no
   shell), with `cwd`, a sanitized environment, and a new process group.
3. The upgrade completes and frames flow until either side ends.
4. Stdin is closed (EOF), then the server waits `worker_term_grace_seconds`,
   sends `SIGTERM` to the whole process group, waits
   `worker_kill_grace_seconds`, and finally `SIGKILL`s the group. Any
   leftover children from the connection are included.
5. The capacity slot is released even when the connection is cancelled
   mid-relay (ASGI servers may cancel the handler as soon as the socket
   closes).

Missing executables, missing `cwd`, and missing `env_from_environment`
variables fail before the worker starts and are reported as
`WORKER_SPAWN_FAILED` (HTTP 503). Protocol violations or worker crashes
during a live session close the client with the codes above. Worker stderr
is continuously drained into a bounded tail and is never forwarded to
clients or logged as content.

The complete environment contract is in [CONTRACTS.md](CONTRACTS.md).

## Limits and failure behavior

- `max_worker_count` is global; `max_connections_per_principal` is per
  principal. Both return `503 SERVER_BUSY` with a `Retry-After` header.
- A worker that is killed, crashes, or closes stdout ends its connection
  with `4511`; the slot is released and a reconnect spawns a fresh worker.
- A client that stops reading is disconnected after the outbound stall
  timeout; server memory stays bounded by the queue limit.
- Answers to ambiguous network failures are never retried by the server:
  JSON-RPC request ids are correlation ids, not exactly-once execution
  guarantees. Reconnect and inspect session state before resubmitting work
  (the host owns session replay).
- On process shutdown, every registered worker is reaped within the
  configured graces; as a last resort the registry `SIGKILL`s leftover
  groups.

## Security model

- Only SHA-256 token hashes are stored in configuration. Token lookup hashes
  the presented bearer token and compares by hash map; secrets are never
  written to logs.
- The catalog is filtered per principal. Unknown and forbidden agents are
  indistinguishable.
- Client-supplied ACP metadata never influences environment, identity,
  deployment selection, workspace roots, or argv. The frame is forwarded to
  the worker as data; the server-owned environment is authoritative.
- Workers run from a dedicated environment's argv without a shell. Each
  connection gets its own process group, which is always killed on
  disconnect.
- stderr never reaches the client and is not logged verbatim; the retained
  tail is bounded.
- The `HARNESS_` environment namespace is reserved and rejected in
  deployment configuration, so configuration cannot spoof principal or
  deployment identity.
- HTTP request bodies are ignored; the catalog is read-only.

## Tests

The server is tested with an in-process Starlette `TestClient` and a fake
`harness-host` (`tests/_fake_hosts.py`) — no runtime, ACP SDK, or provider
credentials are needed:

```bash
pytest tests/test_server_config.py tests/test_server_catalog.py \
       tests/test_server_acp_ws.py tests/test_server_worker.py \
       tests/test_server_cli.py -q
```

Coverage highlights:

| Area | Cases |
| --- | --- |
| Config | token hashes, reserved env, lockfile digest, workspace roots, absolute command checks, duplicate ids/references. |
| Catalog | bearer failures, per-principal filtering, descriptor fields, 1..100 limit bounds, opaque cursors, tamper rejection, unknown/forbidden parity, 404/405 envelopes, TLS policy and forwarded-proto trust. |
| Transport | subprotocol enforcement, 401/404/503 handshake denials, initialize round trip, notifications both ways, binary/batch/non-object/oversize rejection, stall handling, worker crash/exit codes. |
| Environment | contract variables and precedence, client metadata cannot change principal/deployment/allowed cwds/store. |
| Lifecycle | per-connection workers, PID death after disconnect, process-group child kill, stubborn worker SIGTERM bound, spawn failure cleanup, capacity release, registry shutdown. |
| CLI | Uvicorn contract parameters (16 MiB, 30/30 ping, proxy headers, cert passthrough), `--check`, exit codes. |

## Operational limitations

- Single process: capacity accounting, cursors, and worker registry are
  in-memory. Restarting the server invalidates outstanding cursors; clients
  restart pagination from the first page.
- The catalog is immutable for the lifetime of the process. Deploy an update
  by writing a new config with a new deployment revision id and restarting
  (or rolling to a new instance).
- POSIX process-group handling is required; macOS and Linux are the supported
  server hosts.
- The server does not implement session persistence, transcript storage, or
  ACP method dispatch — those belong to `harness-host` and `harness-runtime`
  (see [CONTRACTS.md](CONTRACTS.md)).
- There is no built-in per-request rate limiter beyond WebSocket capacity
  limits; put request-rate limits at the TLS terminator if needed.
- Metrics are not exported; use logs, `/healthz`, and the process exit
  behavior. Worker stderr tails are available to embedding code via the
  `WorkerSession` object, not over HTTP.
