"""Adversarial runtime review probes (read-only contract checks).

Tests in this file exercise `harness-runtime` against the published contract
(``docs/RUNTIME_API.md``) and CONCEPT §6-8/13-14. Findings verified against the code of
2026-10-18 08:05:

- F1 (critical): initial configuration bypasses client_can_enable/client_can_disable.
- F3 (critical): close() during the composition window never requests cancellation of the
  interactive turn; the turn runs to completion after the controlling close.
- F4 (critical): ``AddChange`` accepts a plugin whose descriptor reports runtime
  incompatibility and composes it into the turn.
- F49 (required): ``register_definition`` silently replaces a duplicate
  ``(agent_id, definition_version)`` instead of rejecting it.
- Fnan (required): NaN in ``ConfigureChange.fields`` escapes as a raw ``ValueError``
  instead of a documented ``HarnessError``.

Tests named with the finding id assert the contract and therefore fail against the
current implementation; the remaining tests document adjacent behaviors verified green.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import test_runtime_support as support
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
    SessionManager,
)
from harness_runtime.registry import CompatibilityConstraints
from harness_runtime.store import SessionStore
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

# --- helpers ------------------------------------------------------------------


def review_definition(
    bindings: list[DefinitionBinding],
    *,
    recorder: support.FactoryRecorder | None = None,
    factory: Any | None = None,
    agent_id: str = "review.agent",
    version: str = "1.0.0",
    resources_policy: CapabilityPolicy | None = None,
) -> AgentDefinition:
    recorder = recorder or support.FactoryRecorder()
    return AgentDefinition(
        agent_id=agent_id,
        name="Review agent",
        definition_version=version,
        factory=factory or support.make_factory(recorder),
        bindings=bindings,
    )


async def close_quietly(session: Any) -> None:
    try:
        await session.close()
    except Exception:  # pragma: no cover - teardown best effort
        pass


async def turn_status(store: SessionStore, session_id: str) -> list[str]:
    connection = sqlite3.connect(str(store.path))
    try:
        rows = connection.execute(
            "SELECT status FROM turns WHERE session_id = ? ORDER BY turn_index", (session_id,)
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        connection.close()


def gated_factory(gate: asyncio.Event, recorder: support.FactoryRecorder) -> Any:
    async def build_agent(*, managed_capabilities: Any = ()) -> Agent[Any, Any]:
        recorder.managed_per_call.append(len(managed_capabilities))
        await gate.wait()
        return Agent(
            TestModel(call_tools="all", custom_output_text="done"),
            output_type=str,
            capabilities=list(managed_capabilities),
            name="review-gated-agent",
        )

    return build_agent


def unavailable_echo_descriptor() -> PluginDescriptor:
    """An offered plugin whose host policy is available but which is runtime-incompatible."""
    return PluginDescriptor(
        plugin_id=support.ECHO_PLUGIN,
        version="1.0.0",
        description="Echo",
        config_model=support.EchoConfig,
        factory=support.build_echo_capability,
        change_mode="next_turn",
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields=set(),
            change_mode="next_turn",
        ),
        compatibility=CompatibilityConstraints(
            runtime_compatible=False,
            constraint="not supported on this host",
        ),
    )


# --- F1: initial configuration must respect directional policy (CONCEPT §8.1/§8.2,
#     RUNTIME_API §1 "same change shapes and policy rules") -----------------------


async def test_f1_initial_configuration_respects_client_can_enable(tmp_path: Path) -> None:
    policy = CapabilityPolicy(
        available=True,
        enabled_by_default=True,
        client_can_enable=False,
        client_can_disable=False,
        editable_fields=set(),
        change_mode="next_turn",
    )
    manager = SessionManager(
        support.registry_with(support.echo_descriptor(policy=policy)),
        store=tmp_path / "f1.db",
    )
    definition = review_definition(
        [DefinitionBinding(binding_id="echo", plugin_id=support.ECHO_PLUGIN, enabled=False)]
    )
    try:
        session = await manager.open_session(
            definition, initial_configuration=[EnableChange(binding_id="echo", enabled=True)]
        )
        await close_quietly(session)
        pytest.fail("initial configuration enabled a client_can_enable=False binding")
    except HarnessError as exc:
        assert exc.code == "POLICY_DENIED"


async def test_f1_initial_configuration_respects_client_can_disable(tmp_path: Path) -> None:
    policy = CapabilityPolicy(
        available=True,
        enabled_by_default=True,
        client_can_enable=False,
        client_can_disable=False,
        editable_fields=set(),
        change_mode="next_turn",
    )
    manager = SessionManager(
        support.registry_with(support.echo_descriptor(policy=policy)),
        store=tmp_path / "f1b.db",
    )
    definition = review_definition(
        [DefinitionBinding(binding_id="echo", plugin_id=support.ECHO_PLUGIN, enabled=True)]
    )
    try:
        session = await manager.open_session(
            definition, initial_configuration=[EnableChange(binding_id="echo", enabled=False)]
        )
        await close_quietly(session)
        pytest.fail("initial configuration disabled a client_can_disable=False binding")
    except HarnessError as exc:
        assert exc.code == "POLICY_DENIED"


async def test_policy_still_allows_new_session_choice_at_creation(tmp_path: Path) -> None:
    # The intended lifecycle exception: `new_session` settings may be *chosen* at creation.
    descriptor = support.echo_descriptor(
        policy=CapabilityPolicy(
            available=True,
            enabled_by_default=False,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields=set(),
            change_mode="new_session",
        ),
        change_mode="new_session",
    )
    manager = SessionManager(support.registry_with(descriptor), store=tmp_path / "ns.db")
    definition = review_definition(
        [DefinitionBinding(binding_id="echo", plugin_id=support.ECHO_PLUGIN, enabled=False)]
    )
    session = await manager.open_session(
        definition, initial_configuration=[EnableChange(binding_id="echo", enabled=True)]
    )
    try:
        assert session.snapshot.bindings["echo"].enabled is True
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=False)],
                "later-1",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "NEW_SESSION_REQUIRED"
    finally:
        await close_quietly(session)
        await manager.close()


# --- F3: close() during the composition window must cancel the interactive turn
#     (CONCEPT §14.2/§14.4, RUNTIME_API §2 `cancel_running`) ----------------------


async def _close_during_composition(tmp_path: Path, db_name: str) -> list[str]:
    gate = asyncio.Event()
    recorder = support.FactoryRecorder()
    definition = review_definition(
        [DefinitionBinding(binding_id="echo", plugin_id=support.ECHO_PLUGIN, enabled=True)],
        factory=gated_factory(gate, recorder),
    )
    store = SessionStore(tmp_path / db_name)
    manager = SessionManager(support.registry_with(support.echo_descriptor()), store=store)
    session = await manager.open_session(definition)
    try:
        run_task = asyncio.create_task(session.run("go"))
        await asyncio.sleep(0.3)  # composition parked inside the factory gate
        closer = asyncio.create_task(session.close())
        await asyncio.sleep(0.1)  # close() parks (no cancellation has been requested)
        gate.set()
        try:
            await asyncio.wait_for(run_task, timeout=10)
        except BaseException:
            pass  # a cancelled turn may raise RunCancelled/CancelledError
        await asyncio.wait_for(closer, timeout=10)
        return await turn_status(store, session.session_id)
    finally:
        await manager.close()


async def test_f3_close_during_composition_cancels_run_turn(tmp_path: Path) -> None:
    statuses = await _close_during_composition(tmp_path, "f3-run.db")
    # The turn must not have completed after the controlling close(); it was cancelled.
    assert statuses and statuses[-1] != "completed", statuses


async def test_f3_close_during_composition_cancels_stream_turn(tmp_path: Path) -> None:
    gate = asyncio.Event()
    recorder = support.FactoryRecorder()
    definition = review_definition(
        [DefinitionBinding(binding_id="echo", plugin_id=support.ECHO_PLUGIN, enabled=True)],
        agent_id="review.stream",
        factory=gated_factory(gate, recorder),
    )
    store = SessionStore(tmp_path / "f3-stream.db")
    manager = SessionManager(support.registry_with(support.echo_descriptor()), store=store)
    session = await manager.open_session(definition)
    try:
        events: list[str] = []

        async def consume() -> None:
            async for event in session.stream("go"):
                events.append(event.type)

        stream_task = asyncio.create_task(consume())
        await asyncio.sleep(0.3)
        closer = asyncio.create_task(session.close())
        await asyncio.sleep(0.1)
        gate.set()
        try:
            await asyncio.wait_for(stream_task, timeout=10)
        except BaseException:
            pass
        await asyncio.wait_for(closer, timeout=10)
        statuses = await turn_status(store, session.session_id)
        assert statuses and statuses[-1] != "completed", statuses
    finally:
        await manager.close()


# --- F4: AddChange must not admit unavailable plugins (CONCEPT §8.1 "Unavailable
#     bindings cannot be enabled"; §7.2 host authorization) -----------------------


async def test_f4_add_change_rejects_unavailable_plugin(tmp_path: Path) -> None:
    manager = SessionManager(
        support.registry_with(unavailable_echo_descriptor(), allow_add=True),
        store=tmp_path / "f4.db",
    )
    definition = review_definition(
        [DefinitionBinding(binding_id="def", plugin_id=support.ECHO_PLUGIN, enabled=False)]
    )
    session = await manager.open_session(definition)
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [AddChange(binding_id="extra", plugin_id=support.ECHO_PLUGIN, enabled=True)],
                "add-1",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
        assert "extra" not in session.snapshot.bindings
    finally:
        await close_quietly(session)
        await manager.close()


async def test_enable_of_offered_unavailable_binding_is_rejected(tmp_path: Path) -> None:
    # Green adjacency: the enable path gates availability; only `add` bypasses it today.
    manager = SessionManager(
        support.registry_with(unavailable_echo_descriptor(), allow_add=False),
        store=tmp_path / "f4-enable.db",
    )
    definition = review_definition(
        [DefinitionBinding(binding_id="def", plugin_id=support.ECHO_PLUGIN, enabled=False)]
    )
    session = await manager.open_session(definition)
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="def", enabled=True)],
                "enable-1",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
    finally:
        await close_quietly(session)
        await manager.close()


# --- F49: duplicate (agent_id, definition_version) registration must be rejected
#     (RUNTIME_API §1 "duplicate (agent_id, version) rejected") --------------------


def test_f49_duplicate_definition_registration_is_rejected(tmp_path: Path) -> None:
    manager = SessionManager(support.registry_with(support.echo_descriptor()), store=tmp_path / "f49.db")
    definition = review_definition(
        [DefinitionBinding(binding_id="echo", plugin_id=support.ECHO_PLUGIN, enabled=True)]
    )
    manager.register_definition(definition)
    replacement = review_definition([], agent_id=definition.agent_id, version=definition.definition_version)
    try:
        manager.register_definition(replacement)
        assert manager.definition(definition.agent_id, definition.definition_version) is definition, (
            "a duplicate (agent_id, definition_version) silently replaced the pinned definition"
        )
    except HarnessError:
        pass  # documented behavior: duplicates are rejected


# --- Fnan: invalid numeric values surface as documented HarnessError codes ---------


async def test_f5_nan_configuration_field_is_reported_as_harness_error(tmp_path: Path) -> None:
    manager = SessionManager(support.registry_with(support.echo_descriptor()), store=tmp_path / "nan.db")
    session = await manager.open_session(support.definition_with_echo())
    try:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [ConfigureChange(binding_id="echo", fields={"limit": float("nan")})],
                "nan-1",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code in {"INVALID_CONFIGURATION", "RESOURCE_UNAVAILABLE"}
    finally:
        await close_quietly(session)
        await manager.close()


# --- Green guards: subtle behaviors verified green on the current implementation ---


async def test_committed_retry_returns_snapshot_even_with_stale_expected_revision(
    tmp_path: Path,
) -> None:
    """CONCEPT §10.3/RUNTIME_API §3: retry identity is consulted before the revision check."""
    manager = SessionManager(support.registry_with(support.echo_descriptor()), store=tmp_path / "retry.db")
    session = await manager.open_session(support.definition_with_echo())
    try:
        first = await session.update_configuration(
            [ConfigureChange(binding_id="echo", fields={"limit": 7})], "cfg-1", 1
        )
        committed_revision = first.selected_revision
        # The replay still carries the *original* expected revision; the committed result wins.
        replay = await session.update_configuration(
            [ConfigureChange(binding_id="echo", fields={"limit": 7})], "cfg-1", 1
        )
        assert replay.selected_revision == committed_revision
        # A different body under the same request id is rejected as replay corruption.
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [ConfigureChange(binding_id="echo", fields={"limit": 8})], "cfg-1", 1
            )
        assert excinfo.value.code == "INVALID_CONFIGURATION"
    finally:
        await close_quietly(session)
        await manager.close()


async def test_idempotent_retry_after_restart_returns_committed_snapshot(tmp_path: Path) -> None:
    from harness_runtime import SessionManager as SM

    definition = review_definition(
        [DefinitionBinding(binding_id="echo", plugin_id=support.ECHO_PLUGIN)],
        agent_id="retry.agent",
    )
    store = SessionStore(tmp_path / "restart.db")
    manager = SM(support.registry_with(support.echo_descriptor()), store=store, definitions=[definition])
    session = await manager.open_session(definition)
    committed = await session.update_configuration(
        [EnableChange(binding_id="echo", enabled=False)],
        "tx-9",
        session.snapshot.selected_revision,
    )
    revision = committed.selected_revision
    # Simulate a host crash without closing the session (control row is stale, plugin state
    # and retry records persist).
    import sqlite3

    connection = sqlite3.connect(str(store.path))
    with connection:
        connection.execute("DELETE FROM session_control WHERE session_id = ?", (session.session_id,))
    connection.close()

    manager2 = SM(
        support.registry_with(support.echo_descriptor()),
        store=SessionStore(tmp_path / "restart.db"),
        definitions=[definition],
    )
    try:
        session2 = await manager2.open_saved_session("local", session.session_id)
        try:
            assert session2.snapshot.bindings["echo"].enabled is False
            replay = await session2.update_configuration(
                [EnableChange(binding_id="echo", enabled=False)], "tx-9", 1
            )
            assert replay.selected_revision == revision
        finally:
            await close_quietly(session2)
    finally:
        await manager.close()
        await manager2.close()


async def test_capability_busy_preserves_selected_state(tmp_path: Path) -> None:
    """CONCEPT §8.4/RUNTIME_API §2.3: unconfirmed cancellation blocks the toggle."""
    manager = SessionManager(support.registry_with(support.echo_descriptor()), store=tmp_path / "jobs.db")
    session = await manager.open_session(support.definition_with_echo())
    try:
        revision = session.snapshot.selected_revision

        async def refusing_cancel() -> None:
            raise RuntimeError("cannot confirm cancellation")

        job = session.register_external_job("echo", cancel=refusing_cancel)
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=False)], "jobs-1", revision
            )
        assert excinfo.value.code == "CAPABILITY_BUSY"
        assert session.snapshot.bindings["echo"].enabled is True
        assert session.snapshot.selected_revision == revision
        session.jobs.complete(job)
        result = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)],
            "jobs-2",
            session.snapshot.selected_revision,
        )
        assert result.bindings["echo"].enabled is False
    finally:
        await close_quietly(session)
        await manager.close()
