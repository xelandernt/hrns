# Optional PostgreSQL session store

`harness.runtime.postgres.PostgresSessionStore` is a server-side durable store behind the
same asynchronous interface as the default SQLite WAL [`SessionStore`](../src/harness/runtime/store.py).
It is optional: importing the module works without a database driver, and no server
connection is opened until the store is used.

Status: implemented and **locally verified against PostgreSQL 17** using a disposable
`postgres:17-alpine` container on 2026-10-18. The full `tests/test_postgres.py` suite
passed 18/18, including the opt-in database tests for concurrent claims, revision
allocation, ownership, turn history, transcript coalescing, workspace identities,
cascade deletion, and `SessionManager` open/resume plus a real Pydantic AI turn. No project-provided PostgreSQL
service is required for the default test suite, so a plain `pytest` run reports the
database tests as skipped.

## Install

The driver is the `postgres` extra (`psycopg[binary]>=3.2`):

```sh
uv sync --extra postgres
# or, outside the workspace:
pip install 'harness[postgres]'
```

Without the extra, constructing a store raises an actionable `ImportError`; merely
importing `harness.runtime.postgres` does not fail.

## Usage

```python
from harness.runtime import AgentDefinition, PluginRegistry, SessionManager
from harness.runtime.postgres import PostgresSessionStore

store = PostgresSessionStore(
    "postgresql://harness:secret@db.example/harness?sslmode=require"
)
await store.initialize()          # optional; the first operation does this lazily

manager = SessionManager(PluginRegistry(), store=store)
session = await manager.open_session(
    definition,
    principal="alice",
    deployment_id="deploy-1",
)
await session.close()
await store.close()
```

When the host configures workspace providers, `SessionManager` uses the selected store's
workspace-record callbacks automatically. A manually constructed manager can be wired the
same way:

```python
from harness.runtime.workspaces import WorkspaceManager

workspaces = WorkspaceManager(
    providers=providers,
    record_store=store.workspace_record_callbacks(),
)
```

These callbacks persist `WorkspaceRecord` identities in `harness_workspace_records` and
are removed when the session is deleted. Corrupt records fail with
`WORKSPACE_RECORD_FAILED` rather than being silently ignored.

Constructor arguments:

```python
PostgresSessionStore(
    dsn=None,                      # or HARNESS_STORE when omitted
    *,
    connect_timeout=10.0,          # seconds; 1 is the libpq minimum
    application_name="harness-runtime",
    **connect_kwargs,              # additional libpq parameters, e.g. sslmode
)
```

A PostgreSQL URI (`postgresql://...`, `postgres://...`) or a libpq keyword DSN
(`host=... dbname=...`) is accepted. A filesystem path is rejected immediately with
`ValueError` instead of silently opening SQLite. The connection itself is lazy, so
construction never contacts the server. `await store.close()` closes the connection; a
later operation reconnects and rechecks the schema. The store does not import or fall back
to SQLite.

Optionally set `store.control_stale_after = 30.0` to permit age-based ownership recovery
for rows that have no PID information (the same knob as the SQLite store). Per-call
`stale_after=` overrides it.

`PostgresSessionStore` subclasses `SessionStore` on purpose. The runtime session manager
uses `isinstance(store, SessionStore)` to distinguish a store object from a filesystem
path. Passing a non-subclass would make the manager treat it as a path and create a SQLite
file.

## Host integration

The runtime now selects the backend from `HARNESS_STORE` / `--store` at the process
boundary with `harness.runtime.host.build_store`:

```python
from harness.runtime.host import build_store

store = build_store("postgresql://harness:secret@db.example/harness")
manager = SessionManager(registry, store=store)
```

`SessionManager(..., store="postgresql://…")` also coerces a PostgreSQL DSN directly. The
host's per-session ACP sidecar (cwd/model) is SQLite-file based; with PostgreSQL, the
authoritative cwd comes from the runtime session record and the model falls back to the
deployment default on a cold load. The server passes `server.store_url` to every worker;
the same DSN is never exposed in catalog responses. Integration notes:

- `SessionManager` treats an explicitly supplied `SessionStore` instance as
  caller-owned, so `manager.close()` does **not** close it. Keep a reference and call
  `await store.close()` at application shutdown.
- The host's `HostMetadataStore` still expects a local filesystem path (or `None` for
  in-memory metadata); do **not** pass a `postgresql://` DSN as `HostBundle.store_path`.
  The runtime store now records the authoritative `cwd` (`summary.cwd`), which the host
  prefers on reload, so `None` is a valid minimal choice. Keep a separate local sidecar
  directory if the host-selected model must survive worker restarts.
- The existing `build_local_bundle` / `harness.runtime.session.build_host_bundle`
  helpers call `str(store.path)`. A PostgreSQL store has no filesystem path, so those
  builders need the selector branch and an explicit metadata path (or `None`) rather than
  reusing the DSN.
- The store's `load_record`, `list_records`, and `delete_record` aliases provide the same
  host-facing record shape as SQLite (`SessionSummary` plus transcript).
- If the runtime prefers lazy initialization, no explicit `await store.initialize()` is
  needed before use. It is safe to call explicitly, including concurrently.

A generic selector can also pass a keyword DSN directly; `is_postgres_dsn` only recognises
URI prefixes because a libpq keyword DSN is ambiguous with other configuration strings.
Use `PostgresSessionStore(...)` explicitly for those.

Server-side plumbing needs the same branch in two places:

- `HarnessState.worker_store_path()` currently appends `<agent_id>.sqlite3` to
  `server.store_path`. For a database DSN it should return the DSN unchanged (all
  deployments may share one database; sessions carry their own deployment/agent pins).
- The server startup check that treats `store_path` as a directory (`Path(...).mkdir`)
  must be skipped when the value is a PostgreSQL DSN. Likewise, do not run the
  `HostMetadataStore` path derivation on the DSN; choose a local sidecar or `None`.

No protocol or ACP wire changes are involved.

## Schema

The store uses these `harness_`-prefixed tables. Prefixing avoids colliding with an
unrelated application's generic `sessions` table. A dedicated database (or dedicated
`schema`/`search_path`) is still recommended.

| Table | Contents |
| --- | --- |
| `harness_store_schema` | applied migration versions |
| `harness_sessions` | ownership pins, title, `cwd`, managed-workspace flag, status, timestamps |
| `harness_session_control` | one live owner token per session, plus owner PID/host for safe local crash reclamation |
| `harness_configuration_revisions` | every selected binding revision, policy revision, source, applied flag |
| `harness_configuration_transactions` | idempotency/retry records keyed by `(session_id, request_id)` |
| `harness_turns` | turn index, captured active revision, status, prompt, output, usage, timestamps |
| `harness_messages` | native model message history as canonical JSON, one row per message |
| `harness_workspace_records` | durable workspace identity/retention records, keyed by session |
| `harness_transcript` | renderable events; text deltas coalesced per sequence row |

Migrations are forward-only and run under a PostgreSQL transaction-level advisory lock,
so concurrent workers cannot race schema creation. The current schema version is exported
as `harness.runtime.postgres.SCHEMA_VERSION`. A database whose schema is newer than the
runtime supports fails with `DEPLOYMENT_UNAVAILABLE` rather than being modified. Schema
v2 adds the nullable `owner_pid`/`owner_host` columns to existing v1 control tables with
`ADD COLUMN IF NOT EXISTS`; v3 adds `harness_workspace_records`; v4 adds the
`workspace_managed` session flag. Fresh databases apply v1 through v4.

Most child tables use `ON DELETE CASCADE`; workspace records have no foreign key but are
removed explicitly. Deleting a session removes its control, revisions, retry records,
turns, messages, transcript, and workspace identity atomically. JSON payloads are stored as
canonical `TEXT`, matching the SQLite store's serialization (including its `NaN`/ordering
behaviour) rather than relying on `JSONB` coercions.

## Concurrency and multi-worker semantics

- **One connection per store instance.** Operations on that store are serialized by an
  in-process `asyncio.Lock`. Use one store per process; multiple processes each use their
  own connection. There is no built-in connection pool in this optional module.
- **Per-session serialization.** Every operation that allocates a revision, turn index,
  message sequence, or transcript sequence first takes
  `SELECT ... FROM harness_sessions ... FOR UPDATE`. Different sessions proceed in
  parallel; allocations for one session remain monotonic across worker processes.
- **Retry claims are atomic.** `claim_transaction` uses the
  `(session_id, request_id)` primary key with `INSERT ... ON CONFLICT DO NOTHING`.
  A racing caller observes the committed snapshot for an identical payload, or receives
  `REVISION_CONFLICT` while the original claim is in progress, or
  `INVALID_CONFIGURATION` if the request id was reused with a different payload. Only one
  revision can be allocated for one request id.
- **Committed means committed.** Re-committing returns the original revision and snapshot.
  `fail_transaction` does not downgrade a request that is already committed, which keeps
  idempotent recovery correct when a commit response is lost.
- **Ownership.** `session_control` has one row per session with the owner token, PID, and
  host. Acquisition uses `ON CONFLICT DO NOTHING`; the same owner token may re-acquire.
  For a different token, a claim is reclaimed only if the recorded PID is in the same
  host/PID namespace and no longer exists. A claim owned by another host or container PID
  namespace is never declared dead from a local PID check, so a live remote worker cannot
  be stolen. `stale_after` (or the store-level `control_stale_after` default) only
  reclaims claims without PID information (for example schema-v1 rows). Closing a session
  and deleting a record remove control as part of the same operation.
- **Deadlocks.** Deadlock and serialization failures are retried up to three times because
  the entire operation is rolled back first. Unique violations are not blanket-retried;
  the session-creation and transaction-claim paths handle their own races.
- **Crash recovery.** A crash on the same host is reclaimed through the PID check when
  another worker acquires control. A crashed remote worker leaves a token that requires
  explicit release (or a reviewed SQL cleanup); this module does not use heartbeats or
  time-based stealing of live remote claims. An `in_progress` retry record likewise
  requires `fail_transaction` or a reviewed cleanup before retrying, because a slow worker
  may still be validating. Turns left `running` are marked `interrupted` when a saved
  session is reopened.

The runtime's architecture note still applies: shared storage does not turn a
single-instance host into a distributed worker orchestrator. Session access remains scoped
by `(principal, deployment_id)` in the session layer; the database enforces the recorded
ownership/control rows it is given.

## Verification

Non-server checks (always runnable) cover lazy construction, DSN validation, the
missing-driver path in a subprocess, the explicit connection error path, and parity of every
public `SessionStore` method/signature:

```sh
uv run pytest -q tests/test_postgres.py
```

Real integration tests are opt-in. Point them at a disposable database (they create the
schema and clean up their own sessions; they never drop shared tables):

```sh
uv sync --extra postgres
HARNESS_TEST_POSTGRES_DSN='postgresql://postgres:test@127.0.0.1:5432/harness_test' \
  uv run pytest -q tests/test_postgres.py
```

The integration suite covers schema idempotency, session lifecycle and record aliases,
ownership and host-aware stale reclamation, revision allocation, transaction
claim/commit/fail/no-op idempotency, turns, interruption, native history, workspace
records and callbacks (including a real `WorkspaceManager` round trip), transcript
coalescing and filtering, cascade deletion, and
concurrent multi-store claims/allocations. It also opens, runs a real Pydantic AI test
turn through, and resumes a session with the PostgreSQL store via `SessionManager`.

## Operational notes and limitations

- Use TLS (`sslmode=require` or stricter) and a least-privilege role. The DSN may contain
  credentials; the store never logs it, but callers must keep it out of logs and wire DTOs.
- The role needs `CREATE` on the target schema for first-time migration, then ordinary
  `SELECT`/`INSERT`/`UPDATE`/`DELETE` on the `harness_*` tables. Pre-create the schema
  yourself if runtime DDL is prohibited.
- Back up the database before upgrading a runtime schema version. Migrations are
  forward-only.
- This module does not provide connection pooling, automatic failover, LISTEN/NOTIFY, or
  cross-database sharding. A lost connection fails the current operation; the next
  operation opens a new connection.
- Snapshot builder callbacks run inside the commit transaction and must be synchronous and
  side-effect free with respect to the store, matching the SQLite contract.
