# Optional PostgreSQL session store

`hrns.runtime.postgres.PostgresSessionStore` implements the same scoped ACP persistence
contract as `SQLiteSessionStore`. Importing the module does not import the driver or open a
connection.

## Install

```sh
uv sync --extra postgres
# or: pip install 'hrns[postgres]'
```

The extra is `psycopg[binary]>=3.2`. Without it, `initialize()` raises the driver's
actionable `ImportError`.

## Usage

```python
import os
from hrns.runtime.postgres import PostgresSessionStore

store = PostgresSessionStore(os.environ["DATABASE_URL"])
await store.initialize()
agents = AgentServer(store=store, authenticate=authenticate)
agents.mount(app)
```

`AgentServer` owns the lifespan: it initializes the store and closes it at shutdown. A
custom integration may use the scoped views directly:

```python
scoped = store.scoped(principal="alice", deployment_id='["research","1.0.0"]')
record = await scoped.get(session_id)
await scoped.update(session_id, title="Name", effort="high", touch=True)
await scoped.delete(session_id)
```

## Schema and semantics

One `acp_sessions` table stores:

- native `ModelMessage` history as Pydantic JSON;
- ACP `session/update` transcript entries;
- selected model;
- cwd, title, effort, selected bindings, usage totals, timestamps.
- the next non-reusable session update revision.

Every persisted ACP update carries its stable `_meta.hrns.update.updateId` and monotonic
`revision`. Revision blocks are reserved under a row lock before live delivery, so a crash may
leave a gap but cannot reuse a position. There are no turn, revision-log, workspace, or
transcript-owner tables. SQLite and
PostgreSQL use the same row shape and scope predicates. Existing databases are opened
with an additive `next_update_revision` column migration.

The shared connection runs in autocommit mode. Reads therefore never leave an idle
transaction open, and multi-statement mutations use an explicit transaction that rolls back
on database errors or cancellation. A failed statement does not poison later sessions.

HRNS fails committed transcript saves with `SESSION_DURABILITY_FAILED`; it does not report a
durable update as successfully committed when the row cannot be written. Lease fencing still
rejects stale writers with `SESSION_CONFLICT`.

## Configuration

`PostgresSessionStore(dsn)` accepts a `postgresql://` or `postgres://` URL. A filesystem path
is rejected by `build_store` and falls through to SQLite only when it is not a PostgreSQL DSN.
`build_store` is used by the stdio host; the embedded server accepts a constructed store.

Live PostgreSQL verification requires a running service. If none is available, record the
round-trip/restart check as outstanding rather than claiming it passed.
