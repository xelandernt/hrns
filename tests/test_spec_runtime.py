"""Spec regressions for the runtime against CONCEPT §6-9, §13-14 and §19.

These tests cover confirmed mismatches that remained after the existing runtime
review suite. They exercise the published ``docs/RUNTIME_API.md`` contract through
the real Pydantic AI execution path and the real SQLite store.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import test_acp_host as acp_host
import test_runtime_support as support
import test_runtime_workspaces as workspace_support
from harness_protocol import (
    AddChange,
    CapabilityPolicy,
    ConfigureChange,
    EnableChange,
    HarnessError,
)
from harness_runtime import (
    AgentDefinition,
    DefinitionBinding,
    PluginDescriptor,
    PluginRegistry,
    SessionManager,
    SessionRecord,
    SessionStore,
    WorkspaceBinding,
)
from harness_runtime.host import definition_from_manifest
from harness_runtime.policy import configuration_payload_hash
from harness_runtime.registry import CompatibilityConstraints
from harness_runtime.workspaces import local_workspace_provider
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls, FunctionModel


async def close_quietly(session: Any) -> None:
    try:
        await session.close()
    except Exception:  # pragma: no cover - teardown best effort
        pass


def next_turn_policy(*, available: bool = True, editable_fields: set[str] | None = None) -> CapabilityPolicy:
    return CapabilityPolicy(
        available=available,
        enabled_by_default=True,
        client_can_enable=True,
        client_can_disable=True,
        editable_fields=editable_fields or set(),
        change_mode="next_turn",
    )


# --- atomic transactions / active snapshot immutability -----------------------


class NestedConfig(BaseModel):
    settings: dict[str, int] = {"level": 1}


def build_nested_capability(config: NestedConfig, binding: Any) -> Capability[Any]:
    return Capability(id=binding.binding_id, instructions=f"level={config.settings['level']}")


async def test_rejected_transaction_does_not_mutate_visible_selected_config(
    tmp_path: Path,
) -> None:
    """CONCEPT §8.2/§10.2: a rejected atomic transaction leaves selected state untouched."""
    descriptor = PluginDescriptor(
        plugin_id="test.nested",
        version="1.0.0",
        description="Nested configuration",
        config_model=NestedConfig,
        factory=build_nested_capability,
        change_mode="next_turn",
        default_policy=next_turn_policy(editable_fields={"settings.level"}),
    )
    manager = SessionManager(support.registry_with(descriptor), store=tmp_path / "nested.db")
    definition = AgentDefinition(
        agent_id="test.nested.agent",
        name="Nested agent",
        factory=support.make_factory(support.FactoryRecorder()),
        bindings=[
            DefinitionBinding(
                binding_id="nested",
                plugin_id="test.nested",
                config={"settings": {"level": 1}},
            )
        ],
    )
    session = await manager.open_session(definition)
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [
                    ConfigureChange(binding_id="nested", fields={"settings.level": 2}),
                    EnableChange(binding_id="missing", enabled=False),
                ],
                "nested-reject",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
        # The first change must not have leaked into the live selected value.
        assert session.snapshot.bindings["nested"].config == {"settings": {"level": 1}}
        assert session.catalog.offerings["nested"].config == {"settings": {"level": 1}}

        accepted = await session.update_configuration(
            [ConfigureChange(binding_id="nested", fields={"settings.level": 2})],
            "nested-accept",
            session.snapshot.selected_revision,
        )
        assert accepted.bindings["nested"].config == {"settings": {"level": 2}}
    finally:
        await close_quietly(session)
        await manager.close()


async def test_definition_policy_cannot_grant_host_availability(tmp_path: Path) -> None:
    """CONCEPT §6.3/§8.1: host policy constrains definition-level overrides."""
    descriptor = support.echo_descriptor(
        policy=CapabilityPolicy(
            available=False,
            enabled_by_default=False,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields={"marker"},
            change_mode="next_turn",
        )
    )
    manager = SessionManager(
        support.registry_with(descriptor), store=tmp_path / "policy-cap.db"
    )
    definition = support.definition_with_echo(
        bindings=[
            DefinitionBinding(
                binding_id="echo",
                plugin_id=support.ECHO_PLUGIN,
                enabled=False,
                policy=CapabilityPolicy(available=True),
            )
        ]
    )
    session = await manager.open_session(definition)
    try:
        assert session.catalog.offerings["echo"].available is False
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=True)],
                "grant-availability",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
    finally:
        await close_quietly(session)
        await manager.close()

    invalid = support.definition_with_echo(
        version="1.0.1",
        bindings=[
            DefinitionBinding(
                binding_id="echo",
                plugin_id=support.ECHO_PLUGIN,
                enabled=True,
                policy=CapabilityPolicy(available=True),
            )
        ],
    )
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(invalid)
    assert excinfo.value.code == "INVALID_CONFIGURATION"


# --- AddChange lifecycle, availability, dependencies --------------------------


async def test_add_rejects_plugin_marked_unavailable_by_host_policy(tmp_path: Path) -> None:
    """CONCEPT §8.1: unavailable bindings cannot be requested, including by `add`."""
    descriptor = support.echo_descriptor(
        policy=CapabilityPolicy(
            available=False,
            enabled_by_default=False,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields={"marker"},
            change_mode="next_turn",
        )
    )
    manager = SessionManager(
        support.registry_with(descriptor, allow_add=True), store=tmp_path / "unavailable-add.db"
    )
    definition = support.definition_with_echo(bindings=[])
    session = await manager.open_session(definition)
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [AddChange(binding_id="extra", plugin_id=support.ECHO_PLUGIN)],
                "add-unavailable",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
        assert "extra" not in session.snapshot.bindings
    finally:
        await close_quietly(session)
        await manager.close()

    # The same host policy applies to an initial selection before any session row exists.
    before = {item.session_id for item in await manager.list_sessions("local")}
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(
            definition,
            initial_configuration=[
                AddChange(binding_id="initial", plugin_id=support.ECHO_PLUGIN)
            ],
        )
    assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
    assert {item.session_id for item in await manager.list_sessions("local")} == before


async def test_add_rejects_missing_plugin_dependencies(tmp_path: Path) -> None:
    """CONCEPT §8.1: add validates host prerequisites before creating a binding."""
    descriptor = support.echo_descriptor()
    descriptor.compatibility = CompatibilityConstraints(
        requires_extras=["definitely_missing_runtime_spec_extra"]
    )
    manager = SessionManager(
        support.registry_with(descriptor, allow_add=True), store=tmp_path / "extras-add.db"
    )
    session = await manager.open_session(support.definition_with_echo(bindings=[]))
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [AddChange(binding_id="extra", plugin_id=support.ECHO_PLUGIN)],
                "add-extras",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
        assert "definitely_missing_runtime_spec_extra" in excinfo.value.message
        assert "extra" not in session.snapshot.bindings
    finally:
        await close_quietly(session)
        await manager.close()


@pytest.mark.parametrize(
    ("requirements", "conflicts"),
    [
        (["not-offered"], []),
        ([], ["not-offered"]),
    ],
)
async def test_add_rejects_unresolvable_dependency_references(
    tmp_path: Path, requirements: list[str], conflicts: list[str]
) -> None:
    """CONCEPT §7.3/§8.2: requirements and conflicts are resolved atomically."""
    plugin_id = "test.add.graph"
    descriptor = support.echo_descriptor(
        plugin_id=plugin_id, requirements=requirements, conflicts=conflicts
    )
    manager = SessionManager(
        support.registry_with(descriptor, allow_add=True), store=tmp_path / "graph-add.db"
    )
    session = await manager.open_session(support.definition_with_echo(bindings=[]))
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [AddChange(binding_id="extra", plugin_id=plugin_id)],
                "add-graph",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "INVALID_CONFIGURATION"
        assert "extra" not in session.snapshot.bindings
    finally:
        await close_quietly(session)
        await manager.close()


async def test_add_cannot_select_bundled_plugin_not_registered_for_local_creation(
    tmp_path: Path,
) -> None:
    """CONCEPT §7.2/§8.1: only the host's registered catalog is addable locally."""
    registry = PluginRegistry(allow_local_binding_creation=True)
    manager = SessionManager(registry, store=tmp_path / "bundled-add.db")
    session = await manager.open_session(support.definition_with_echo(bindings=[]))
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [AddChange(binding_id="planning", plugin_id="harness.planning")],
                "add-bundled",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
        assert "planning" not in session.snapshot.bindings
    finally:
        await close_quietly(session)
        await manager.close()


# --- policy reactivation on reload -------------------------------------------


async def test_reload_revalidates_saved_selection_against_current_policy(
    tmp_path: Path,
) -> None:
    """CONCEPT §8.5: a revoked binding must not stay active from a saved revision."""
    database = tmp_path / "revoked.db"
    definition = support.definition_with_echo(
        bindings=[DefinitionBinding(binding_id="echo", plugin_id=support.ECHO_PLUGIN, enabled=False)]
    )
    first = SessionManager(
        support.registry_with(support.echo_descriptor()), store=database, definitions=[definition]
    )
    session = await first.open_session(definition)
    session_id = session.session_id
    await session.update_configuration(
        [EnableChange(binding_id="echo", enabled=True)], "enable-before-revoke", 1
    )
    await session.close()
    await first.close()

    revoked = support.echo_descriptor(
        policy=CapabilityPolicy(
            available=False,
            enabled_by_default=False,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields={"marker"},
            change_mode="next_turn",
        )
    )
    second = SessionManager(
        support.registry_with(revoked), store=database, definitions=[definition]
    )
    try:
        with pytest.raises(HarnessError) as excinfo:
            await second.open_saved_session("local", session_id)
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
        assert excinfo.value.details["binding_id"] == "echo"
        assert excinfo.value.details["required_changes"] == [
            {"type": "enable", "bindingId": "echo", "enabled": False}
        ]
    finally:
        await second.close()


async def test_reload_accepts_saved_disabled_selection_when_defaults_were_revoked(
    tmp_path: Path,
) -> None:
    """CONCEPT §8.5: revalidation applies to the saved selection, not stale defaults."""
    database = tmp_path / "revoked-default.db"
    definition = support.definition_with_echo()
    first = SessionManager(
        support.registry_with(support.echo_descriptor()), store=database, definitions=[definition]
    )
    session = await first.open_session(definition)
    session_id = session.session_id
    await session.update_configuration(
        [EnableChange(binding_id="echo", enabled=False)], "disable-before-revoke", 1
    )
    await session.close()
    await first.close()

    revoked = support.echo_descriptor(
        policy=CapabilityPolicy(
            available=False,
            enabled_by_default=True,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields={"marker"},
            change_mode="next_turn",
        )
    )
    second = SessionManager(
        support.registry_with(revoked), store=database, definitions=[definition]
    )
    reopened = await second.open_saved_session("local", session_id)
    try:
        assert reopened.snapshot.bindings["echo"].enabled is False
        result = await reopened.run("still usable")
        assert result.output == "done"
        assert support.MARKER not in "\n".join(
            str(message.instructions)
            for message in result.new_messages()
            if getattr(message, "instructions", None)
        )
    finally:
        await close_quietly(reopened)
        await second.close()


# --- managed workspaces / no fallback ----------------------------------------


async def test_managed_workspace_overrides_prebound_host_workspace_path(
    tmp_path: Path,
) -> None:
    """CONCEPT §13.2/§13.3: the selected managed environment is the session workspace."""
    managed = tmp_path / "managed"
    managed.mkdir()
    host_path = tmp_path / "host"
    host_path.mkdir()
    manager = SessionManager(
        workspace_support.workspace_registry(),
        store=tmp_path / "managed.db",
        workspace_providers=[local_workspace_provider(managed)],
        workspace_binding=WorkspaceBinding(provider="local", mode="persistent", name="named"),
    )
    session = await manager.open_session(
        workspace_support.workspace_definition(),
        resources={"workspace.main": str(host_path)},
    )
    try:
        assert session.workspace_lease is not None
        result = await session.run("probe")
        assert result.output == "ws-done"
        assert (managed / "probe.txt").read_text() == "workspace-hello"
        assert not (host_path / "probe.txt").exists()
    finally:
        await close_quietly(session)
        await manager.close()


async def test_reopening_plain_workspace_never_creates_a_managed_environment(
    tmp_path: Path,
) -> None:
    """CONCEPT §11.5/§13.3: reopening preserves the recorded workspace identity."""
    database = tmp_path / "plain.db"
    host_path = tmp_path / "host"
    host_path.mkdir()
    managed = tmp_path / "managed-later"
    managed.mkdir()
    definition = workspace_support.workspace_definition()

    first = SessionManager(workspace_support.workspace_registry(), store=database)
    session = await first.open_session(
        definition, resources={"workspace.main": str(host_path)}
    )
    session_id = session.session_id
    await session.close()
    await first.close()

    # A host that later enables managed workspaces must not silently migrate an old
    # plain-workspace session to a brand-new environment.
    second = SessionManager(
        workspace_support.workspace_registry(),
        store=database,
        workspace_providers=[local_workspace_provider(managed)],
        workspace_binding=WorkspaceBinding(provider="local", mode="persistent", name="named"),
        definitions=[definition],
    )
    reopened = await second.open_saved_session(
        "local", session_id, resources={"workspace.main": str(host_path)}
    )
    try:
        assert reopened.workspace_lease is None
        assert await second.store.get_workspace_record(session_id) is None
        result = await reopened.run("probe")
        assert result.output == "ws-done"
        assert (host_path / "probe.txt").read_text() == "workspace-hello"
        assert not (managed / "probe.txt").exists()
    finally:
        await close_quietly(reopened)
        await second.close()


# --- definition pinning -------------------------------------------------------


async def test_definition_pin_does_not_change_after_registration(tmp_path: Path) -> None:
    """CONCEPT §6.3/§8.5: an opened session keeps its pinned definition revision."""
    recorder = support.FactoryRecorder()
    definition = support.definition_with_echo(recorder=recorder)
    manager = SessionManager(
        support.registry_with(support.echo_descriptor()),
        store=tmp_path / "pin.db",
        definitions=[definition],
    )
    session = await manager.open_session(definition)
    try:
        definition.factory = support.make_factory(
            support.FactoryRecorder(), text="mutated-after-open"
        )
        session.definition.factory = support.make_factory(
            support.FactoryRecorder(), text="mutated-through-session"
        )
        result = await session.run("pinned")
        assert result.output == "done"
        stored = manager.definition(definition.agent_id, definition.definition_version)
        assert stored is not None and stored.factory is not definition.factory
    finally:
        await close_quietly(session)
        await manager.close()


async def test_fixed_capabilities_apply_to_python_factory_definitions(
    tmp_path: Path,
) -> None:
    """CONCEPT §6.1: framework fixed contributions survive the Python factory path."""
    recorder = support.FactoryRecorder()
    definition = support.definition_with_echo(
        recorder=recorder,
        fixed_capabilities=[Capability(instructions="FIXED_MARKER")],
    )
    manager = SessionManager(
        support.registry_with(support.echo_descriptor()), store=tmp_path / "fixed.db"
    )
    session = await manager.open_session(definition)
    try:
        result = await session.run("fixed")
        instructions = "\n".join(
            str(message.instructions)
            for message in result.new_messages()
            if getattr(message, "instructions", None)
        )
        assert "FIXED_MARKER" in instructions
    finally:
        await close_quietly(session)
        await manager.close()


# --- composition identity and tool collisions --------------------------------


def shared_tool(text: str = "x") -> str:
    """A tool name shared by two capabilities in the collision probe."""
    return text


def build_shared_tool_capability(config: Any, binding: Any) -> Capability[Any]:
    return Capability(id=binding.binding_id, tools=[shared_tool])


async def test_duplicate_native_tool_names_reject_preparation(tmp_path: Path) -> None:
    """CONCEPT §7.3: tool identity collisions fail before a turn is accepted."""
    descriptors = [
        support.echo_descriptor(plugin_id="test.collision.a", factory=build_shared_tool_capability),
        support.echo_descriptor(plugin_id="test.collision.b", factory=build_shared_tool_capability),
    ]
    manager = SessionManager(
        support.registry_with(*descriptors), store=tmp_path / "tool-collision.db"
    )
    definition = support.definition_with_echo(
        bindings=[
            DefinitionBinding(binding_id="a", plugin_id="test.collision.a"),
            DefinitionBinding(binding_id="b", plugin_id="test.collision.b"),
        ]
    )
    session = await manager.open_session(definition)
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.run("collide")
        assert excinfo.value.code == "INVALID_CONFIGURATION"
        assert excinfo.value.details["tool_name"] == "shared_tool"
        assert await session._store.list_turns(session.session_id) == []
        assert session.snapshot.application_status == "failed"
    finally:
        await close_quietly(session)
        await manager.close()


async def test_fixed_and_managed_capability_identity_collision_rejects_preparation(
    tmp_path: Path,
) -> None:
    """CONCEPT §6.2/§7.3: fixed and managed contributions cannot share a native id."""
    def build_fixed_identity(config: Any, binding: Any) -> Capability[Any]:
        return Capability(id="shared.capability")

    manager = SessionManager(
        support.registry_with(support.echo_descriptor(factory=build_fixed_identity)),
        store=tmp_path / "identity-collision.db",
    )
    definition = support.definition_with_echo(fixed_capabilities=[Capability(id="shared.capability")])
    session = await manager.open_session(definition)
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.run("collide")
        assert excinfo.value.code == "INVALID_CONFIGURATION"
        assert excinfo.value.details["capability_id"] == "shared.capability"
        assert await session._store.list_turns(session.session_id) == []
    finally:
        await close_quietly(session)
        await manager.close()


# --- run-scoped child tasks ---------------------------------------------------


async def test_run_scoped_tasks_are_cancelled_but_registered_jobs_survive(
    tmp_path: Path,
) -> None:
    """CONCEPT §8.4: run children drain at turn exit; explicit jobs outlive the turn."""
    holder: dict[str, Any] = {}
    run_cancelled = asyncio.Event()
    job_cancelled = asyncio.Event()

    async def lingering_run_task() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            run_cancelled.set()
            raise

    async def job_task() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            job_cancelled.set()
            raise

    async def spawn_work() -> str:
        """Register one background job and leak one run-scoped child task."""
        holder["session"].register_job("echo", job_task(), description="survives")
        asyncio.create_task(lingering_run_task())
        return "spawned"

    def build(config: Any, binding: Any) -> Capability[Any]:
        return Capability(id=binding.binding_id, tools=[spawn_work])

    manager = SessionManager(
        support.registry_with(support.echo_descriptor(factory=build)),
        store=tmp_path / "run-scope.db",
    )
    definition = support.definition_with_echo(
        factory=support.make_factory(support.FactoryRecorder(), call_tools=["spawn_work"])
    )
    session = await manager.open_session(definition)
    holder["session"] = session
    try:
        result = await session.run("spawn")
        assert result.output == "done"
        await asyncio.sleep(0.05)
        assert run_cancelled.is_set()
        assert not job_cancelled.is_set()
        assert session.jobs.has_active("echo")
        await session.close()
        await asyncio.sleep(0.05)
        assert job_cancelled.is_set()
    finally:
        await close_quietly(session)
        await manager.close()


# --- transcript persistence ---------------------------------------------------


async def _stream_before_and_after_tool(messages: Any, info: Any):
    has_return = any(
        isinstance(part, ToolReturnPart)
        for message in messages
        for part in getattr(message, "parts", ())
    )
    if not has_return:
        yield "before"
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="echo_tool", json_args='{"text":"tool input"}', tool_call_id="tool-1"
                )
            }
        )
    else:
        yield "after"


async def test_transcript_keeps_assistant_text_after_tool_call_in_order(
    tmp_path: Path,
) -> None:
    """CONCEPT §14.1/§19(10): transcript replay preserves chronology around tools."""
    descriptor = support.echo_descriptor()

    def factory(*, managed_capabilities: Any = ()) -> Agent[Any, Any]:
        return Agent(
            FunctionModel(stream_function=_stream_before_and_after_tool),
            capabilities=list(managed_capabilities),
            output_type=str,
        )

    definition = support.definition_with_echo(factory=factory)
    manager = SessionManager(
        support.registry_with(descriptor), store=tmp_path / "transcript.db"
    )
    session = await manager.open_session(definition)
    try:
        result = await session.run("go")
        assert result.output == "after"
        transcript = await session.transcript()
        text_rows = [row for row in transcript if row["kind"] == "text_delta"]
        tool_rows = [row for row in transcript if row["kind"] == "tool_call"]
        assert tool_rows, transcript
        before = next(row for row in text_rows if row["data"]["text"] == "before")
        after = next(row for row in text_rows if row["data"]["text"] == "after")
        tool = tool_rows[0]
        assert before["seq"] < tool["seq"] < after["seq"], transcript
    finally:
        await close_quietly(session)
        await manager.close()


# --- configuration retry identity --------------------------------------------


async def test_failed_retry_marker_never_downgrades_committed_transaction(
    tmp_path: Path,
) -> None:
    """CONCEPT §10.3: a completed retry must keep returning the committed result."""
    store = SessionStore(tmp_path / "retry.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="s-retry",
                principal="local",
                deployment_id="local",
                agent_id="agent",
                definition_version="1.0.0",
            ),
            initial_bindings={},
            policy_revision=1,
        )
        await store.claim_transaction("s-retry", "request-1", "payload-hash")
        await store.commit_transaction(
            "s-retry",
            "request-1",
            bindings={"x": {"enabled": False, "config": {}}},
            policy_revision=1,
            snapshot_builder=lambda revision: {"selectedRevision": revision},
            create_revision=True,
        )
        # A late teardown/error path must not turn a committed request into a retryable
        # blank claim.
        await store.fail_transaction("s-retry", "request-1")
        claim = await store.claim_transaction("s-retry", "request-1", "payload-hash")
        assert claim.status == "committed"
        assert claim.revision == 2
        assert claim.snapshot == {"selectedRevision": 2}
    finally:
        await store.close()


async def test_restart_recovers_abandoned_in_progress_configuration_claim(
    tmp_path: Path,
) -> None:
    """CONCEPT §10.3/§14.3: a crashed claim is retryable after authoritative reload."""
    database = tmp_path / "abandoned-claim.db"
    definition = support.definition_with_echo()
    changes = [EnableChange(binding_id="echo", enabled=False)]
    first = SessionManager(
        support.registry_with(support.echo_descriptor()), store=database, definitions=[definition]
    )
    session = await first.open_session(definition)
    session_id = session.session_id
    # Simulate the crash point: the transaction was claimed but never committed.
    await session._store.claim_transaction(
        session_id,
        "abandoned-1",
        configuration_payload_hash(session_id, "abandoned-1", 1, changes),
    )
    await session.close()
    await first.close()

    second = SessionManager(
        support.registry_with(support.echo_descriptor()), store=database, definitions=[definition]
    )
    reopened = await second.open_saved_session("local", session_id)
    try:
        snapshot = await reopened.update_configuration(
            changes, "abandoned-1", reopened.snapshot.selected_revision
        )
        assert snapshot.selected_revision == 2
        assert snapshot.bindings["echo"].enabled is False
    finally:
        await close_quietly(reopened)
        await second.close()


# --- ownership and session listing -------------------------------------------


async def test_host_ownership_lookup_and_listing_are_not_truncated(tmp_path: Path) -> None:
    """CONCEPT §11.3/§14.1: ownership checks do not depend on a listing page."""
    agent, _client = await acp_host.make_host(tmp_path)
    definition = agent.definition
    for index in range(101):
        await agent.manager.store.create_session(
            SessionRecord(
                session_id=f"s_{index:03d}",
                principal=agent.policy.principal,
                deployment_id=agent.policy.deployment_id,
                agent_id=definition.agent_id,
                definition_version=definition.definition_version,
                title=f"Session {index}",
            ),
            initial_bindings={},
            policy_revision=1,
        )
    listed = {
        summary.session_id
        for summary in await agent.manager.list_sessions(agent.policy.principal)
    }
    assert len(listed) == 100
    excluded = next(
        f"s_{index:03d}" for index in range(101) if f"s_{index:03d}" not in listed
    )

    # session/list must paginate through all 101, not just the first store page.
    seen: list[str] = []
    cursor: str | None = None
    while True:
        response = await agent.list_sessions(cwd=None, cursor=cursor)
        seen.extend(session.session_id for session in response.sessions)
        cursor = response.next_cursor
        if cursor is None:
            break
    assert len(seen) == 101
    assert excluded in seen

    # load/delete check the authoritative ownership record, not a capped list.
    loaded = await agent.load_session(
        cwd=agent.policy.fallback_cwd(), session_id=excluded
    )
    assert loaded is not None
    await agent.delete_session(excluded)
    assert await agent.manager.store.get_session(excluded) is None


# --- manifest host conversion -------------------------------------------------


def test_host_manifest_conversion_preserves_binding_policy() -> None:
    """CONCEPT §6.2/§16.2: host-loaded manifests keep definition-level policy."""
    from harness_runtime import parse_manifest_data

    policy = CapabilityPolicy(
        available=True,
        enabled_by_default=False,
        client_can_enable=False,
        client_can_disable=True,
        editable_fields={"max_results"},
        change_mode="next_turn",
    )
    manifest = parse_manifest_data(
        {
            "schema_version": 1,
            "agent_id": "manifest.host",
            "name": "Manifest host",
            "bindings": [
                {
                    "binding_id": "search",
                    "plugin_id": "pydantic.web_search",
                    "enabled": False,
                    "policy": policy.model_dump(mode="json"),
                }
            ],
        }
    )
    definition = definition_from_manifest(manifest)
    assert definition.bindings[0].policy == policy
