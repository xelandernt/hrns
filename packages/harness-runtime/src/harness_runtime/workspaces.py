"""Workspace identity and lifecycle on top of Pydantic AI's workspace backends.

This module deliberately owns no sandbox implementation.  A host registers a
:class:`WorkspaceProvider` for each official ``pydantic_ai.workspaces.WorkspaceBackend`` it
supports, supplying explicit callbacks to create, attach to, release, and delete environments.
The manager only stores the provider-neutral :class:`WorkspaceRecord` identity (a stable
``WorkspaceRef`` plus retention metadata) and an in-process live backend per session.

The important lifecycle rules mirror CONCEPT.md section 13:

* A workspace is acquired lazily for one session and reused across turns.
* A saved reference is restored through the registered provider's ``restore`` callback.  If that
  workspace is missing, ``WORKSPACE_UNAVAILABLE`` is raised; the manager never silently opens a
  fresh environment in its place.
* ``close`` releases/stops the live backend but keeps its identity.  Ephemeral identities are
  deleted after an idle TTL (24 hours by default); named persistent identities are retained
  until an explicit ``delete``.
* A failed record, release, or delete callback leaves the reference in memory and in the record
  store for a later retry.  It is never dropped merely because cleanup failed.

The record store is caller-owned.  Pass a mutable mapping (for example ``{}`` for a pure
in-process manager) or an object with ``get``/``save``/``delete``/``records`` methods.  The
latter adapter points at the existing session database; this module does not create a second
database.  The callbacks may be synchronous or asynchronous.

No provider is imported by name from a stored reference or remote input.  Registering a
provider is the only way to make it available to the manager.
"""

from __future__ import annotations

import inspect
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

import anyio
from harness_protocol import HarnessError
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    LocalWorkspaceBackend,
    Workspace,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceRef,
    WorkspaceUnavailableError,
)

__all__ = [
    "DEFAULT_EPHEMERAL_TTL_SECONDS",
    "E2B_SANDBOX_DESCRIPTOR",
    "LOCAL_WORKSPACE_DESCRIPTOR",
    "SMOL_SANDBOX_DESCRIPTOR",
    "CleanupFailure",
    "CleanupReport",
    "WorkspaceLease",
    "WorkspaceManager",
    "WorkspaceMode",
    "WorkspaceProvider",
    "WorkspaceProviderDescriptor",
    "WorkspaceRecord",
    "WorkspaceRecordCallbacks",
    "WorkspaceRecordStore",
    "WorkspaceUnavailable",
    "local_workspace_provider",
]

DEFAULT_EPHEMERAL_TTL_SECONDS = 24 * 60 * 60
"""Idle retention for managed ephemeral workspaces, as required by CONCEPT.md section 13.3."""

WorkspaceMode: TypeAlias = Literal["ephemeral", "persistent"]
Isolation: TypeAlias = Literal["isolated", "none"]
Clock: TypeAlias = Callable[[], float]

OpenWorkspace: TypeAlias = Callable[[], WorkspaceBackend | Awaitable[WorkspaceBackend]]
RestoreWorkspace: TypeAlias = Callable[
    [WorkspaceRef],
    WorkspaceBackend | None | Awaitable[WorkspaceBackend | None],
]
ReleaseWorkspace: TypeAlias = Callable[[WorkspaceBackend], None | Awaitable[None]]
DeleteWorkspace: TypeAlias = Callable[[WorkspaceRef], None | Awaitable[None]]


class WorkspaceUnavailable(HarnessError, WorkspaceUnavailableError):
    """A saved workspace cannot be reached, or its provider is not registered.

    It is both a :class:`harness_protocol.HarnessError` with code ``WORKSPACE_UNAVAILABLE`` and
    an upstream :class:`pydantic_ai.workspaces.WorkspaceUnavailableError`, so session code and
    Pydantic AI tools can both recognise it.  The recovery action is a new session/workspace,
    never an implicit replacement in the existing session.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        HarnessError.__init__(self, "WORKSPACE_UNAVAILABLE", message, details)


def _ref_details(ref: WorkspaceRef | None) -> dict[str, str] | None:
    return None if ref is None else {"provider": ref.provider, "id": ref.id}


@dataclass(frozen=True, kw_only=True)
class WorkspaceProviderDescriptor:
    """Published facts about one official backend integration.

    Descriptors are informational and selectable by name, but they do not contain code.  A host
    must still supply the callbacks in :class:`WorkspaceProvider`.  This keeps provider SDKs
    optional and prevents a stored reference from importing arbitrary modules.
    """

    name: str
    """Provider name recorded in ``WorkspaceRef.provider`` (for example ``"local"``)."""

    isolation: Isolation
    """``"isolated"`` for a real sandbox, ``"none"`` for host execution."""

    backend: str
    """Fully qualified official ``WorkspaceBackend`` class that supplies execution."""

    install: str
    """Exact optional dependency needed to use the upstream integration."""

    prerequisites: str
    """Platform, credentials, and host prerequisites that are not installable."""

    lifecycle: str
    """How the provider creates, pauses/stops, and deletes environments."""

    source: str
    """Upstream documentation URL."""

    def __post_init__(self) -> None:
        if not self.name or any(character.isspace() or character == ":" for character in self.name):
            raise ValueError(f"provider name must be a non-empty ref-safe token, got {self.name!r}")
        if self.isolation not in ("isolated", "none"):
            raise ValueError(f"unsupported isolation {self.isolation!r}")
        for field_name in ("backend", "install", "prerequisites", "lifecycle", "source"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"provider descriptor {self.name!r} requires {field_name}")


@dataclass(frozen=True, kw_only=True)
class WorkspaceProvider:
    """Explicit host binding for one official workspace backend.

    ``open`` constructs a backend for a new environment; ``restore`` constructs a backend that
    attaches to ``ref`` (returning ``None`` when the provider does not recognise it).  Both
    callbacks must follow Pydantic AI's backend contract and perform no I/O during construction;
    the first workspace operation creates or attaches to the environment.

    ``release`` stops/pauses the live backend on session close without deleting its environment
    (for Smol this should stop the VM so its disk is flushed).  ``delete`` destroys the
    environment named by a retained reference.  Omit a callback only when the host genuinely
    does not own that lifecycle step; an ephemeral workspace with no ``delete`` callback cannot
    be automatically cleaned, and its record is retained with a reported failure.
    """

    descriptor: WorkspaceProviderDescriptor
    open: OpenWorkspace
    restore: RestoreWorkspace
    release: ReleaseWorkspace | None = None
    delete: DeleteWorkspace | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, WorkspaceProviderDescriptor):
            raise TypeError("provider descriptor must be a WorkspaceProviderDescriptor")
        if not callable(self.open) or not callable(self.restore):
            raise TypeError(f"provider {self.name!r} needs callable open and restore callbacks")
        if self.release is not None and not callable(self.release):
            raise TypeError(f"provider {self.name!r} release callback is not callable")
        if self.delete is not None and not callable(self.delete):
            raise TypeError(f"provider {self.name!r} delete callback is not callable")

    @property
    def name(self) -> str:
        return self.descriptor.name

    @property
    def isolation(self) -> Isolation:
        return self.descriptor.isolation


LOCAL_WORKSPACE_DESCRIPTOR = WorkspaceProviderDescriptor(
    name="local",
    isolation="none",
    backend="pydantic_ai.workspaces.LocalWorkspaceBackend",
    install="pydantic-ai-slim==2.57.0 (already a harness-runtime dependency)",
    prerequisites=(
        "POSIX host; the working directory must already exist; commands and absolute paths "
        "reach the whole host with the runtime user's authority, so this is not a sandbox"
    ),
    lifecycle=(
        "the directory is caller-owned; release is a no-op and delete only forgets the record, "
        "never deleting host files"
    ),
    source="https://pydantic.dev/docs/ai/core-concepts/workspace/",
)
"""Non-isolated LocalWorkspace binding.  Use only for trusted local work."""

SMOL_SANDBOX_DESCRIPTOR = WorkspaceProviderDescriptor(
    name="smol",
    isolation="isolated",
    backend="pydantic_ai_harness.smol_sandbox.SmolSandboxBackend",
    install='pydantic-ai-harness[smol]==0.57.0 (installs "smolmachines")',
    prerequisites=(
        "macOS on Apple silicon or Linux x86_64/aarch64 with glibc >= 2.34; on Linux the user "
        "needs read/write access to /dev/kvm; an OCI image is optional and is pulled by tag; "
        "new VMs have no network unless allow_internet_access=True"
    ),
    lifecycle=(
        "a persistent local microVM; the backend never deletes it. Stop the VM through the "
        "host release callback so its disk is flushed, and delete it with SmolSandbox.destroy; "
        "only one process should own a VM at a time"
    ),
    source="https://pydantic.dev/docs/ai/harness/smol-sandbox/",
)

E2B_SANDBOX_DESCRIPTOR = WorkspaceProviderDescriptor(
    name="e2b",
    isolation="isolated",
    backend="pydantic_ai_harness.e2b_sandbox.E2BSandboxBackend",
    install="pydantic-ai-harness[e2b]==0.57.0 (installs the e2b SDK)",
    prerequisites=(
        "an E2B account and E2B_API_KEY in the runtime host's environment; outbound network "
        "from the host; the default 1-hour sandbox lifetime is the Hobby-plan maximum, while "
        "Pro allows up to 86,400 seconds"
    ),
    lifecycle=(
        "E2B pauses a sandbox when its lifetime expires and resumes it on attach; there is no "
        "separate stop. E2BSandbox.destroy() kills live or paused sandboxes, and a workspace "
        "may remain billable until that deletion"
    ),
    source="https://pydantic.dev/docs/ai/harness/e2b-sandbox/",
)


def local_workspace_provider(
    working_dir: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> WorkspaceProvider:
    """Return a provider backed by official ``LocalWorkspaceBackend`` (non-isolated).

    This is an explicit opt-in to host execution: the directory only sets the starting point and
    is not a security boundary.  The provider owns no lifecycle operations; ``delete`` forgets
    the manager's record but never removes the directory or its files.
    """

    # Construct once so a relative directory is pinned now, not when a later acquire happens
    # after the process working directory changed.
    pinned = LocalWorkspaceBackend(working_dir, env=env)
    pinned_dir = pinned.ref.id
    environment = dict(env or {})

    def open_backend() -> WorkspaceBackend:
        return LocalWorkspaceBackend(pinned_dir, env=environment)

    def restore_backend(ref: WorkspaceRef) -> WorkspaceBackend | None:
        backend = LocalWorkspaceBackend(pinned_dir, env=environment)
        return backend if ref == backend.ref else None

    def release_backend(backend: WorkspaceBackend) -> None:
        del backend

    def delete_environment(ref: WorkspaceRef) -> None:
        # A LocalWorkspace is the caller's directory, not a managed environment.  Deleting the
        # session record must never delete host files.
        del ref

    return WorkspaceProvider(
        descriptor=LOCAL_WORKSPACE_DESCRIPTOR,
        open=open_backend,
        restore=restore_backend,
        release=release_backend,
        delete=delete_environment,
    )


@dataclass(frozen=True, kw_only=True)
class WorkspaceRecord:
    """Persisted identity and retention policy, independent of any live backend.

    ``session_id`` and the reference are the stable identity.  Timestamps are Unix seconds from
    the manager's clock.  A record with ``ref=None`` means acquisition began but no environment
    has been materialised yet; such a record has no environment to delete.
    """

    session_id: str
    provider: str
    created_at: float
    last_used_at: float
    ref: WorkspaceRef | None = None
    mode: WorkspaceMode = "ephemeral"
    name: str | None = None
    released_at: float | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("workspace record session_id must not be empty")
        if not self.provider:
            raise ValueError("workspace record provider must not be empty")
        if self.ref is not None and self.ref.provider != self.provider:
            raise ValueError(
                f"workspace ref provider {self.ref.provider!r} does not match record provider "
                f"{self.provider!r}"
            )
        if self.mode not in ("ephemeral", "persistent"):
            raise ValueError(f"unsupported workspace mode {self.mode!r}")
        if self.name is not None:
            if not self.name.strip():
                raise ValueError("workspace name must not be blank")
            if self.mode != "persistent":
                raise ValueError("named workspaces must use persistent retention")
        for field_name in ("created_at", "last_used_at"):
            if not math.isfinite(getattr(self, field_name)):
                raise ValueError(f"workspace record {field_name} must be finite")
        if self.released_at is not None and not math.isfinite(self.released_at):
            raise ValueError("workspace record released_at must be finite")
        if self.last_used_at < self.created_at:
            raise ValueError("workspace last_used_at cannot precede created_at")

    @property
    def idle_since(self) -> float:
        """When the current idle retention interval started."""
        return self.released_at if self.released_at is not None else self.last_used_at

    def delete_due_at(self, ttl: float) -> float | None:
        """When automatic deletion is due, or ``None`` for a retained persistent workspace."""
        if self.mode == "persistent":
            return None
        return self.idle_since + ttl

    def with_ref(self, ref: WorkspaceRef | None) -> WorkspaceRecord:
        return replace(self, ref=ref)

    def touched(self, now: float) -> WorkspaceRecord:
        return replace(self, last_used_at=now, released_at=None)

    def released(self, now: float) -> WorkspaceRecord:
        # Repeated closes do not extend the idle interval.
        released_at = self.released_at if self.released_at is not None else now
        return replace(self, last_used_at=now, released_at=released_at)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation for a caller-owned session database."""
        return {
            "session_id": self.session_id,
            "provider": self.provider,
            "ref": _ref_details(self.ref),
            "mode": self.mode,
            "name": self.name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "released_at": self.released_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkspaceRecord:
        ref_data = data.get("ref")
        ref = None
        if ref_data is not None:
            if not isinstance(ref_data, Mapping):
                raise TypeError("workspace ref must be a mapping or None")
            ref = WorkspaceRef(provider=str(ref_data["provider"]), id=str(ref_data["id"]))
        return cls(
            session_id=str(data["session_id"]),
            provider=str(data["provider"]),
            ref=ref,
            mode=str(data.get("mode", "ephemeral")),  # type: ignore[arg-type]
            name=None if data.get("name") is None else str(data["name"]),
            created_at=float(data["created_at"]),
            last_used_at=float(data["last_used_at"]),
            released_at=(None if data.get("released_at") is None else float(data["released_at"])),
        )


RecordGet: TypeAlias = Callable[[str], WorkspaceRecord | None | Awaitable[WorkspaceRecord | None]]
RecordSave: TypeAlias = Callable[[WorkspaceRecord], None | Awaitable[None]]
RecordDelete: TypeAlias = Callable[[str], None | Awaitable[None]]
RecordList: TypeAlias = Callable[
    [], Iterable[WorkspaceRecord] | Awaitable[Iterable[WorkspaceRecord]]
]


@dataclass(frozen=True, kw_only=True)
class WorkspaceRecordCallbacks:
    """Minimal callback adapter for a host's existing session record storage.

    The host maps these four operations onto its session table.  They may be sync or async.
    ``records`` is only used for TTL cleanup; a host that never runs cleanup may omit nothing,
    because safe cleanup requires enumerating retained ephemeral identities.
    """

    get: RecordGet
    save: RecordSave
    delete: RecordDelete
    records: RecordList

    def __post_init__(self) -> None:
        if not all(
            callable(callback) for callback in (self.get, self.save, self.delete, self.records)
        ):
            raise TypeError("all workspace record callbacks must be callable")


@runtime_checkable
class WorkspaceRecordStore(Protocol):
    """Storage contract accepted by :class:`WorkspaceManager`.

    A mutable mapping of session id to :class:`WorkspaceRecord` is also accepted directly, which
    makes a standalone, in-process manager useful without a database.
    """

    def get(self, session_id: str) -> WorkspaceRecord | None | Awaitable[WorkspaceRecord | None]:
        """Load one session's workspace identity, if it has one."""
        ...

    def save(self, record: WorkspaceRecord) -> None | Awaitable[None]:
        """Atomically persist the complete record."""
        ...

    def delete(self, session_id: str) -> None | Awaitable[None]:
        """Remove the session's workspace identity after its environment is gone."""
        ...

    def records(self) -> Iterable[WorkspaceRecord] | Awaitable[Iterable[WorkspaceRecord]]:
        """Enumerate records for idle TTL cleanup."""
        ...


class _MappingRecordStore:
    """Use a caller-provided mutable mapping without creating another store."""

    def __init__(self, values: MutableMapping[str, WorkspaceRecord]) -> None:
        self._values = values

    def get(self, session_id: str) -> WorkspaceRecord | None:
        return self._values.get(session_id)

    def save(self, record: WorkspaceRecord) -> None:
        self._values[record.session_id] = record

    def delete(self, session_id: str) -> None:
        self._values.pop(session_id, None)

    def records(self) -> Iterable[WorkspaceRecord]:
        return list(self._values.values())


@dataclass(frozen=True, kw_only=True)
class CleanupFailure:
    """One record that could not be removed, retained for a later retry."""

    session_id: str
    ref: WorkspaceRef | None
    error: str


@dataclass(frozen=True, kw_only=True)
class CleanupReport:
    """Outcome of one explicit ``cleanup()`` pass.

    ``failures`` are not silent losses: every failed record remains in the manager's cache and
    in the caller's record store for the next cleanup attempt.
    """

    deleted: tuple[WorkspaceRecord, ...] = ()
    retained: tuple[WorkspaceRecord, ...] = ()
    failures: tuple[CleanupFailure, ...] = ()


class _TrackedWorkspace(Workspace):
    """Official ``Workspace`` facade that records a ref as soon as the backend has one.

    The wrapped backend is still the official provider object; this class only observes
    operations so the lifetime service can persist an identity that upstream creates lazily.
    """

    def __init__(self, backend: WorkspaceBackend, manager: WorkspaceManager, session_id: str):
        super().__init__(backend)
        self._manager = manager
        self._session_id = session_id

    async def _after_failed_operation(self) -> None:
        # A cancellation or failed command can still have materialised the environment; record
        # its ref despite the cancellation, without masking the original exception.
        with anyio.CancelScope(shield=True):
            await self._manager._after_operation(self._session_id, suppress_record_errors=True)

    async def _track(self, operation: Awaitable[Any]) -> Any:
        try:
            result = await operation
        except WorkspaceUnavailableError as error:
            await self._after_failed_operation()
            raise self._manager._unavailable_from_backend(self._session_id, error) from error
        except BaseException:
            await self._after_failed_operation()
            raise
        await self._manager._after_operation(self._session_id)
        return result

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        return await self._track(super().run(command, shell=shell, env=env, timeout=timeout))

    async def working_dir(self) -> str:
        return await self._track(super().working_dir())

    async def read_bytes(self, path: str) -> bytes:
        return await self._track(super().read_bytes(path))

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self._track(super().write_bytes(path, data))

    async def stat(self, path: str) -> FileEntry:
        return await self._track(super().stat(path))

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        return await self._track(super().list_dir(path))

    async def make_dir(self, path: str) -> None:
        await self._track(super().make_dir(path))

    async def remove(self, path: str) -> None:
        await self._track(super().remove(path))

    async def exists(self, path: str) -> bool:
        return await self._track(super().exists(path))

    async def realpath(self, path: str) -> str:
        return await self._track(super().realpath(path))


class WorkspaceLease:
    """One session's lazily acquired backend plus its stable identity.

    ``workspace`` is the object a ``ResourceSet`` should bind for ``Agent.run(workspace=...)``.
    ``backend`` exposes the official provider backend for provider-specific escape hatches.
    """

    __slots__ = ("_backend", "_manager", "_session_id", "_workspace")

    def __init__(
        self,
        manager: WorkspaceManager,
        session_id: str,
        backend: WorkspaceBackend,
        workspace: Workspace,
    ) -> None:
        self._manager = manager
        self._session_id = session_id
        self._backend = backend
        self._workspace = workspace

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def provider(self) -> str:
        return self._manager.record_snapshot(self._session_id).provider

    @property
    def backend(self) -> WorkspaceBackend:
        """The official provider backend, with workspace policy wrappers unwrapped."""
        return self._workspace.backend

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._backend.ref

    async def record(self) -> WorkspaceRecord:
        record = await self._manager.record(self._session_id)
        if record is None:
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                f"Session {self._session_id!r} has a live workspace but no record.",
                {"session_id": self._session_id},
            )
        return record

    async def flush(self) -> WorkspaceRecord | None:
        """Persist the current identity after a record callback failure."""
        return await self._manager.flush(self._session_id)

    def __repr__(self) -> str:
        return f"WorkspaceLease(session_id={self._session_id!r}, ref={self._backend.ref!r})"


@dataclass
class _Live:
    provider: WorkspaceProvider
    backend: WorkspaceBackend
    lease: WorkspaceLease


async def _invoke(callback: Callable[..., Any], *args: Any) -> Any:
    """Call an explicitly bound callback, awaiting it only when it returns an awaitable."""
    value = callback(*args)
    if inspect.isawaitable(value):
        value = await value
    return value


def _error_text(error: BaseException) -> str:
    if isinstance(error, HarnessError):
        return f"{error.code}: {error.message}"
    return f"{type(error).__name__}: {error}"


class WorkspaceManager:
    """Own workspace identities, live handles, retention, and cleanup for sessions.

    Providers are bound explicitly.  Sessions are addressed by an opaque ID; the manager never
    derives a provider or import path from that ID or from a saved reference.  Acquisition is
    lazy in the upstream sense: the provider's ``open``/``restore`` callback runs when the
    session first needs a workspace, constructs a backend without I/O, and the environment is
    created or attached on the backend's first operation.

    A pure in-process manager can use ``record_store={}``.  A runtime host should pass
    :class:`WorkspaceRecordCallbacks` backed by its existing session table.  No session database
    is created here.
    """

    def __init__(
        self,
        *,
        providers: Iterable[WorkspaceProvider] = (),
        record_store: (
            WorkspaceRecordStore | Mapping[str, WorkspaceRecord] | WorkspaceRecordCallbacks | None
        ) = None,
        clock: Clock = time.time,
        ephemeral_ttl: float = DEFAULT_EPHEMERAL_TTL_SECONDS,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            not isinstance(ephemeral_ttl, (int, float))
            or isinstance(ephemeral_ttl, bool)
            or not math.isfinite(ephemeral_ttl)
            or ephemeral_ttl <= 0
        ):
            raise ValueError("ephemeral_ttl must be a positive finite number of seconds")
        self._clock = clock
        self._ephemeral_ttl = float(ephemeral_ttl)
        self._providers: dict[str, WorkspaceProvider] = {}
        for provider in providers:
            self.register(provider)
        self._record_store = self._coerce_store(record_store)
        self._records: dict[str, WorkspaceRecord] = {}
        self._live: dict[str, _Live] = {}
        self._dirty: set[str] = set()
        self._locks: dict[str, anyio.Lock] = {}
        self._cleanup_lock = anyio.Lock()

    @staticmethod
    def _coerce_store(
        record_store: (
            WorkspaceRecordStore | Mapping[str, WorkspaceRecord] | WorkspaceRecordCallbacks | None
        ),
    ) -> WorkspaceRecordStore:
        if record_store is None:
            return _MappingRecordStore({})
        if isinstance(record_store, MutableMapping):
            return _MappingRecordStore(record_store)
        if isinstance(record_store, Mapping):
            # An immutable seed mapping still makes a usable pure manager; updates go to the
            # manager's own copy rather than failing on the first save.
            return _MappingRecordStore(dict(record_store))
        if not isinstance(record_store, WorkspaceRecordCallbacks):
            for method in ("get", "save", "delete", "records"):
                if not callable(getattr(record_store, method, None)):
                    raise TypeError(
                        "record_store must be a mutable mapping or implement "
                        "get/save/delete/records"
                    )
        return record_store  # type: ignore[return-value]

    # -- provider registration -------------------------------------------------

    def register(self, provider: WorkspaceProvider) -> None:
        """Register one explicit provider.  Unknown names can never be resolved dynamically."""
        if not isinstance(provider, WorkspaceProvider):
            raise TypeError("register() takes a WorkspaceProvider")
        if provider.name in self._providers:
            raise ValueError(f"workspace provider {provider.name!r} is already registered")
        self._providers[provider.name] = provider

    @property
    def providers(self) -> Mapping[str, WorkspaceProvider]:
        return MappingProxyType(dict(self._providers))

    def provider(self, name: str) -> WorkspaceProvider | None:
        return self._providers.get(name)

    @property
    def ephemeral_ttl(self) -> float:
        return self._ephemeral_ttl

    @property
    def dirty_sessions(self) -> frozenset[str]:
        """Sessions whose latest record could not yet be persisted."""
        return frozenset(self._dirty)

    def live(self, session_id: str) -> WorkspaceLease | None:
        live = self._live.get(session_id)
        return None if live is None else live.lease

    def record_snapshot(self, session_id: str) -> WorkspaceRecord:
        record = self._records.get(session_id)
        if record is None:
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                f"Session {session_id!r} has no cached workspace record.",
                {"session_id": session_id},
            )
        return record

    # -- record plumbing -------------------------------------------------------

    def _lock_for(self, session_id: str) -> anyio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = anyio.Lock()
            self._locks[session_id] = lock
        return lock

    def _now(self, now: float | None = None) -> float:
        value = float(self._clock() if now is None else now)
        if not math.isfinite(value):
            raise ValueError("workspace clock returned a non-finite time")
        return value

    async def _load_record(self, session_id: str) -> WorkspaceRecord | None:
        try:
            loaded = await _invoke(self._record_store.get, session_id)
        except Exception as error:
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                "The workspace record store could not be read; refusing to create a new "
                "workspace because a saved workspace may exist.",
                {"session_id": session_id},
            ) from error
        if loaded is None:
            return None
        if not isinstance(loaded, WorkspaceRecord):
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                "The workspace record store returned an invalid record.",
                {"session_id": session_id, "type": type(loaded).__name__},
            )
        if loaded.session_id != session_id:
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                "The workspace record store returned a record for a different session.",
                {"session_id": session_id, "record_session_id": loaded.session_id},
            )
        return loaded

    async def _save_record(self, record: WorkspaceRecord) -> None:
        try:
            await _invoke(self._record_store.save, record)
        except Exception as error:
            self._dirty.add(record.session_id)
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                "The workspace identity is retained in memory but could not be persisted; "
                "retry flush() or the lifecycle operation.",
                {
                    "session_id": record.session_id,
                    "provider": record.provider,
                    "ref": _ref_details(record.ref),
                },
            ) from error
        self._dirty.discard(record.session_id)

    async def _delete_record(self, session_id: str) -> None:
        try:
            await _invoke(self._record_store.delete, session_id)
        except Exception as error:
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                "The workspace environment is removed but its saved identity could not be "
                "deleted; the record is retained for a retry.",
                {"session_id": session_id},
            ) from error
        self._dirty.discard(session_id)

    async def _all_records(self) -> dict[str, WorkspaceRecord]:
        try:
            listed = await _invoke(self._record_store.records)
        except Exception as error:
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                "The workspace record store could not be enumerated; cleanup made no changes.",
                {},
            ) from error
        try:
            if isinstance(listed, Mapping):
                values: Iterable[Any] = listed.values()
            elif hasattr(listed, "__aiter__"):
                values = [item async for item in listed]  # type: ignore[union-attr]
            else:
                values = listed  # type: ignore[assignment]
            records: dict[str, WorkspaceRecord] = {}
            for record in values:
                if not isinstance(record, WorkspaceRecord):
                    raise HarnessError(
                        "WORKSPACE_RECORD_FAILED",
                        "The workspace record store returned an invalid record during cleanup.",
                        {"type": type(record).__name__},
                    )
                records[record.session_id] = record
        except HarnessError:
            raise
        except Exception as error:
            raise HarnessError(
                "WORKSPACE_RECORD_FAILED",
                "The workspace record store returned an invalid enumeration; cleanup made no "
                "changes.",
                {},
            ) from error
        # The in-memory cache can be newer than the store after a failed save.
        records.update(self._records)
        return records

    async def record(self, session_id: str) -> WorkspaceRecord | None:
        async with self._lock_for(session_id):
            return await self._record_locked(session_id)

    async def _record_locked(self, session_id: str) -> WorkspaceRecord | None:
        record = self._records.get(session_id)
        if record is None:
            record = await self._load_record(session_id)
            if record is not None:
                self._records[session_id] = record
        return record

    async def flush(self, session_id: str) -> WorkspaceRecord | None:
        """Persist the newest cached record, retrying a failed record callback."""
        async with self._lock_for(session_id):
            record = await self._record_locked(session_id)
            if record is None:
                return None
            live = self._live.get(session_id)
            if live is not None:
                record = self._apply_backend_ref(record, live.backend)
                record = replace(record, last_used_at=self._now())
            self._records[session_id] = record
            await self._save_record(record)
            return record

    # -- identity and acquisition ---------------------------------------------

    def _apply_backend_ref(
        self,
        record: WorkspaceRecord,
        backend: WorkspaceBackend,
    ) -> WorkspaceRecord:
        current = backend.ref
        if current is None:
            return record
        if current.provider != record.provider:
            raise HarnessError(
                "WORKSPACE_PROVIDER_MISMATCH",
                "The provider backend returned a workspace reference for another provider.",
                {
                    "session_id": record.session_id,
                    "expected_provider": record.provider,
                    "actual_provider": current.provider,
                },
            )
        if record.ref is not None and current != record.ref:
            raise WorkspaceUnavailable(
                "The workspace identity changed after it was persisted; refusing to continue "
                "in a different environment.",
                {
                    "session_id": record.session_id,
                    "expected": _ref_details(record.ref),
                    "actual": _ref_details(current),
                },
            )
        if record.ref == current:
            return record
        return record.with_ref(current)

    async def _open(
        self, session_id: str, provider: WorkspaceProvider, ref: WorkspaceRef | None
    ) -> WorkspaceBackend:
        if ref is None:
            try:
                backend = await _invoke(provider.open)
            except WorkspaceUnavailableError as error:
                raise WorkspaceUnavailable(
                    f"Workspace provider {provider.name!r} could not create an environment.",
                    {"session_id": session_id, "provider": provider.name},
                ) from error
            if backend is None:
                raise WorkspaceUnavailable(
                    f"Workspace provider {provider.name!r} returned no workspace backend.",
                    {"session_id": session_id, "provider": provider.name},
                )
            return backend

        try:
            restored = await _invoke(provider.restore, ref)
        except WorkspaceUnavailableError as error:
            raise WorkspaceUnavailable(
                f"The saved {ref.provider}:{ref.id} workspace no longer exists.",
                {
                    "session_id": session_id,
                    "provider": ref.provider,
                    "ref": _ref_details(ref),
                },
            ) from error
        if restored is None:
            raise WorkspaceUnavailable(
                f"Workspace provider {provider.name!r} does not recognise the saved "
                f"{ref.provider}:{ref.id} workspace; it was not recreated.",
                {
                    "session_id": session_id,
                    "provider": ref.provider,
                    "ref": _ref_details(ref),
                },
            )
        return restored

    @staticmethod
    def _validate_backend(backend: Any) -> WorkspaceBackend:
        if not isinstance(backend, WorkspaceBackend):
            raise HarnessError(
                "WORKSPACE_PROVIDER_INVALID",
                "A workspace provider returned an object that is not an upstream WorkspaceBackend.",
                {"type": type(backend).__name__},
            )
        return backend

    def _binding_conflict(
        self,
        session_id: str,
        record: WorkspaceRecord,
        provider: str,
        mode: WorkspaceMode | None,
        name: str | None,
    ) -> HarnessError | None:
        if record.provider != provider:
            return HarnessError(
                "WORKSPACE_BINDING_CONFLICT",
                "A session's workspace provider cannot change; create a new session.",
                {
                    "session_id": session_id,
                    "stored_provider": record.provider,
                    "requested_provider": provider,
                },
            )
        if mode is not None and record.mode != mode:
            return HarnessError(
                "WORKSPACE_BINDING_CONFLICT",
                "A session's workspace retention mode cannot change; create a new session.",
                {
                    "session_id": session_id,
                    "stored_mode": record.mode,
                    "requested_mode": mode,
                },
            )
        if name is not None and record.name != name:
            return HarnessError(
                "WORKSPACE_BINDING_CONFLICT",
                "A session's workspace name cannot change; create a new session.",
                {
                    "session_id": session_id,
                    "stored_name": record.name,
                    "requested_name": name,
                },
            )
        return None

    async def acquire(
        self,
        session_id: str,
        *,
        provider: str,
        mode: WorkspaceMode | None = None,
        name: str | None = None,
    ) -> WorkspaceLease:
        """Acquire (or reattach to) the session's workspace exactly once.

        ``provider`` names an explicitly registered provider.  For an existing record, omitted
        ``mode``/``name`` inherit the stored values; supplying a different value is a binding
        conflict rather than a silent switch.  A new ``name`` implies persistent retention.
        """
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not provider:
            raise ValueError("provider must not be empty")
        if mode is not None and mode not in ("ephemeral", "persistent"):
            raise ValueError(f"unsupported workspace mode {mode!r}")
        if name is not None and not name.strip():
            raise ValueError("workspace name must not be blank")
        if name is not None and mode == "ephemeral":
            raise ValueError("named workspaces must use persistent retention")

        async with self._lock_for(session_id):
            provider_impl = self._providers.get(provider)
            if provider_impl is None:
                raise WorkspaceUnavailable(
                    f"Workspace provider {provider!r} is not registered on this host.",
                    {"session_id": session_id, "provider": provider},
                )

            record = await self._record_locked(session_id)
            live = self._live.get(session_id)

            if live is not None:
                if record is None:
                    raise HarnessError(
                        "WORKSPACE_RECORD_FAILED",
                        "A live workspace exists without a retained identity.",
                        {"session_id": session_id},
                    )
                conflict = self._binding_conflict(session_id, record, provider, mode, name)
                if conflict is not None:
                    raise conflict
                record = self._apply_backend_ref(record, live.backend)
                record = record.touched(self._now())
                self._records[session_id] = record
                if session_id in self._dirty:
                    await self._save_record(record)
                return live.lease

            now = self._now()
            if record is None:
                chosen_mode: WorkspaceMode = (
                    mode
                    if mode is not None
                    else ("persistent" if name is not None else "ephemeral")
                )
                if name is not None and chosen_mode != "persistent":
                    raise ValueError("named workspaces must use persistent retention")
                record = WorkspaceRecord(
                    session_id=session_id,
                    provider=provider,
                    created_at=now,
                    last_used_at=now,
                    mode=chosen_mode,
                    name=name,
                )
            else:
                conflict = self._binding_conflict(session_id, record, provider, mode, name)
                if conflict is not None:
                    raise conflict

            backend = await self._open(session_id, provider_impl, record.ref)
            self._validate_backend(backend)
            record = self._apply_backend_ref(record, backend)
            record = record.touched(now)

            workspace = _TrackedWorkspace(backend, self, session_id)
            lease = WorkspaceLease(self, session_id, backend, workspace)
            self._records[session_id] = record
            self._live[session_id] = _Live(
                provider=provider_impl,
                backend=backend,
                lease=lease,
            )
            # State is registered before persistence: if the record callback fails, the live
            # backend and stable ref remain available for a retry rather than being discarded.
            await self._save_record(record)
            return lease

    def _unavailable_from_backend(
        self, session_id: str, error: WorkspaceUnavailableError
    ) -> WorkspaceUnavailable:
        record = self._records.get(session_id)
        ref = None if record is None else record.ref
        return WorkspaceUnavailable(
            "The saved workspace is no longer available; it was not silently recreated.",
            {
                "session_id": session_id,
                "provider": None if record is None else record.provider,
                "ref": _ref_details(ref),
            },
        )

    async def _after_operation(
        self, session_id: str, *, suppress_record_errors: bool = False
    ) -> None:
        async with self._lock_for(session_id):
            live = self._live.get(session_id)
            record = self._records.get(session_id)
            if live is None or record is None:
                return
            try:
                updated = self._apply_backend_ref(record, live.backend)
            except HarnessError:
                if suppress_record_errors:
                    return
                raise
            updated = replace(updated, last_used_at=self._now(), released_at=None)
            self._records[session_id] = updated
            if updated.ref != record.ref or session_id in self._dirty:
                try:
                    await self._save_record(updated)
                except HarnessError:
                    # The ref is kept in the cache and _dirty; a later flush/lifecycle call
                    # retries.  On an error path do not mask the backend's own exception.
                    if not suppress_record_errors:
                        raise

    # -- close, release, delete ------------------------------------------------

    async def close(self, session_id: str) -> WorkspaceRecord | None:
        """Release/stop the live handle while retaining workspace identity.

        The environment is not deleted.  Ephemeral records keep their reference for the idle
        TTL; persistent records retain it indefinitely.  A failed release leaves the live handle
        and record in place so ``close`` can be retried.
        """
        if not session_id:
            raise ValueError("session_id must not be empty")
        async with self._lock_for(session_id):
            live = self._live.get(session_id)
            record = await self._record_locked(session_id)
            now = self._now()
            if live is not None:
                if record is None:
                    raise HarnessError(
                        "WORKSPACE_RECORD_FAILED",
                        "A live workspace exists without a retained identity.",
                        {"session_id": session_id},
                    )
                record = self._apply_backend_ref(record, live.backend)
                if live.provider.release is not None:
                    try:
                        await _invoke(live.provider.release, live.backend)
                    except WorkspaceUnavailableError as error:
                        raise self._unavailable_from_backend(session_id, error) from error
                    except Exception as error:
                        raise HarnessError(
                            "WORKSPACE_RELEASE_FAILED",
                            f"Workspace provider {live.provider.name!r} could not release the "
                            "live handle; the workspace reference is retained for a retry.",
                            {
                                "session_id": session_id,
                                "provider": live.provider.name,
                                "ref": _ref_details(record.ref),
                            },
                        ) from error
                del self._live[session_id]
            if record is None:
                return None
            record = record.released(now)
            self._records[session_id] = record
            await self._save_record(record)
            return record

    async def delete(self, session_id: str) -> WorkspaceRecord | None:
        """Explicitly delete a workspace and its retained identity.

        Persistent workspaces are only removed through this method.  A failed provider delete
        or record delete keeps the reference for a retry; a provider without a delete callback
        cannot have a managed environment deleted and raises rather than forgetting it.
        """
        if not session_id:
            raise ValueError("session_id must not be empty")
        async with self._lock_for(session_id):
            live = self._live.get(session_id)
            record = await self._record_locked(session_id)
            if live is None and record is None:
                return None

            release_error: Exception | None = None
            if live is not None:
                if record is not None:
                    record = self._apply_backend_ref(record, live.backend)
                if live.provider.release is not None:
                    try:
                        await _invoke(live.provider.release, live.backend)
                    except Exception as error:
                        # Deletion supersedes stop.  Keep the live handle attached until the
                        # destroy succeeds, so a failed delete remains retryable.
                        release_error = error
                    else:
                        del self._live[session_id]
                else:
                    del self._live[session_id]

            if release_error is not None and (record is None or record.ref is None):
                raise HarnessError(
                    "WORKSPACE_RELEASE_FAILED",
                    "The live workspace could not be stopped and has no provider delete step; "
                    "the handle and reference are retained for a retry.",
                    {
                        "session_id": session_id,
                        "provider": None if live is None else live.provider.name,
                        "ref": None if record is None else _ref_details(record.ref),
                    },
                ) from release_error

            if record is not None and record.ref is not None:
                provider = self._providers.get(record.provider)
                if provider is None:
                    raise WorkspaceUnavailable(
                        f"Workspace provider {record.provider!r} is not registered; the "
                        "environment cannot be deleted and its reference is retained.",
                        {
                            "session_id": session_id,
                            "provider": record.provider,
                            "ref": _ref_details(record.ref),
                        },
                    )
                if provider.delete is None:
                    raise HarnessError(
                        "WORKSPACE_DELETE_UNSUPPORTED",
                        f"Workspace provider {provider.name!r} has no delete callback; the "
                        "reference is retained instead of being forgotten.",
                        {
                            "session_id": session_id,
                            "provider": provider.name,
                            "ref": _ref_details(record.ref),
                        },
                    )
                try:
                    await _invoke(provider.delete, record.ref)
                except Exception as error:
                    raise HarnessError(
                        "WORKSPACE_DELETE_FAILED",
                        f"Workspace provider {provider.name!r} could not delete the "
                        "environment; the reference is retained for a retry.",
                        {
                            "session_id": session_id,
                            "provider": provider.name,
                            "ref": _ref_details(record.ref),
                        },
                    ) from error
                # The environment is gone from here on, even if persisting its removal fails.
                self._live.pop(session_id, None)

            if record is not None:
                await self._delete_record(session_id)
                self._records.pop(session_id, None)
            return record

    async def cleanup(self, *, now: float | None = None) -> CleanupReport:
        """Delete idle ephemeral workspaces and report retained/failed cleanup.

        Persistent records are never touched automatically.  Live handles and records that have
        not reached the idle TTL are retained.  If a delete or record callback fails, that
        record remains in both the in-memory cache and the caller's store for the next pass.
        """
        instant = self._now(now)
        async with self._cleanup_lock:
            records = await self._all_records()
            deleted: list[WorkspaceRecord] = []
            retained: list[WorkspaceRecord] = []
            failures: list[CleanupFailure] = []

            for session_id in sorted(records):
                record = records[session_id]
                async with self._lock_for(session_id):
                    current = self._records.get(session_id, record)
                    due_at = current.delete_due_at(self._ephemeral_ttl)
                    if (
                        current.mode == "persistent"
                        or session_id in self._live
                        or due_at is None
                        or instant < due_at
                    ):
                        retained.append(current)
                        continue

                    if current.ref is not None:
                        provider = self._providers.get(current.provider)
                        if provider is None:
                            failures.append(
                                CleanupFailure(
                                    session_id=session_id,
                                    ref=current.ref,
                                    error=(
                                        f"provider {current.provider!r} is not registered; "
                                        "reference retained"
                                    ),
                                )
                            )
                            continue
                        if provider.delete is None:
                            failures.append(
                                CleanupFailure(
                                    session_id=session_id,
                                    ref=current.ref,
                                    error=(
                                        f"provider {provider.name!r} has no delete callback; "
                                        "reference retained"
                                    ),
                                )
                            )
                            continue
                        try:
                            await _invoke(provider.delete, current.ref)
                        except Exception as error:
                            failures.append(
                                CleanupFailure(
                                    session_id=session_id,
                                    ref=current.ref,
                                    error=_error_text(error),
                                )
                            )
                            continue

                    # The environment is gone (or never materialised); removing the stored
                    # identity cannot lose a workspace now.
                    try:
                        await self._delete_record(session_id)
                    except HarnessError as error:
                        failures.append(
                            CleanupFailure(
                                session_id=session_id,
                                ref=current.ref,
                                error=_error_text(error),
                            )
                        )
                        self._records[session_id] = current
                        continue
                    self._records.pop(session_id, None)
                    deleted.append(current)

            return CleanupReport(
                deleted=tuple(deleted),
                retained=tuple(retained),
                failures=tuple(failures),
            )
