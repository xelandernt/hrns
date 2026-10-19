"""Lifecycle tests for the workspace service (CONCEPT.md section 13).

The tests use fake upstream backends and callbacks only for the lifecycle layer; the LocalWorkspace
test exercises Pydantic AI's real ``LocalWorkspaceBackend``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from harness_protocol import HarnessError
from harness_runtime.workspaces import (
    DEFAULT_EPHEMERAL_TTL_SECONDS,
    E2B_SANDBOX_DESCRIPTOR,
    LOCAL_WORKSPACE_DESCRIPTOR,
    SMOL_SANDBOX_DESCRIPTOR,
    CleanupReport,
    WorkspaceManager,
    WorkspaceProvider,
    WorkspaceProviderDescriptor,
    WorkspaceRecord,
    WorkspaceRecordCallbacks,
    WorkspaceUnavailable,
    local_workspace_provider,
)
from pydantic_ai.workspaces import (
    CommandResult,
    LocalWorkspaceBackend,
    WorkspaceRef,
    WorkspaceUnavailableError,
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingStore:
    """Small synchronous record store, matching a host's session table adapter."""

    def __init__(self) -> None:
        self.items: dict[str, WorkspaceRecord] = {}
        self.saves: list[WorkspaceRecord] = []
        self.deletes: list[str] = []
        self.save_failures = 0
        self.get_failures = 0
        self.delete_failures = 0
        self.list_failures = 0

    def get(self, session_id: str) -> WorkspaceRecord | None:
        if self.get_failures:
            self.get_failures -= 1
            raise RuntimeError("database read failed")
        return self.items.get(session_id)

    def save(self, record: WorkspaceRecord) -> None:
        if self.save_failures:
            self.save_failures -= 1
            raise RuntimeError("database write failed")
        self.saves.append(record)
        self.items[record.session_id] = record

    def delete(self, session_id: str) -> None:
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeError("database delete failed")
        self.deletes.append(session_id)
        self.items.pop(session_id, None)

    def records(self) -> list[WorkspaceRecord]:
        if self.list_failures:
            self.list_failures -= 1
            raise RuntimeError("database list failed")
        return list(self.items.values())


class FakeBackend:
    def __init__(
        self,
        *,
        ref: WorkspaceRef | None = None,
        env_id: str = "env-1",
        available: bool = True,
    ) -> None:
        self._ref = ref
        self._env_id = env_id
        self.available = available
        self.commands: list[Any] = []

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    async def working_dir(self) -> str:
        if not self.available:
            raise WorkspaceUnavailableError(f"environment {self._env_id!r} is gone")
        if self._ref is None:
            self._ref = WorkspaceRef(provider="fake", id=self._env_id)
        return f"/workspace/{self._ref.id}"

    async def run(
        self,
        command: Any,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        del shell, env, timeout
        self.commands.append(command)
        await self.working_dir()
        return CommandResult(exit_code=0, stdout="ok", stderr="")


class FakeProvider:
    """Configurable explicit provider callbacks for the lifecycle tests."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.open_calls = 0
        self.restore_calls: list[WorkspaceRef] = []
        self.release_calls: list[Any] = []
        self.delete_calls: list[WorkspaceRef] = []
        self.restore_mode = "ref"  # ref | none | raise | other
        self.available = True
        self.release_failures = 0
        self.delete_failures = 0
        self._next_env = 1

    def open(self) -> FakeBackend:
        self.open_calls += 1
        env_id = f"env-{self._next_env}"
        self._next_env += 1
        return FakeBackend(env_id=env_id, available=self.available)

    def restore(self, ref: WorkspaceRef) -> FakeBackend | None:
        self.restore_calls.append(ref)
        if self.restore_mode == "none":
            return None
        if self.restore_mode == "raise":
            raise WorkspaceUnavailableError("sandbox was deleted")
        if self.restore_mode == "other":
            return FakeBackend(ref=WorkspaceRef(provider=self.name, id="different"))
        return FakeBackend(ref=ref, available=self.available)

    def release(self, backend: FakeBackend) -> None:
        self.release_calls.append(backend)
        if self.release_failures:
            self.release_failures -= 1
            raise RuntimeError("stop failed")

    def delete(self, ref: WorkspaceRef) -> None:
        self.delete_calls.append(ref)
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeError("destroy failed")

    def build(self, *, release: bool = True, delete: bool = True) -> WorkspaceProvider:
        return WorkspaceProvider(
            descriptor=WorkspaceProviderDescriptor(
                name=self.name,
                isolation="isolated",
                backend="tests.FakeBackend",
                install="test",
                prerequisites="test",
                lifecycle="test",
                source="https://example.invalid/workspaces",
            ),
            open=self.open,
            restore=self.restore,
            release=self.release if release else None,
            delete=self.delete if delete else None,
        )


def make_record(
    session_id: str = "session",
    *,
    provider: str = "fake",
    ref: WorkspaceRef | None = None,
    mode: str = "ephemeral",
    name: str | None = None,
    now: float = 1_000.0,
    released_at: float | None = None,
) -> WorkspaceRecord:
    return WorkspaceRecord(
        session_id=session_id,
        provider=provider,
        ref=ref,
        mode=mode,  # type: ignore[arg-type]
        name=name,
        created_at=now,
        last_used_at=now,
        released_at=released_at,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> RecordingStore:
    return RecordingStore()


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def manager(
    store: RecordingStore, fake_provider: FakeProvider, clock: FakeClock
) -> WorkspaceManager:
    return WorkspaceManager(
        providers=[fake_provider.build()],
        record_store=store,
        clock=clock,
    )


async def test_acquire_is_lazy_persists_one_stable_ref_and_reuses_backend(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
    clock: FakeClock,
) -> None:
    lease = await manager.acquire("session", provider="fake")

    assert fake_provider.open_calls == 1
    assert fake_provider.restore_calls == []
    assert lease.ref is None
    assert store.items["session"].ref is None

    assert await lease.workspace.working_dir() == "/workspace/env-1"
    stable_ref = WorkspaceRef(provider="fake", id="env-1")
    assert lease.ref == stable_ref
    assert store.items["session"].ref == stable_ref
    saves_with_ref = [record for record in store.saves if record.ref == stable_ref]
    assert saves_with_ref

    same_lease = await manager.acquire("session", provider="fake")
    assert same_lease is lease
    assert fake_provider.open_calls == 1
    assert fake_provider.restore_calls == []

    result = await lease.workspace.run(["echo", "hello"])
    assert result == CommandResult(exit_code=0, stdout="ok", stderr="")
    # Ordinary use updates last_used_at in memory but does not rewrite the stable ref.
    assert [record for record in store.saves if record.ref == stable_ref] == saves_with_ref

    released_at = await manager.close("session")
    assert released_at is not None
    assert released_at.released_at == clock.now
    assert manager.live("session") is None
    assert fake_provider.release_calls == [lease.backend]

    reopened = await manager.acquire("session", provider="fake")
    assert reopened is not lease
    assert reopened.ref == stable_ref
    assert fake_provider.restore_calls == [stable_ref]
    assert fake_provider.open_calls == 1


async def test_mapping_record_store_makes_a_complete_pure_manager(clock: FakeClock) -> None:
    records: dict[str, WorkspaceRecord] = {}
    provider = FakeProvider()
    manager = WorkspaceManager(
        providers=[provider.build()],
        record_store=records,  # the documented pure-manager mapping
        clock=clock,
    )

    lease = await manager.acquire("session", provider="fake")
    await lease.workspace.working_dir()
    assert records["session"].ref == lease.ref
    await manager.close("session")
    assert records["session"].released_at == clock.now


async def test_identity_survives_a_new_manager_and_cleanup_uses_the_shared_store(
    store: RecordingStore,
    fake_provider: FakeProvider,
    clock: FakeClock,
) -> None:
    first = WorkspaceManager(providers=[fake_provider.build()], record_store=store, clock=clock)
    lease = await first.acquire("session", provider="fake")
    await lease.workspace.working_dir()
    stable_ref = lease.ref
    await first.close("session")

    second = WorkspaceManager(providers=[fake_provider.build()], record_store=store, clock=clock)
    reopened = await second.acquire("session", provider="fake")
    assert reopened.ref == stable_ref
    assert fake_provider.restore_calls == [stable_ref]

    await second.close("session")
    clock.advance(DEFAULT_EPHEMERAL_TTL_SECONDS)
    report = await second.cleanup()
    assert [record.session_id for record in report.deleted] == ["session"]
    assert fake_provider.delete_calls == [stable_ref]
    assert store.items == {}


@pytest.mark.parametrize("restore_mode", ["none", "raise"])
async def test_missing_saved_workspace_is_unavailable_and_never_recreated(
    restore_mode: str,
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
) -> None:
    saved_ref = WorkspaceRef(provider="fake", id="gone")
    store.items["session"] = make_record(ref=saved_ref)
    fake_provider.restore_mode = restore_mode

    with pytest.raises(HarnessError) as caught:
        await manager.acquire("session", provider="fake")

    assert caught.value.code == "WORKSPACE_UNAVAILABLE"
    assert fake_provider.open_calls == 0
    assert store.items["session"].ref == saved_ref


async def test_restore_identity_mismatch_is_unavailable_and_not_replaced(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
) -> None:
    saved_ref = WorkspaceRef(provider="fake", id="saved")
    store.items["session"] = make_record(ref=saved_ref)
    fake_provider.restore_mode = "other"

    with pytest.raises(WorkspaceUnavailable) as caught:
        await manager.acquire("session", provider="fake")

    assert caught.value.code == "WORKSPACE_UNAVAILABLE"
    assert fake_provider.open_calls == 0
    assert store.items["session"].ref == saved_ref


async def test_lazy_restored_backend_loss_becomes_workspace_unavailable(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
) -> None:
    saved_ref = WorkspaceRef(provider="fake", id="paused")
    store.items["session"] = make_record(ref=saved_ref)
    fake_provider.available = False

    lease = await manager.acquire("session", provider="fake")
    with pytest.raises(WorkspaceUnavailable) as caught:
        await lease.workspace.working_dir()

    assert caught.value.code == "WORKSPACE_UNAVAILABLE"
    # It is also an upstream WorkspaceUnavailableError so tools recognise the lost environment.
    assert isinstance(caught.value, WorkspaceUnavailableError)
    assert fake_provider.open_calls == 0


async def test_provider_or_retention_change_is_a_binding_conflict(
    manager: WorkspaceManager, store: RecordingStore, fake_provider: FakeProvider
) -> None:
    store.items["session"] = make_record(ref=WorkspaceRef(provider="fake", id="saved"))
    other = FakeProvider("other")
    manager.register(other.build())

    with pytest.raises(HarnessError) as caught:
        await manager.acquire("session", provider="other")
    assert caught.value.code == "WORKSPACE_BINDING_CONFLICT"

    with pytest.raises(HarnessError) as caught:
        await manager.acquire("session", provider="fake", mode="persistent")
    assert caught.value.code == "WORKSPACE_BINDING_CONFLICT"

    assert fake_provider.open_calls == 0
    assert fake_provider.restore_calls == []
    assert other.open_calls == 0


async def test_ephemeral_ttl_is_24_hours_and_persistent_or_live_records_survive(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
    clock: FakeClock,
) -> None:
    ephemeral = await manager.acquire("ephemeral", provider="fake")
    await ephemeral.workspace.working_dir()
    await manager.close("ephemeral")

    persistent = await manager.acquire(
        "persistent", provider="fake", mode="persistent", name="project"
    )
    await persistent.workspace.working_dir()
    await manager.close("persistent")

    live = await manager.acquire("live", provider="fake")
    await live.workspace.working_dir()

    clock.advance(DEFAULT_EPHEMERAL_TTL_SECONDS - 1)
    report = await manager.cleanup()
    assert isinstance(report, CleanupReport)
    assert report.deleted == ()
    assert {record.session_id for record in report.retained} == {
        "ephemeral",
        "persistent",
        "live",
    }
    assert fake_provider.delete_calls == []

    clock.advance(1)  # Exactly 24 hours after close starts deletion.
    report = await manager.cleanup()
    assert [record.session_id for record in report.deleted] == ["ephemeral"]
    assert {record.session_id for record in report.retained} == {"persistent", "live"}
    assert fake_provider.delete_calls == [WorkspaceRef(provider="fake", id="env-1")]
    assert set(store.items) == {"persistent", "live"}

    # An active handle is never swept even when its last use is older than the TTL.
    clock.advance(DEFAULT_EPHEMERAL_TTL_SECONDS * 2)
    report = await manager.cleanup()
    assert report.deleted == ()
    assert "live" in {record.session_id for record in report.retained}

    await manager.close("live")
    clock.advance(DEFAULT_EPHEMERAL_TTL_SECONDS)
    report = await manager.cleanup()
    assert [record.session_id for record in report.deleted] == ["live"]
    assert "persistent" in {record.session_id for record in report.retained}


async def test_cleanup_delete_failure_retains_reference_and_retries(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
    clock: FakeClock,
) -> None:
    lease = await manager.acquire("session", provider="fake")
    await lease.workspace.working_dir()
    await manager.close("session")
    clock.advance(DEFAULT_EPHEMERAL_TTL_SECONDS)
    fake_provider.delete_failures = 1

    report = await manager.cleanup()
    assert report.deleted == ()
    assert len(report.failures) == 1
    assert report.failures[0].session_id == "session"
    assert "session" in store.items
    assert len(fake_provider.delete_calls) == 1

    report = await manager.cleanup()
    assert [record.session_id for record in report.deleted] == ["session"]
    assert report.failures == ()
    assert store.items == {}
    assert len(fake_provider.delete_calls) == 2


async def test_release_failure_keeps_live_handle_and_is_retryable(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
) -> None:
    lease = await manager.acquire("session", provider="fake")
    await lease.workspace.working_dir()
    fake_provider.release_failures = 1

    with pytest.raises(HarnessError) as caught:
        await manager.close("session")
    assert caught.value.code == "WORKSPACE_RELEASE_FAILED"
    assert manager.live("session") is lease
    assert store.items["session"].released_at is None
    assert len(fake_provider.release_calls) == 1

    record = await manager.close("session")
    assert record is not None
    assert record.released_at is not None
    assert manager.live("session") is None
    assert len(fake_provider.release_calls) == 2


async def test_delete_failure_keeps_reference_and_retries(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
) -> None:
    lease = await manager.acquire("session", provider="fake")
    await lease.workspace.working_dir()
    fake_provider.delete_failures = 1

    with pytest.raises(HarnessError) as caught:
        await manager.delete("session")
    assert caught.value.code == "WORKSPACE_DELETE_FAILED"
    assert "session" in store.items
    assert len(fake_provider.delete_calls) == 1

    record = await manager.delete("session")
    assert record is not None
    assert store.items == {}
    assert manager.live("session") is None
    assert len(fake_provider.delete_calls) == 2


async def test_record_delete_failure_keeps_record_and_retries(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
) -> None:
    lease = await manager.acquire("session", provider="fake")
    await lease.workspace.working_dir()
    store.delete_failures = 1

    with pytest.raises(HarnessError) as caught:
        await manager.delete("session")
    assert caught.value.code == "WORKSPACE_RECORD_FAILED"
    assert "session" in store.items
    assert await manager.record("session") is not None

    await manager.delete("session")
    assert store.items == {}
    # Destroy is idempotent upstream; the second attempt is safe.
    assert len(fake_provider.delete_calls) == 2


async def test_record_save_failure_retains_live_backend_and_can_be_flushed(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
) -> None:
    store.save_failures = 1

    with pytest.raises(HarnessError) as caught:
        await manager.acquire("session", provider="fake")
    assert caught.value.code == "WORKSPACE_RECORD_FAILED"
    assert manager.live("session") is not None
    assert "session" in manager.dirty_sessions
    assert fake_provider.open_calls == 1

    # Retrying the same acquire attaches to the retained live handle and retries persistence.
    lease = await manager.acquire("session", provider="fake")
    assert manager.dirty_sessions == frozenset()
    assert store.items["session"].ref is None
    assert fake_provider.open_calls == 1

    store.save_failures = 1
    with pytest.raises(HarnessError) as caught:
        await lease.workspace.working_dir()
    assert caught.value.code == "WORKSPACE_RECORD_FAILED"
    stable_ref = WorkspaceRef(provider="fake", id="env-1")
    assert lease.ref == stable_ref
    assert store.items["session"].ref is None
    assert "session" in manager.dirty_sessions

    record = await manager.flush("session")
    assert record is not None
    assert record.ref == stable_ref
    assert store.items["session"].ref == stable_ref
    assert manager.dirty_sessions == frozenset()


async def test_record_load_failure_refuses_to_create_a_replacement(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
) -> None:
    store.get_failures = 1

    with pytest.raises(HarnessError) as caught:
        await manager.acquire("session", provider="fake")

    assert caught.value.code == "WORKSPACE_RECORD_FAILED"
    assert fake_provider.open_calls == 0
    assert fake_provider.restore_calls == []


async def test_cleanup_list_failure_makes_no_changes(
    manager: WorkspaceManager,
    store: RecordingStore,
    fake_provider: FakeProvider,
    clock: FakeClock,
) -> None:
    lease = await manager.acquire("session", provider="fake")
    await lease.workspace.working_dir()
    await manager.close("session")
    clock.advance(DEFAULT_EPHEMERAL_TTL_SECONDS)
    store.list_failures = 1

    with pytest.raises(HarnessError) as caught:
        await manager.cleanup()

    assert caught.value.code == "WORKSPACE_RECORD_FAILED"
    assert fake_provider.delete_calls == []
    assert "session" in store.items


async def test_named_workspaces_are_persistent_and_reject_ephemeral_retention(
    manager: WorkspaceManager,
) -> None:
    with pytest.raises(ValueError, match="persistent"):
        await manager.acquire("session", provider="fake", name="project", mode="ephemeral")

    lease = await manager.acquire("session", provider="fake", name="project")
    record = await lease.record()
    assert record.mode == "persistent"
    assert record.name == "project"


def test_provider_descriptors_name_official_upstream_backends_and_prerequisites() -> None:
    assert DEFAULT_EPHEMERAL_TTL_SECONDS == 24 * 60 * 60
    assert LOCAL_WORKSPACE_DESCRIPTOR.isolation == "none"
    assert "not a sandbox" in LOCAL_WORKSPACE_DESCRIPTOR.prerequisites
    assert SMOL_SANDBOX_DESCRIPTOR.isolation == "isolated"
    assert "SmolSandboxBackend" in SMOL_SANDBOX_DESCRIPTOR.backend
    assert "smolmachines" in SMOL_SANDBOX_DESCRIPTOR.install
    assert "/dev/kvm" in SMOL_SANDBOX_DESCRIPTOR.prerequisites
    assert E2B_SANDBOX_DESCRIPTOR.isolation == "isolated"
    assert "E2BSandboxBackend" in E2B_SANDBOX_DESCRIPTOR.backend
    assert "E2B_API_KEY" in E2B_SANDBOX_DESCRIPTOR.prerequisites
    assert "billable" in E2B_SANDBOX_DESCRIPTOR.lifecycle
    assert SMOL_SANDBOX_DESCRIPTOR.source.startswith("https://pydantic.dev/docs/")


async def test_local_workspace_provider_is_official_non_isolated_and_caller_owned(
    tmp_path, store: RecordingStore, clock: FakeClock
) -> None:
    provider = local_workspace_provider(tmp_path)
    assert provider.descriptor is LOCAL_WORKSPACE_DESCRIPTOR
    assert provider.isolation == "none"
    assert provider.descriptor.backend == "pydantic_ai.workspaces.LocalWorkspaceBackend"
    assert "not a sandbox" in provider.descriptor.prerequisites

    manager = WorkspaceManager(providers=[provider], record_store=store, clock=clock)
    lease = await manager.acquire("local", provider="local", mode="persistent")

    assert isinstance(lease.backend, LocalWorkspaceBackend)
    expected_ref = LocalWorkspaceBackend(tmp_path).ref
    assert lease.ref == expected_ref
    assert store.items["local"].ref == expected_ref
    await lease.workspace.write_text("hello.txt", "hello")
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello"

    await manager.close("local")
    reopened = await manager.acquire("local", provider="local")
    assert reopened.ref == expected_ref
    assert await reopened.workspace.read_text("hello.txt") == "hello"

    await manager.delete("local")
    assert store.items == {}
    # LocalWorkspace is caller-owned: deleting the session record never deletes host files.
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello"


async def test_local_provider_declines_a_ref_for_a_different_directory(
    tmp_path, store: RecordingStore, clock: FakeClock
) -> None:
    provider = local_workspace_provider(tmp_path)
    store.items["local"] = make_record(
        "local",
        provider="local",
        ref=WorkspaceRef(provider="local", id=str(tmp_path / "elsewhere")),
    )
    manager = WorkspaceManager(providers=[provider], record_store=store, clock=clock)

    with pytest.raises(WorkspaceUnavailable):
        await manager.acquire("local", provider="local")


async def test_unregistered_provider_is_never_resolved_dynamically(store: RecordingStore) -> None:
    store.items["session"] = make_record(provider="remote-untrusted")
    manager = WorkspaceManager(record_store=store)

    with pytest.raises(WorkspaceUnavailable) as caught:
        await manager.acquire("session", provider="remote-untrusted")

    assert caught.value.code == "WORKSPACE_UNAVAILABLE"
    assert manager.providers == {}
    provider = FakeProvider().build()
    manager.register(provider)
    with pytest.raises(ValueError, match="already registered"):
        manager.register(provider)


async def test_delete_without_provider_delete_callback_retains_the_reference(
    store: RecordingStore, clock: FakeClock
) -> None:
    provider = FakeProvider()
    manager = WorkspaceManager(
        providers=[provider.build(delete=False)], record_store=store, clock=clock
    )
    lease = await manager.acquire("session", provider="fake")
    await lease.workspace.working_dir()

    with pytest.raises(HarnessError) as caught:
        await manager.delete("session")

    assert caught.value.code == "WORKSPACE_DELETE_UNSUPPORTED"
    assert "session" in store.items


async def test_async_record_callbacks_are_awaited(tmp_path) -> None:
    records: dict[str, WorkspaceRecord] = {}

    async def get(session_id: str) -> WorkspaceRecord | None:
        return records.get(session_id)

    async def save(record: WorkspaceRecord) -> None:
        records[record.session_id] = record

    async def delete(session_id: str) -> None:
        records.pop(session_id, None)

    async def records_list() -> list[WorkspaceRecord]:
        return list(records.values())

    callbacks = WorkspaceRecordCallbacks(
        get=get,
        save=save,
        delete=delete,
        records=records_list,
    )
    provider = local_workspace_provider(tmp_path)
    manager = WorkspaceManager(providers=[provider], record_store=callbacks)

    lease = await manager.acquire("session", provider="local")
    assert records["session"].ref == lease.ref
    assert await manager.close("session") is not None
    assert records["session"].released_at is not None
    assert await manager.delete("session") is not None
    assert records == {}


def test_record_round_trip_keeps_identity_and_retention() -> None:
    record = make_record(
        "session",
        ref=WorkspaceRef(provider="fake", id="env-1"),
        mode="persistent",
        name="project",
    )
    restored = WorkspaceRecord.from_dict(record.to_dict())
    assert restored == record

    with pytest.raises(ValueError, match="ref provider"):
        make_record(ref=WorkspaceRef(provider="other", id="env-1"))
