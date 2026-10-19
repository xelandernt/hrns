# Workspace lifecycle

`harness_runtime.workspaces` is the runtime host's workspace **identity and lifetime** service.
It does not implement a sandbox. Execution stays in the official Pydantic AI backend selected by
the deployment: `LocalWorkspaceBackend` on a trusted host, or an upstream Harness integration such
as `SmolSandboxBackend` or `E2BSandboxBackend` for isolation.

The manager solves the part upstream deliberately leaves to the caller:

- one stable `WorkspaceRef` per session, retained across turns and process restarts;
- lazy per-session acquisition and reuse of the live backend;
- `close` releases or stops the live handle without deleting the environment;
- explicit deletion for named persistent workspaces, and a 24-hour idle TTL for ephemeral ones;
- `WORKSPACE_UNAVAILABLE` when a saved workspace is missing — never an implicit empty workspace;
- retryable record/release/delete failures that keep the reference instead of forgetting it.

It creates no second database and no bespoke Docker abstraction.

## Two separate things: record and live backend

A `WorkspaceRecord` is the durable part:

| Field | Meaning |
| --- | --- |
| `session_id` | Opaque session owner; the record key. |
| `provider` | Registered provider name; must match `WorkspaceRef.provider`. |
| `ref` | Stable `WorkspaceRef` or `None` until the backend materialises an environment. |
| `mode` | `ephemeral` (TTL deletion) or `persistent` (delete explicitly). |
| `name` | Optional stable label; implies persistent retention. |
| `created_at`, `last_used_at`, `released_at` | Timestamps for idle cleanup. |

The live backend is only an in-process handle in `WorkspaceManager._live`; it is never serialized.
On close the record remains, so the next acquisition restores the same `WorkspaceRef`. A provider
change, retention change, or name change is rejected on an existing session (`WORKSPACE_BINDING_CONFLICT`);
create a new session instead.

`WorkspaceRecord.to_dict()` gives a JSON-safe representation for a host database. The record
callback is the only persistence integration; pass a mutable mapping for a standalone manager.

## Quick start with LocalWorkspace

`LocalWorkspaceBackend` is the explicitly non-isolated option. It uses host subprocesses and the
host filesystem. Its working directory is a starting point, not a security boundary.

```python
from harness_runtime.workspaces import WorkspaceManager, local_workspace_provider

manager = WorkspaceManager(
    providers=[local_workspace_provider("/srv/harness/projects/example")],
    record_store={},  # pure in-process manager
)

lease = await manager.acquire(
    session_id="session-123",
    provider="local",
    mode="persistent",
    name="example",
)
# Bind this to the definition's workspace resource:
workspace = lease.workspace

# ... agent.run(..., workspace=workspace) across turns ...

record = await manager.close("session-123")  # release/stop, keep identity
# The same LocalWorkspace can be restored on the next acquire; the directory is untouched.
```

The directory must already exist. Deleting the record through `manager.delete()` never deletes
host files: the directory belongs to the caller.

## Registering the upstream isolated providers

Registration is explicit. Nothing in this module imports `pydantic_ai_harness`, `e2b`, or
`smolmachines`; the host installs the optional dependency and binds the official classes. A
`WorkspaceRef.provider` stored in the database can therefore never cause an import.

### Smol Sandbox (local microVM)

Descriptor: `SMOL_SANDBOX_DESCRIPTOR` (`isolation="isolated"`). Prerequisites:

- `pydantic-ai-harness[smol]==0.57.0`, which installs `smolmachines`.
- macOS on Apple silicon, or Linux x86_64/aarch64 with glibc 2.34 or newer.
- On Linux, read/write access to `/dev/kvm`.
- Optional OCI image is pulled by tag on first use. A VM has no network unless
  `allow_internet_access=True`; that access originates from the host.
- A VM is persistent. Stop it before the host exits so its disk is flushed; delete it to reclaim
  the disk. The provider backend never deletes itself.

```python
from pydantic_ai_harness.smol_sandbox import SmolSandbox, SmolSandboxBackend
from harness_runtime.workspaces import (
    SMOL_SANDBOX_DESCRIPTOR,
    WorkspaceProvider,
    WorkspaceRef,
)

smol = SmolSandbox(image="python:3.12-alpine")

async def release_smol(backend) -> None:
    # Stop the VM so smolvm flushes its disk; this is not a delete.
    await (await backend.get_sandbox()).stop()

smol_provider = WorkspaceProvider(
    descriptor=SMOL_SANDBOX_DESCRIPTOR,
    open=lambda: SmolSandboxBackend(
        image="python:3.12-alpine",
        allow_internet_access=False,
        working_dir="/workspace",
    ),
    restore=lambda ref: smol.backend(ref) if ref.provider == "smol" else None,
    release=release_smol,
    delete=smol.destroy,
)
```

`SmolSandbox.destroy(ref)` attaches briefly when necessary and tolerates an already-gone VM.
Only one process should own a VM at a time.

### E2B (remote cloud sandbox)

Descriptor: `E2B_SANDBOX_DESCRIPTOR` (`isolation="isolated"`). Prerequisites:

- `pydantic-ai-harness[e2b]==0.57.0`, which installs the E2B SDK.
- An E2B account and `E2B_API_KEY` in the runtime host's environment.
- Outbound network from the host. The SDK and provider errors stay upstream errors.
- The default sandbox lifetime is 1 hour (E2B Hobby maximum); Pro plans allow up to 86,400
  seconds. A timeout pauses the sandbox; the next attach resumes it.
- There is no separate stop operation. `E2BSandbox.destroy(ref)` kills live or paused sandboxes,
  and a workspace may remain billable until that deletion.

```python
from pydantic_ai_harness.e2b_sandbox import E2BSandbox, E2BSandboxBackend
from harness_runtime.workspaces import E2B_SANDBOX_DESCRIPTOR, WorkspaceProvider

e2b = E2BSandbox(template="my-harness-template", sandbox_timeout=3600)

e2b_provider = WorkspaceProvider(
    descriptor=E2B_SANDBOX_DESCRIPTOR,
    open=lambda: E2BSandboxBackend(
        template="my-harness-template",
        sandbox_timeout=3600,
        working_dir="/home/user/project",
    ),
    restore=lambda ref: e2b.backend(ref) if ref.provider == "e2b" else None,
    # E2B pauses on its own; leaving release unset means close keeps the sandbox for a later attach.
    delete=e2b.destroy,
)
```

Add these providers to `WorkspaceManager(providers=[smol_provider, e2b_provider])` only on a host
that installed the corresponding extra and met the platform prerequisites. If construction fails,
the error is not replaced with LocalWorkspace execution.

The manager does not add Modal, Sprites, Bubblewrap, or Docker backends. Those can be registered
through the same explicit interface when their official upstream integration exists. No provider
is inferred from a remote or untrusted reference.

## Record storage: use the session database

A pure manager can use `record_store={}`. A host should adapt its existing session store; do not
create a second store. The four callbacks may be synchronous or asynchronous:

```python
from harness_runtime.workspaces import WorkspaceManager, WorkspaceRecordCallbacks

records = WorkspaceRecordCallbacks(
    get=session_store.get_workspace_record,       # (session_id) -> WorkspaceRecord | None
    save=session_store.save_workspace_record,     # (record) -> None
    delete=session_store.delete_workspace_record, # (session_id) -> None
    records=session_store.list_workspace_records, # () -> Iterable[WorkspaceRecord]
)
manager = WorkspaceManager(providers=[...], record_store=records)
```

The manager refuses to open a fresh workspace if the record store cannot be read; a read failure is
`WORKSPACE_RECORD_FAILED`, not an assumption that no saved workspace exists. A failed save keeps the
new record in memory and in `dirty_sessions`; `lease.flush()` or the next lifecycle call retries it.

## Lifecycle API

| Call | Effect |
| --- | --- |
| `await acquire(session_id, provider=..., mode=..., name=...)` | Load or create the record, lazily construct the official backend, persist the ref as soon as upstream exposes it, and return a reusable `WorkspaceLease`. A saved ref is restored through the provider; a missing one raises `WORKSPACE_UNAVAILABLE`. |
| `await close(session_id)` | Call the provider's `release`/stop callback, drop the live handle, retain the record. Ephemeral records become eligible for TTL cleanup; persistent records wait for explicit deletion. A failed release keeps the live handle for retry. |
| `await delete(session_id)` | Explicitly destroy the environment through the provider's `delete` callback and remove its record. A failed destroy or record removal retains the record. |
| `await cleanup(now=...)` | Sweep closed ephemeral records whose idle interval is at least the TTL (24 hours by default). Persistent, live, and not-yet-due records are retained. |
| `await flush(session_id)` | Retry a failed record write for a cached record. |
| `manager.live(session_id)` | Return the in-process lease, if any. |
| `manager.dirty_sessions` | Sessions with a record that could not be persisted yet. |

`WorkspaceLease.workspace` is a normal upstream `Workspace`, so it can be passed directly as
`agent.run(workspace=...)` or bound in a `ResourceSet`. `WorkspaceLease.backend` is the official
provider backend for provider-specific escape hatches. The manager wraps the public `Workspace`
facade only to observe when upstream assigns a ref; the wrapped backend remains official. Calls
that bypass the facade (for example, a provider SDK call directly on `lease.backend`) are not
observed, so call `await lease.flush()` after such a call if it may have created the environment.
Provider `delete` callbacks should be idempotent: a record-store delete failure can make the
manager retry `delete` for an environment that has already been destroyed.

### TTL and deterministic clocks

```python
from harness_runtime.workspaces import DEFAULT_EPHEMERAL_TTL_SECONDS, WorkspaceManager

manager = WorkspaceManager(
    providers=[...],
    record_store=records,
    clock=lambda: 1_000.0,             # inject a fake clock in tests
    ephemeral_ttl=DEFAULT_EPHEMERAL_TTL_SECONDS,  # 24 hours; deployment override
)
report = await manager.cleanup(now=1_000.0 + 24 * 60 * 60)
assert report.failures == ()
```

Idle time starts at `released_at` if the session was closed, otherwise at `last_used_at` (which
covers a manager restart after a crash). Exactly TTL seconds is due; one second earlier is not.
Live handles are never swept, even if their last use is old. Persistent records are never swept.

### Failure semantics

| Failure | Result |
| --- | --- |
| Record `get` fails | `WORKSPACE_RECORD_FAILED`; no backend is opened, because a saved workspace could exist. |
| Record `save` fails | The record and live backend stay in memory; `dirty_sessions` is set and a retry through `flush()` or the next operation can persist it. |
| Provider `restore` returns `None`, raises `WorkspaceUnavailableError`, or returns a different ref | `WORKSPACE_UNAVAILABLE`; `open` is not called. |
| A restored backend reports loss on first use | The wrapper raises `WorkspaceUnavailable`, which is both a `HarnessError` with code `WORKSPACE_UNAVAILABLE` and an upstream `WorkspaceUnavailableError`. |

`WORKSPACE_UNAVAILABLE` is terminal for that workspace: retrying with the same reference cannot
repair a deleted sandbox. The recovery action is an explicit new session/workspace, optionally
carrying a transcript summary; the manager never turns the old reference into an empty
environment.
| `release` fails | `WORKSPACE_RELEASE_FAILED`; the live handle and record remain for retry. |
| `delete` fails | `WORKSPACE_DELETE_FAILED`; the reference remains. |
| Record `delete` fails | `WORKSPACE_RECORD_FAILED`; the record remains (the provider delete is idempotent and will run again on retry). |
| Cleanup cannot enumerate or delete | The record is kept and reported in `CleanupReport.failures`; a later cleanup retries. |
| Provider has no `delete` callback | `WORKSPACE_DELETE_UNSUPPORTED` on explicit delete, and a retained failure during cleanup. It is never silently forgotten. |

## Security and non-goals

- A `WorkspaceRef` is trusted input only after it comes from the caller's record store. Do not pass
  a client-supplied ref directly to `acquire`; providers that accept refs should validate IDs (the
  official Smol and E2B integrations do).
- Provider selection is a registered host name. There is no import-by-name path, no remote module
  loading, and no fallback from a requested sandbox to LocalWorkspace.
- The manager runs no shell commands itself. The official `Workspace` facade may derive file
  operations from a backend's command support, but that is upstream provider behavior, not a
  harness replacement backend.
- Plugins and provider clients remain host code; sandboxing relocates commands and files, not the
  agent runtime.
- Files are seeded by explicit import, clone, or mount. The manager performs no automatic two-way
  synchronization and keeps session/memory data outside ephemeral workspaces by default.

## Session integration hook

Session code should swap the definition's workspace resource for the lease workspace and delegate
all lifetime decisions to one manager:

```python
# Once per host (or runtime worker):
workspace_manager = WorkspaceManager(
    providers=[...],
    record_store=WorkspaceRecordCallbacks(
        get=store.get_workspace_record,
        save=store.save_workspace_record,
        delete=store.delete_workspace_record,
        records=store.list_workspace_records,
    ),
)

# Before composing a session's resources:
lease = await workspace_manager.acquire(
    session_id,
    provider=configured_provider,   # deployment binding, not client input
    mode=configured_mode,
    name=configured_name,
)
resources = resources.merge({"workspace": lease.workspace})
# Then resolve the requirement normally; ResourceSet.get_workspace() returns the same object.

# On session close (after active work is drained):
await workspace_manager.close(session_id)

# On session deletion (after the session record is no longer needed):
await workspace_manager.delete(session_id)

# At host startup and periodically (for example hourly), after rehydrating the store:
await workspace_manager.cleanup()
```

This hook is integrated into `SessionManager`/`Session`:

1. `SessionStore` (and `PostgresSessionStore`) persist one `WorkspaceRecord` per session and expose
   the four `WorkspaceRecordCallbacks`; `SessionManager` attaches them automatically.
2. A session acquires its lease before resource resolution, binds `lease.workspace` into the
   existing `ResourceSet`, reloads the recorded lease with `WorkspaceManager.restore`, and calls
   `close`/`delete` at the matching lifecycle boundaries. No live workspace object is persisted.

`Session.workspace_lease` and `Session.workspace_record()` expose the retained identity for host
and diagnostic use. This keeps resource bindings thin: `ResourceSet` still carries the native
workspace object, while the manager owns identity, release/stop, deletion, and TTL cleanup.

## Upstream references

- [Pydantic AI workspaces](https://pydantic.dev/docs/ai/core-concepts/workspace/)
- [`pydantic_ai.workspaces` API](https://pydantic.dev/docs/ai/api/pydantic-ai/workspaces/)
- [Smol Sandbox](https://pydantic.dev/docs/ai/harness/smol-sandbox/)
- [E2B Sandbox](https://pydantic.dev/docs/ai/harness/e2b-sandbox/)
