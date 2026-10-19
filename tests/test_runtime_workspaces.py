"""Managed workspace lifecycle integration: sessions, persistence, close/delete hooks.

These tests use the real workspace service and the real Pydantic AI local workspace backend,
plus a fake isolated provider where lifecycle callbacks must be observable. They never
substitute a host path when a managed workspace is configured but unreachable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

import pytest
from harness_protocol import HarnessError
from harness_runtime import (
    AgentDefinition,
    DefinitionBinding,
    PluginDescriptor,
    PluginRegistry,
    ResourceRequirement,
    SessionManager,
    SessionStore,
    WorkspaceBinding,
)
from harness_runtime.workspaces import (
    WorkspaceManager,
    WorkspaceProvider,
    WorkspaceProviderDescriptor,
    WorkspaceRecord,
    local_workspace_provider,
)
from pydantic_ai import Agent
from pydantic_ai.capabilities import Capability
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.workspaces import (
    CommandResult,
    WorkspaceRef,
    WorkspaceUnavailableError,
)

WORKSPACE_PLUGIN = "test.workspace"


async def probe_workspace(ctx: RunContext[Any]) -> str:
    """Write and read a file through the run's workspace."""
    await ctx.workspace.write_bytes("probe.txt", b"workspace-hello")
    data = await ctx.workspace.read_bytes("probe.txt")
    return data.decode()


def build_probe_capability(config: Any, binding: Any) -> Capability[Any]:
    return Capability(id=binding.binding_id, instructions="WORKSPACE_ACTIVE", tools=[probe_workspace])


def workspace_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register(
        PluginDescriptor(
            plugin_id=WORKSPACE_PLUGIN,
            version="1.0.0",
            description="Workspace probe",
            factory=build_probe_capability,
            change_mode="next_turn",
        )
    )
    return registry


def workspace_definition(
    *,
    requires_workspace: bool = True,
    plugin_id: str = WORKSPACE_PLUGIN,
) -> AgentDefinition:
    def factory(*, managed_capabilities=()) -> Agent[Any, Any]:
        return Agent(
            TestModel(call_tools=["probe_workspace"], custom_output_text="ws-done"),
            capabilities=list(managed_capabilities),
        )

    return AgentDefinition(
        agent_id="test.workspace_agent",
        name="Workspace agent",
        definition_version="1.0.0",
        factory=factory,
        bindings=[DefinitionBinding(binding_id="probe", plugin_id=plugin_id)],
        resources=(
            [ResourceRequirement(name="workspace.main", kind="workspace")]
            if requires_workspace
            else []
        ),
    )


# --- fake isolated provider -------------------------------------------------


class FakeBackend:
    def __init__(self, *, ref: WorkspaceRef | None = None, env_id: str = "env-1") -> None:
        self._ref = ref
        self._env_id = env_id

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    async def working_dir(self) -> str:
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
        await self.working_dir()
        return CommandResult(exit_code=0, stdout="ok", stderr="")


class FakeProvider:
    def __init__(self, *, open_error: bool = False) -> None:
        self.open_error = open_error
        self.open_calls = 0
        self.restore_calls: list[WorkspaceRef] = []
        self.release_calls: list[Any] = []
        self.delete_calls: list[WorkspaceRef] = []
        self.release_failures = 0

    def open(self) -> FakeBackend:
        self.open_calls += 1
        if self.open_error:
            raise WorkspaceUnavailableError("no capacity for a sandbox")
        return FakeBackend(env_id=f"env-{self.open_calls}")

    def restore(self, ref: WorkspaceRef) -> FakeBackend:
        self.restore_calls.append(ref)
        return FakeBackend(ref=ref)

    def release(self, backend: FakeBackend) -> None:
        self.release_calls.append(backend)
        if self.release_failures:
            self.release_failures -= 1
            raise RuntimeError("stop failed")

    def delete(self, ref: WorkspaceRef) -> None:
        self.delete_calls.append(ref)

    def provider(self) -> WorkspaceProvider:
        return WorkspaceProvider(
            descriptor=WorkspaceProviderDescriptor(
                name="fake",
                isolation="isolated",
                backend="tests.FakeBackend",
                install="test",
                prerequisites="test",
                lifecycle="test",
                source="test",
            ),
            open=self.open,
            restore=self.restore,
            release=self.release,
            delete=self.delete,
        )


def make_workspace_manager(
    tmp_path,
    *,
    manager: WorkspaceManager | None = None,
    fake: FakeProvider | None = None,
    provider: WorkspaceProvider | None = None,
    binding: WorkspaceBinding | None = None,
    db_name: str = "workspace.db",
) -> SessionManager:
    return SessionManager(
        workspace_registry(),
        store=tmp_path / db_name,
        workspaces=manager,
        workspace_providers=[] if manager is not None else [provider or (fake or FakeProvider()).provider()],
        workspace_binding=binding,
    )


# --- local (non-isolated) workspace integration -----------------------------


async def test_local_managed_workspace_runs_and_reopens(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    db = tmp_path / "local.db"
    definition = workspace_definition()

    manager = SessionManager(
        workspace_registry(),
        store=db,
        workspace_providers=[local_workspace_provider(workspace_dir)],
        workspace_binding=WorkspaceBinding(provider="local", mode="persistent", name="demo"),
    )
    session = await manager.open_session(definition)
    session_id = session.session_id
    try:
        lease = session.workspace_lease
        assert lease is not None
        assert lease.workspace.ref == WorkspaceRef(provider="local", id=str(workspace_dir))
        result = await session.run("probe")
        assert result.output == "ws-done"
        assert result.usage.tool_calls == 1
        assert (workspace_dir / "probe.txt").read_text() == "workspace-hello"
        record = await manager.store.get_workspace_record(session_id)
        assert record is not None
        assert record.provider == "local"
        assert record.ref == WorkspaceRef(provider="local", id=str(workspace_dir))
        assert record.mode == "persistent"
        assert record.name == "demo"
    finally:
        await session.close()

    closed = await manager.store.get_workspace_record(session_id)
    assert closed is not None and closed.released_at is not None

    reopened_manager = SessionManager(
        workspace_registry(),
        store=db,
        workspace_providers=[local_workspace_provider(workspace_dir)],
        workspace_binding=WorkspaceBinding(provider="local", mode="persistent", name="demo"),
    )
    reopened_manager.register_definition(definition)
    session = await reopened_manager.open_saved_session("local", session_id)
    try:
        assert session.workspace_lease is not None
        assert session.workspace_lease.workspace.ref == WorkspaceRef(
            provider="local", id=str(workspace_dir)
        )
        result = await session.run("again")
        assert result.output == "ws-done"
        assert (workspace_dir / "probe.txt").read_text() == "workspace-hello"
    finally:
        await session.close()

    await reopened_manager.delete_session("local", session_id)
    assert await reopened_manager.store.get_workspace_record(session_id) is None


async def test_explicit_host_workspace_used_when_no_managed_binding(tmp_path):
    workspace_dir = tmp_path / "explicit"
    workspace_dir.mkdir()
    fake = FakeProvider()
    manager = make_workspace_manager(tmp_path, fake=fake, binding=None)
    definition = workspace_definition()
    async with manager.open_session(
        definition, resources={"workspace": str(workspace_dir)}
    ) as session:
        assert session.workspace_lease is None
        result = await session.run("probe")
        assert result.output == "ws-done"
        assert fake.open_calls == 0  # a configured manager does not force acquisition
        assert await manager.store.get_workspace_record(session.session_id) is None
    assert (workspace_dir / "probe.txt").read_text() == "workspace-hello"


async def test_no_automatic_local_fallback_when_managed_workspace_fails(tmp_path):
    workspace_dir = tmp_path / "fallback"
    workspace_dir.mkdir()
    fake = FakeProvider(open_error=True)
    manager = make_workspace_manager(
        tmp_path, fake=fake, binding=WorkspaceBinding(provider="fake")
    )
    definition = workspace_definition()
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(definition, resources={"workspace": str(workspace_dir)})
    assert excinfo.value.code == "WORKSPACE_UNAVAILABLE"
    assert fake.open_calls == 1
    # The explicit host path was never taught to the agent, and no orphan session exists.
    assert await manager.list_sessions("local") == []
    assert not (workspace_dir / "probe.txt").exists()


async def test_unregistered_provider_is_unavailable_not_local(tmp_path):
    workspace_dir = tmp_path / "fallback2"
    workspace_dir.mkdir()
    manager = make_workspace_manager(
        tmp_path, fake=FakeProvider(), binding=WorkspaceBinding(provider="not-registered")
    )
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(
            workspace_definition(), resources={"workspace": str(workspace_dir)}
        )
    assert excinfo.value.code == "WORKSPACE_UNAVAILABLE"
    assert await manager.list_sessions("local") == []


async def test_definition_without_workspace_requirement_does_not_acquire(tmp_path):
    fake = FakeProvider()
    manager = make_workspace_manager(
        tmp_path, fake=fake, binding=WorkspaceBinding(provider="fake")
    )
    async with manager.open_session(workspace_definition(requires_workspace=False)) as session:
        assert session.workspace_lease is None
    assert fake.open_calls == 0


# --- close / delete / reopen hooks -----------------------------------------


async def test_close_releases_handle_and_retains_identity(tmp_path):
    fake = FakeProvider()
    manager = make_workspace_manager(
        tmp_path, fake=fake, binding=WorkspaceBinding(provider="fake", mode="persistent", name="named")
    )
    session = await manager.open_session(workspace_definition())
    try:
        await session.workspace_lease.workspace.working_dir()  # lazily materialises the fake env
        ref = session.workspace_lease.workspace.ref
        assert ref is not None
    finally:
        await session.close()
    assert len(fake.release_calls) == 1
    record = await manager.store.get_workspace_record(session.session_id)
    assert record is not None
    assert record.ref == ref
    assert record.released_at is not None
    assert session.workspace_lease is None


async def test_delete_calls_provider_and_removes_workspace_identity(tmp_path):
    fake = FakeProvider()
    manager = make_workspace_manager(
        tmp_path, fake=fake, binding=WorkspaceBinding(provider="fake", mode="persistent", name="named")
    )
    session = await manager.open_session(workspace_definition())
    session_id = session.session_id
    await session.workspace_lease.workspace.working_dir()
    ref = session.workspace_lease.workspace.ref
    await session.close()
    assert await manager.store.get_workspace_record(session_id) is not None
    await manager.delete_session("local", session_id)
    assert fake.delete_calls == [ref]
    assert await manager.store.get_workspace_record(session_id) is None
    assert await manager.store.get_session(session_id) is None


async def test_reopen_restores_saved_provider_and_ref(tmp_path):
    fake = FakeProvider()
    db = tmp_path / "restore.db"
    definition = workspace_definition()
    manager = SessionManager(
        workspace_registry(),
        store=db,
        workspace_providers=[fake.provider()],
        workspace_binding=WorkspaceBinding(provider="fake", mode="persistent", name="named"),
    )
    session = await manager.open_session(definition)
    session_id = session.session_id
    await session.workspace_lease.workspace.working_dir()
    ref = session.workspace_lease.workspace.ref
    await session.close()

    reopened_manager = SessionManager(
        workspace_registry(),
        store=db,
        workspace_providers=[fake.provider()],
        # No binding supplied: the stored record selects the same provider/mode/name.
    )
    reopened_manager.register_definition(definition)
    reopened = await reopened_manager.open_saved_session("local", session_id)
    try:
        assert fake.restore_calls == [ref]
        assert reopened.workspace_lease is not None
        assert reopened.workspace_lease.workspace.ref == ref
    finally:
        await reopened.close()


async def test_reload_without_workspace_manager_refuses_substitute(tmp_path):
    fake = FakeProvider()
    db = tmp_path / "missing-manager.db"
    definition = workspace_definition()
    manager = SessionManager(
        workspace_registry(),
        store=db,
        workspace_providers=[fake.provider()],
        workspace_binding=WorkspaceBinding(provider="fake"),
    )
    session = await manager.open_session(definition)
    session_id = session.session_id
    await session.close()

    without_manager = SessionManager(workspace_registry(), store=db)
    without_manager.register_definition(definition)
    workspace_dir = tmp_path / "should-not-be-used"
    workspace_dir.mkdir()
    with pytest.raises(HarnessError) as excinfo:
        await without_manager.open_saved_session(
            "local", session_id, resources={"workspace": str(workspace_dir)}
        )
    assert excinfo.value.code == "WORKSPACE_UNAVAILABLE"
    assert excinfo.value.details["reason"] == "workspace_manager_missing"


async def test_missing_workspace_identity_is_not_recreated(tmp_path):
    fake = FakeProvider()
    db = tmp_path / "lost-identity.db"
    definition = workspace_definition()
    manager = SessionManager(
        workspace_registry(),
        store=db,
        workspace_providers=[fake.provider()],
        workspace_binding=WorkspaceBinding(provider="fake"),
    )
    session = await manager.open_session(definition)
    session_id = session.session_id
    await session.close()
    # Simulate an operator deleting only the workspace identity, not the session.
    await manager.store.delete_workspace_record(session_id)

    reopened_manager = SessionManager(
        workspace_registry(),
        store=db,
        workspace_providers=[fake.provider()],
        workspace_binding=WorkspaceBinding(provider="fake"),
    )
    reopened_manager.register_definition(definition)
    with pytest.raises(HarnessError) as excinfo:
        await reopened_manager.open_saved_session("local", session_id)
    assert excinfo.value.code == "WORKSPACE_UNAVAILABLE"
    assert excinfo.value.details["reason"] == "workspace_identity_missing"


async def test_delete_without_manager_refuses_to_forget_workspace(tmp_path):
    fake = FakeProvider()
    db = tmp_path / "delete-refuse.db"
    definition = workspace_definition()
    manager = SessionManager(
        workspace_registry(),
        store=db,
        workspace_providers=[fake.provider()],
        workspace_binding=WorkspaceBinding(provider="fake"),
    )
    session = await manager.open_session(definition)
    session_id = session.session_id
    await session.close()

    without_manager = SessionManager(workspace_registry(), store=db)
    with pytest.raises(HarnessError) as excinfo:
        await without_manager.delete_session("local", session_id)
    assert excinfo.value.code == "WORKSPACE_DELETE_UNSUPPORTED"
    assert await without_manager.store.get_session(session_id) is not None
    assert await without_manager.store.get_workspace_record(session_id) is not None


async def test_release_failure_keeps_session_open_for_retry(tmp_path):
    fake = FakeProvider()
    fake.release_failures = 1
    manager = make_workspace_manager(
        tmp_path, fake=fake, binding=WorkspaceBinding(provider="fake")
    )
    session = await manager.open_session(workspace_definition())
    with pytest.raises(HarnessError) as excinfo:
        await session.close()
    assert excinfo.value.code == "WORKSPACE_RELEASE_FAILED"
    assert not session.closed
    await session.close()  # retry succeeds; the workspace manager retained the handle
    assert session.closed
    assert len(fake.release_calls) == 2


async def test_prebuilt_workspace_manager_records_move_into_session_store(tmp_path):
    fake = FakeProvider()
    workspaces = WorkspaceManager(providers=[fake.provider()])  # empty in-memory record store
    manager = make_workspace_manager(
        tmp_path,
        manager=workspaces,
        binding=WorkspaceBinding(provider="fake"),
    )
    session = await manager.open_session(workspace_definition())
    session_id = session.session_id
    await session.close()
    record = await manager.store.get_workspace_record(session_id)
    assert record is not None and record.provider == "fake"


# --- store adapter ----------------------------------------------------------


async def test_store_workspace_record_roundtrip_and_callbacks(tmp_path):
    store = SessionStore(tmp_path / "records.db")
    await store.initialize()
    try:
        record = WorkspaceRecord(
            session_id="s-1",
            provider="fake",
            mode="persistent",
            name="named",
            created_at=1_000.0,
            last_used_at=1_000.0,
        )
        await store.save_workspace_record(record)
        loaded = await store.get_workspace_record("s-1")
        assert loaded == record
        assert [item.session_id for item in await store.list_workspace_records()] == ["s-1"]

        callbacks = store.workspace_record_callbacks()
        assert await callbacks.get("s-1") == record
        assert await callbacks.records() == [record]
        await callbacks.delete("s-1")
        assert await store.get_workspace_record("s-1") is None
    finally:
        await store.close()


async def test_corrupt_workspace_record_is_reported_not_ignored(tmp_path):
    path = tmp_path / "corrupt.db"
    store = SessionStore(path)
    await store.initialize()
    try:
        await store.save_workspace_record(
            WorkspaceRecord(
                session_id="s-1",
                provider="fake",
                created_at=1.0,
                last_used_at=1.0,
            )
        )
        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE workspace_records SET record = ? WHERE session_id = ?", ("{not json", "s-1")
        )
        connection.commit()
        connection.close()
        with pytest.raises(HarnessError) as excinfo:
            await store.get_workspace_record("s-1")
        assert excinfo.value.code == "WORKSPACE_RECORD_FAILED"
    finally:
        await store.close()
