# Authentication

Authentication identifies a caller. Your session store decides which sessions that
caller can see and which operations they can perform. ACP and AG-UI use the same
store contract, including for sessions already resident in memory.

## Resolve a principal

Pass an async `authenticate` callback when mounting each transport. It receives a
Starlette `HTTPConnection`, which exposes headers, cookies, URL, and connection state.
Return an application-defined principal for an accepted caller, or `None` to reject
an invalid or missing credential.

```python
from dataclasses import dataclass
from starlette.requests import HTTPConnection

@dataclass(frozen=True)
class Principal:
    user_id: str
    read_only: bool = False

async def authenticate(connection: HTTPConnection) -> Principal | None:
    token = connection.headers.get("authorization")
    if token == "Bearer example-token":
        return Principal(user_id="alice")
    if token == "Bearer example-readonly-token":
        return Principal(user_id="alice", read_only=True)
    return None

from hrns.server import HrnsServer

server = HrnsServer[Principal]()
# Register agents with a SessionStore[Principal] and create app with its lifespan.
server.mount_acp(app, authenticate=authenticate)
server.mount_ag_ui(app, authenticate=authenticate)
```

The fixed tokens above demonstrate the flow. An application can instead resolve a
principal from its identity provider, database, or existing HTTP authentication state.
The principal type flows through `HrnsServer[Principal]`, `Authenticator[Principal]`,
and `SessionStore[Principal]`. The store receives `Principal | None`, with `None`
representing anonymous access. The optional value reflects the mount contract;
credential rejection also uses `None` as the authentication result. A store that
requires authentication rejects a missing principal before using its typed fields.
The principal is kept on the ACP adapter for that connection. Concurrent connections
retain independent principals. Each AG-UI request authenticates independently.

If a mount omits `authenticate`, it admits callers with principal `None`. Your store
still receives that value and decides whether anonymous access is allowed. Returning
`None` from a configured authentication callback rejects admission.

## Implement a principal-aware store

Pass your store through `server.add_agent(..., session_store=...)`. Each registration
owns a separate store or an explicitly partitioned instance, so the store already has
its agent scope. The principal is passed directly to these methods:

| Method | Contract |
| --- | --- |
| `create(session_id, *, checkpoint, principal)` | Check permission to create and establish ownership/persistence for the assigned ID before the agent factory runs. `checkpoint` carries cwd and the initial snapshot. Raise `PermissionError` to reject creation. |
| `load(session_id, *, principal, operation)` | Fetch an accessible stored session and check permission for this operation. Return `DocumentCheckpoint`, return `None` to hide it, or raise `PermissionError` to reject access. |
| `list_sessions(*, principal, cwd=None, cursor=None)` | Query sessions visible to the principal, filter before pagination, and return `(summaries, next_cursor)`. |
| `delete(session_id, *, principal)` | Delete using the same permission rules as `load(..., operation="session/delete")`. |
| `save(session_id, checkpoint)` | Persist a document checkpoint from the trusted runtime, using the ownership established at creation. |

`SessionStore[Principal]` requires create/save/load. Implement
`ListableSessionStore[Principal]` to advertise listing and
`DeletableSessionStore[Principal]` to support deletion. The default
`InMemoryDocumentStore` accepts every principal and provides unrestricted access;
use an application store to enforce ownership or roles. `SQLiteDocumentStore` adds
durable checkpoints with an optional `authorize` callback.

`save` has no connection principal. Runs belong to server-owned sessions and can
continue after an ACP disconnect. Runtime saves retain the ownership and access rules
already stored by your application. Failures in runtime persistence are logged;
access-denial decisions belong in the principal-aware methods.

## Principal typing

Use the same principal type on the server, store, and authentication callback:

```python
from hrns.server import HrnsServer
from hrns.server.acp import DeletableSessionStore, ListableSessionStore
from hrns.server.fastapi import Authenticator

class UserSessionStore(
    ListableSessionStore[Principal],
    DeletableSessionStore[Principal],
):
    # Implement the methods above with principal: Principal | None.
    ...

server = HrnsServer[Principal]()
authenticate_user: Authenticator[Principal] = authenticate
```

Registration, session, and adapter types retain that principal parameter:
`RegisteredAgent[Principal, Deps, Output]` and
`PydanticAIACPAgent[Principal, Deps, Output]`; a live `Session[Deps, Output]` is
transport-independent. A store or authenticator accepting a different principal type
does not satisfy the server's type contract. For a fully anonymous server, use
`AcpServer[None]()` or `HrnsServer[None]()` and `InMemoryDocumentStore[None]`. The
runtime's `CheckpointPersistence` interface exposes only trusted saves, so connection
identity does not enter background persistence.

## Separate permissions per operation

The `operation` argument of `load` is a `SessionAccess` literal, exported from
`hrns.server.acp`. A store can combine its session query and permission check in one
method, rather than performing a separate authorization callback and then fetching
the same data again.

| Operation | When `load` runs |
| --- | --- |
| `session/load` | Before returning a resident session or restoring a stored one. |
| `session/prompt` | Before accepting the prompt into the session queue. |
| `session/cancel` | Before cancelling the active turn and pausing queued work. |
| `session/close` | Before closing the live session and removing its resident view. |
| `session/delete` | Before closing any resident session and invoking `delete`. |
| `session/set_config_option` | Before changing the model or session settings. |
| `document/didOpen`, `document/didChange`, `document/didClose`, `document/didSave`, `document/didFocus` | Before updating session document state. |
| `ag-ui/snapshot` | Before reading an AG-UI snapshot. |
| `ag-ui/events` | Before opening an AG-UI event stream. |
| `loro/snapshot` | Before returning a session's metadata or document snapshot. |
| `loro/list` | Before enumerating stored sessions over HTTP. |
| `loro/sync` | Before opening a document WebSocket; includes imports and writes. |
| `loro/write` | Before creating a session through `SQLiteDocumentStore`'s authorize hook. |

Creation, listing, and deletion also receive the principal through their dedicated
store methods. A listing query can return all visible sessions while `load` permits
prompting or deletion only for selected roles.

For example, the [FastAPI example](https://github.com/xelandernt/hrns/blob/main/examples/fastapi_app.py)
implements an application-owned store with this lookup:

```python
async def load(
    self,
    session_id: str,
    *,
    principal: Principal | None,
    operation: SessionAccess,
) -> StoredSession | None:
    user = require_principal(principal)
    if self._owners.get(session_id) != user.user_id:
        return None
    if user.read_only and operation not in {
        "session/load", "ag-ui/snapshot", "ag-ui/events",
        "loro/snapshot", "loro/list",
    }:
        raise PermissionError
    return await self._store(user).load(
        session_id, principal=user, operation=operation
    )
```

The example's reader and writer tokens identify the same owner with different
permissions. Both can list and observe that user's sessions. Only the writer can
create sessions, submit prompts, change settings, cancel, close, or delete them.
ACP attachment still follows the server's single-client attachment rules.

## Filter before pagination

The adapter calls `store.list_sessions(principal=..., cwd=..., cursor=...)` once
and sends the returned page and cursor to the client. It does not fetch an unfiltered
page and check each result afterward.

For a database-backed store, apply ownership or visibility conditions before ordering,
limiting, and creating the next cursor. A query might take this shape:

```sql
SELECT session_id, cwd, title, updated_at
FROM sessions
WHERE owner_id = :user_id
ORDER BY updated_at DESC, session_id
LIMIT :page_size
```

Bind `user_id` from the authenticated principal. Apply your store's cwd and cursor
conditions to that filtered query. Shared sessions can use a membership or permission
join instead of a single owner field.

The FastAPI example partitions its in-memory data by user before calling the underlying
store's pagination method. A page of 100 visible sessions is therefore independent of
how many sessions belong to other users.

## Resident sessions and errors

Every supported client session operation calls the principal-aware store lookup,
even when the runtime is already resident or attached. The returned stored state is
used for restoration only when the runtime is absent; a resident runtime keeps its
current history and execution state. AG-UI performs the same lookup without restoring
sessions, then reads the resident observer.

| Result | ACP | AG-UI |
| --- | --- | --- |
| Authentication returns `None` | WebSocket rejected with close code 1008 before ACP starts | HTTP 401 |
| Store raises `PermissionError` | Harness error `-32003`, `Not authorized` | HTTP 403 |
| Store `load` returns `None` | Invalid parameters for the unavailable session | HTTP 404 |
| Authorized session is not resident | `session/load` can restore it | HTTP 404 |

Denied ACP notifications have no response and perform no session mutation. Denial
responses do not include the store's exception message. Other store failures are
operational errors rather than permission denials.

Access to `session/delete` is checked before closing the resident session; the store's
`delete` method must enforce the same rule. Its final persistence operation runs after
the live session has closed. As with other storage failures, an error during that final
delete can leave a closed session in persistence.

## Connection and execution lifetime

ACP authentication runs once when the WebSocket opens. The principal is reused for
every operation, while the store can consult current database permissions on each
lookup. Permission changes affect subsequent operations. An accepted prompt continues
if permissions later change, and an open SSE stream remains open; access is checked
when the operation is accepted.

An explicitly created ACP adapter accepts
`registration.connection(principal=principal)`. Stdio accepts
`run_acp_stdio(..., principal=principal)` using an identity resolved by the host.
Both pass the supplied identity to the same store methods. They perform no HTTP
authentication and use `None` when no principal is supplied.

Server-side `registration.new_session(..., principal=...)` and
`registration.get_session(..., principal=...)` also use the store. Direct `Session`
handles are trusted Python objects for server-owned work and do not represent an ACP
caller. Authentication and session access are separate from deferred tool approval;
provider routing has registration scope and is separate from session permissions.
