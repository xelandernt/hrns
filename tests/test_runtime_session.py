"""Session execution, composition, policy transactions, streaming, and cancellation."""

from __future__ import annotations

import asyncio

import pytest
import test_runtime_support as support
from harness_protocol import (
    AddChange,
    CapabilityPolicy,
    ConfigureChange,
    EnableChange,
    HarnessError,
)
from harness_runtime import DefinitionBinding
from pydantic import BaseModel
from pydantic_ai import Agent, RunCancelled
from pydantic_ai.models.test import TestModel
from test_runtime_support import (
    ECHO_PLUGIN,
    MARKER,
    FactoryRecorder,
    build_echo_capability,
    build_failing_capability,
    build_slow_capability,
    definition_with_echo,
    echo_descriptor,
    make_factory,
    make_manager,
    run_level_factory,
)


class City(BaseModel):
    name: str
    country: str = "FR"


def instructions_of(result: object) -> str:
    """Instructions attached to this turn's requests only (history keeps older ones)."""
    messages = getattr(result, "new_messages")()
    return "\n".join(
        str(message.instructions)
        for message in messages
        if getattr(message, "instructions", None)
    )


def events_of(events: list[object]) -> list[tuple[str, dict[str, object]]]:
    return [(event.type, dict(event.data)) for event in events]  # type: ignore[attr-defined]


async def collect(stream: object) -> list[object]:
    return [event async for event in stream]  # type: ignore[attr-defined]


# --- native execution ---------------------------------------------------


async def test_run_returns_native_result_and_persists_history(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        first = await session.run("hello")
        assert first.output == "done"
        assert first.usage.requests >= 1
        history_after_first = await session.history()
        assert len(history_after_first) >= 3  # prompt, tool round trip, response
        second = await session.run("again")
        assert second.output == "done"
        assert len(await session.history()) > len(history_after_first)
        turns = await session._store.list_turns(session.session_id)
        assert [turn.status for turn in turns] == ["completed", "completed"]
        assert [turn.active_revision for turn in turns] == [1, 1]
        assert turns[0].usage["requests"] >= 1


async def test_capability_instructions_and_tools_are_native(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        result = await session.run("hello")
        assert MARKER in instructions_of(result)
        assert result.usage.tool_calls >= 1


async def test_structured_output_override_preserved(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        result = await session.run(
            "Paris?",
            output_type=City,
            model=TestModel(call_tools=["echo_tool"], custom_output_args={"name": "Paris"}),
        )
        assert isinstance(result.output, City)
        assert result.output.name == "Paris"
        assert result.output.country == "FR"


async def test_factory_declared_output_type_preserved(tmp_path):
    recorder = FactoryRecorder()
    factory = make_factory(
        recorder,
        output_type=City,
        call_tools=[],
    )
    manager = make_manager(tmp_path)
    definition = definition_with_echo(factory=factory)
    async with manager.open_session(definition) as session:
        result = await session.run(
            "Paris?",
            model=TestModel(call_tools=[], custom_output_args={"name": "Lyon"}),
        )
        assert isinstance(result.output, City)
        assert result.output.name == "Lyon"


async def test_async_factory_supported(tmp_path):
    recorder = FactoryRecorder()
    factory = make_factory(recorder, async_factory=True, call_tools=[])
    manager = make_manager(tmp_path)
    async with manager.open_session(definition_with_echo(factory=factory)) as session:
        result = await session.run("hello")
        assert result.output == "done"


async def test_fresh_composition_every_turn(tmp_path):
    recorder = FactoryRecorder()
    constructed: list[int] = []

    def factory(config, binding):
        capability = build_echo_capability(config, binding)
        constructed.append(id(capability))
        return capability

    descriptor = echo_descriptor(factory=factory)
    manager = make_manager(tmp_path, descriptor)
    definition = definition_with_echo(recorder=recorder, factory=make_factory(recorder))
    async with manager.open_session(definition) as session:
        await session.run("one")
        await session.run("two")
    assert len(set(constructed)) == 2  # a fresh capability per turn
    assert recorder.managed_per_call == [1, 1]
    assert recorder.agents[0] is not recorder.agents[1]


async def test_managed_binding_added_exactly_once_per_turn(tmp_path):
    calls: list[int] = []

    def factory(config, binding):
        calls.append(1)
        return build_echo_capability(config, binding)

    descriptor = echo_descriptor(factory=factory)
    manager = make_manager(tmp_path, descriptor)
    recorder = FactoryRecorder()
    definition = definition_with_echo(recorder=recorder, factory=make_factory(recorder))
    async with manager.open_session(definition) as session:
        result = await session.run("hello")
        assert result.usage.tool_calls == 1
        assert len(calls) == 1
        assert recorder.managed_per_call == [1]


async def test_factory_receiving_model_is_not_double_applied(tmp_path):
    received: list[object] = []

    def factory(*, model=None, managed_capabilities=()):
        received.append(model)
        return Agent(
            TestModel(call_tools=[], custom_output_text="alias-ok"),
            capabilities=list(managed_capabilities),
        )

    definition = definition_with_echo(factory=factory)
    manager = make_manager(tmp_path)
    async with manager.open_session(definition) as session:
        # A definition may treat the model selection as its own alias; the runtime must not
        # then pass the same value to Pydantic AI as a run override.
        result = await session.run("go", model="internal:alias")
        assert result.output == "alias-ok"
        assert received[-1] == "internal:alias"


async def test_run_level_factory_still_gets_native_capabilities(tmp_path):
    recorder = FactoryRecorder()
    definition = definition_with_echo(factory=run_level_factory(recorder))
    manager = make_manager(tmp_path)
    async with manager.open_session(definition) as session:
        result = await session.run("hello")
        assert recorder.managed_per_call == [0]  # factory took no managed argument
        assert MARKER in instructions_of(result)
        assert result.usage.tool_calls == 1


# --- next-turn composition ---------------------------------------------


async def test_disable_omits_instruction_and_tool_next_turn(tmp_path):
    factory_calls: list[int] = []

    def factory(config, binding):
        factory_calls.append(1)
        return build_echo_capability(config, binding)

    descriptor = echo_descriptor(factory=factory)
    manager = make_manager(tmp_path, descriptor)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        first = await session.run("on")
        assert MARKER in instructions_of(first)
        assert len(factory_calls) == 1
        snapshot = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)],
            "disable-1",
            session.snapshot.selected_revision,
        )
        assert snapshot.bindings["echo"].enabled is False
        second = await session.run("off")
        assert MARKER not in instructions_of(second)
        assert second.usage.tool_calls == 0
        assert len(factory_calls) == 1  # disabled binding never instantiated


async def test_enable_disabled_default_next_turn(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo(
        bindings=[DefinitionBinding(binding_id="echo", plugin_id=ECHO_PLUGIN, enabled=False)]
    )
    async with manager.open_session(definition) as session:
        first = await session.run("off")
        assert MARKER not in instructions_of(first)
    # Enabling applies before the first turn of a session; TestModel only calls tools
    # before a model response exists in history, so use a fresh session for that proof.
    session = await manager.open_session(definition)
    try:
        await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=True)],
            "enable-1",
            session.snapshot.selected_revision,
        )
        second = await session.run("on")
        assert MARKER in instructions_of(second)
        assert second.usage.tool_calls == 1
    finally:
        await session.close()


async def test_configuration_change_during_turn_is_pending_for_next_turn(tmp_path):
    support.slow_tool_started = asyncio.Event()
    descriptor = echo_descriptor(factory=build_slow_capability)
    manager = make_manager(tmp_path, descriptor)
    definition = definition_with_echo()
    session = await manager.open_session(definition)
    try:
        turn = asyncio.create_task(session.run("hold"))
        await asyncio.wait_for(support.slow_tool_started.wait(), 5)
        snapshot = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)],
            "mid-turn",
            session.snapshot.selected_revision,
        )
        assert snapshot.selected_revision == 2
        assert snapshot.active_revision == 1
        assert snapshot.application_status == "pending"
        assert snapshot.bindings["echo"].enabled is False
        live = session.snapshot
        assert live.application_status == "pending"
        descriptors, _ = await session.capabilities()
        echo = next(descriptor for descriptor in descriptors if descriptor.binding_id == "echo")
        assert echo.enabled is False
        assert echo.active_enabled is True
        await session.cancel()
        with pytest.raises(RunCancelled):
            await turn
        settled = session.snapshot
        assert settled.application_status == "applied"
        assert settled.active_revision == 2
        follow_up = await session.run("after")
        assert follow_up.usage.tool_calls == 0
    finally:
        await session.close()


async def test_cancellation_marks_turn_cancelled_and_session_reusable(tmp_path):
    support.slow_tool_started = asyncio.Event()
    manager = make_manager(tmp_path, echo_descriptor(factory=build_slow_capability))
    definition = definition_with_echo()
    session = await manager.open_session(definition)
    try:
        turn = asyncio.create_task(session.run("slow"))
        await asyncio.wait_for(support.slow_tool_started.wait(), 5)
        assert await session.cancel() is True
        with pytest.raises(RunCancelled):
            await turn
        turns = await session._store.list_turns(session.session_id)
        assert turns[-1].status == "cancelled"
        assert turns[-1].error["code"] == "TURN_CANCELLED"
        assert not session.closed
    finally:
        await session.close()


async def test_stream_events_cover_text_tools_usage_and_completion(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        events = await collect(session.stream("hello"))
        pairs = events_of(events)
        types = [event_type for event_type, _ in pairs]
        assert "text_delta" in types
        assert "tool_call" in types
        assert "tool_update" in types
        assert "usage" in types
        assert types[-1] == "turn_complete"
        assert types.count("turn_complete") == 1
        assert types.index("usage") < types.index("turn_complete")
        text = "".join(
            str(data["text"]) for event_type, data in pairs if event_type == "text_delta"
        )
        assert text
        tool_call = next(data for event_type, data in pairs if event_type == "tool_call")
        assert tool_call["tool_name"] == "echo_tool"
        assert isinstance(tool_call["args"], dict)
        tool_update = next(data for event_type, data in pairs if event_type == "tool_update")
        assert tool_update["status"] == "completed"
        assert tool_update["outcome"] == "success"
        assert tool_update["tool_call_id"] == tool_call["tool_call_id"]
        complete = pairs[-1][1]
        assert complete["output"] == "done"
        assert complete["output_type"].endswith("str")
        assert complete["active_revision"] == 1


async def test_deferred_approval_turn_and_resume(tmp_path):
    from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

    descriptor = echo_descriptor(factory=support.build_approval_capability)
    manager = make_manager(tmp_path, descriptor)
    definition = support.approval_definition()
    async with manager.open_session(definition) as stream_session:
        streamed = await collect(stream_session.stream("needs approval"))
        pairs = events_of(streamed)
        assert "deferred_tool_requests" in [event_type for event_type, _ in pairs]
        completion = pairs[-1][1]
        approvals = completion["deferred_tool_requests"]["approvals"]
        assert approvals and approvals[0]["tool_name"] == "approval_tool"
        pending = completion["deferred_tool_requests"]
        # The stream consumer can resume the deferred segment with explicit decisions.
        resumed_stream = await collect(
            stream_session.stream(
                None,
                deferred_tool_results=DeferredToolResults(
                    approvals={pending["approvals"][0]["tool_call_id"]: True}
                ),
            )
        )
        assert resumed_stream[-1].data["output"] == "approved-done"

    async with manager.open_session(definition) as session:
        result = await session.run("needs approval")
        assert isinstance(result.output, DeferredToolRequests)
        assert result.output.approvals
        results = result.output.build_results(approve_all=True)
        resumed = await session.run(None, deferred_tool_results=results)
        assert resumed.output == "approved-done"
        turns = await session._store.list_turns(session.session_id)
        assert [turn.status for turn in turns] == ["completed", "completed"]


async def test_stream_error_event_and_session_reusable(tmp_path):
    descriptor = echo_descriptor(factory=build_failing_capability)
    manager = make_manager(tmp_path, descriptor)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        snapshot = await session.update_configuration(
            [ConfigureChange(binding_id="echo", fields={"marker": "boom"})],
            "boom-1",
            session.snapshot.selected_revision,
        )
        assert snapshot.application_status == "applied"
        events = await collect(session.stream("hello"))
        pairs = events_of(events)
        assert [event_type for event_type, _ in pairs].count("error") == 1
        error = next(data for event_type, data in pairs if event_type == "error")
        assert error["code"] == "CAPABILITY_UNAVAILABLE"
        assert session.snapshot.application_status == "failed"
        assert session.snapshot.selected_revision == 2
        await session.update_configuration(
            [ConfigureChange(binding_id="echo", fields={"marker": MARKER})],
            "boom-2",
            session.snapshot.selected_revision,
        )
        result = await session.run("recovered")
        assert result.output == "done"
        assert session.snapshot.application_status == "applied"


# --- configuration transactions ----------------------------------------


async def test_configuration_revision_and_idempotent_retry(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        change = [EnableChange(binding_id="echo", enabled=False)]
        first = await session.update_configuration(change, "tx-1", 1)
        assert first.selected_revision == 2
        revisions_before = await session._store.latest_revision(session.session_id)
        retried = await session.update_configuration(change, "tx-1", None)
        assert retried.selected_revision == 2
        revisions_after = await session._store.latest_revision(session.session_id)
        assert revisions_before.revision == revisions_after.revision


async def test_reusing_request_id_with_different_payload_rejected(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], "tx-2", 1
        )
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=True)], "tx-2", None
            )
        assert excinfo.value.code == "INVALID_CONFIGURATION"


async def test_committed_retry_with_original_expected_revision_returns_result(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        change = [EnableChange(binding_id="echo", enabled=False)]
        first = await session.update_configuration(change, "retry-rev-1", 1)
        assert first.selected_revision == 2
        # The same request body and its original expected revision is a completed retry,
        # not a stale revision: it must return the committed snapshot.
        retried = await session.update_configuration(change, "retry-rev-1", 1)
        assert retried.selected_revision == 2
        assert retried.bindings["echo"].enabled is False


async def test_retry_with_different_expected_revision_is_a_different_payload(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        change = [EnableChange(binding_id="echo", enabled=False)]
        await session.update_configuration(change, "retry-expected-1", 1)
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(change, "retry-expected-1", 999)
        assert excinfo.value.code == "INVALID_CONFIGURATION"


async def test_stale_revision_conflict_reports_latest_snapshot(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], "rev-1", 1
        )
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=True)], "rev-2", 1
            )
        assert excinfo.value.code == "REVISION_CONFLICT"
        details = excinfo.value.details
        assert details["snapshot"]["selectedRevision"] == 2


async def test_idempotent_noop_does_not_bump_revision(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        snapshot = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=True)], "noop-1", 1
        )
        assert snapshot.selected_revision == 1
        assert snapshot.application_status == "applied"
        latest = await session._store.latest_revision(session.session_id)
        assert latest.revision == 1


async def test_directional_policy_denied(tmp_path):
    locked = echo_descriptor(
        policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_enable=False,
            client_can_disable=False,
            change_mode="next_turn",
        )
    )
    manager = make_manager(tmp_path, locked)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=False)], "locked-1", 1
            )
        assert excinfo.value.code == "POLICY_DENIED"
        # A request that already matches is an idempotent no-op, not a permission failure.
        noop = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=True)], "locked-2", 1
        )
        assert noop.selected_revision == 1


async def test_dependencies_require_atomic_enable(tmp_path):
    a = echo_descriptor(plugin_id="test.a")
    b = echo_descriptor(plugin_id="test.b", requirements=["a"])
    manager = make_manager(tmp_path, a, b)
    definition = definition_with_echo(
        bindings=[
            DefinitionBinding(binding_id="a", plugin_id="test.a", enabled=False),
            DefinitionBinding(binding_id="b", plugin_id="test.b", enabled=True),
        ]
    )
    # b enabled by default but a disabled by default is an invalid deployment.
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(definition)
    assert excinfo.value.code == "INVALID_CONFIGURATION"

    ok_definition = definition_with_echo(
        version="1.0.1",
        bindings=[
            DefinitionBinding(binding_id="a", plugin_id="test.a", enabled=False),
            DefinitionBinding(binding_id="b", plugin_id="test.b", enabled=False),
        ],
    )
    async with manager.open_session(ok_definition) as session:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="b", enabled=True)], "dep-1", 1
            )
        assert excinfo.value.code == "DEPENDENCY_CONFLICT"
        assert excinfo.value.details["missing"] == ["a"]

        both = await session.update_configuration(
            [
                EnableChange(binding_id="a", enabled=True),
                EnableChange(binding_id="b", enabled=True),
            ],
            "dep-2",
            1,
        )
        assert both.bindings["a"].enabled and both.bindings["b"].enabled

        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="a", enabled=False)], "dep-3", 2
            )
        assert excinfo.value.code == "DEPENDENCY_CONFLICT"


async def test_conflicting_bindings_rejected(tmp_path):
    a = echo_descriptor(plugin_id="test.a")
    b = echo_descriptor(plugin_id="test.b", conflicts=["a"])
    manager = make_manager(tmp_path, a, b)
    definition = definition_with_echo(
        bindings=[
            DefinitionBinding(binding_id="a", plugin_id="test.a", enabled=True),
            DefinitionBinding(binding_id="b", plugin_id="test.b", enabled=True),
        ]
    )
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(definition)
    assert excinfo.value.code == "INVALID_CONFIGURATION"

    safe_definition = definition_with_echo(
        version="1.0.1",
        bindings=[
            DefinitionBinding(binding_id="a", plugin_id="test.a", enabled=True),
            DefinitionBinding(binding_id="b", plugin_id="test.b", enabled=False),
        ],
    )
    async with manager.open_session(safe_definition) as session:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="b", enabled=True)], "conflict-1", 1
            )
        assert excinfo.value.code == "DEPENDENCY_CONFLICT"


async def test_new_session_binding_rejected_after_open_but_allowed_initially(tmp_path):
    descriptor = echo_descriptor(change_mode="new_session")
    manager = make_manager(tmp_path, descriptor)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=False)], "new-session-1", 1
            )
        assert excinfo.value.code == "NEW_SESSION_REQUIRED"

    initial = await manager.open_session(
        definition,
        initial_configuration=[EnableChange(binding_id="echo", enabled=False)],
    )
    async with initial as session:
        assert session.snapshot.bindings["echo"].enabled is False
        assert session.snapshot.selected_revision == 1


async def test_unavailable_binding_cannot_enable(tmp_path):
    descriptor = echo_descriptor(
        policy=CapabilityPolicy(
            available=False,
            enabled_by_default=False,
            client_can_enable=True,
            client_can_disable=True,
            change_mode="next_turn",
        )
    )
    manager = make_manager(tmp_path, descriptor)
    definition = definition_with_echo(
        bindings=[DefinitionBinding(binding_id="echo", plugin_id=ECHO_PLUGIN, enabled=False)]
    )
    async with manager.open_session(definition) as session:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=True)], "unavailable-1", 1
            )
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"

    invalid = definition_with_echo(
        bindings=[DefinitionBinding(binding_id="echo", plugin_id=ECHO_PLUGIN, enabled=True)]
    )
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(invalid)
    assert excinfo.value.code == "INVALID_CONFIGURATION"


async def test_add_binding_requires_host_permission_and_works_when_allowed(tmp_path):
    descriptor = echo_descriptor()
    locked_manager = make_manager(tmp_path, descriptor, allow_add=False, db_name="locked.db")
    definition = definition_with_echo(bindings=[])
    async with locked_manager.open_session(definition) as session:
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [
                    AddChange(
                        binding_id="extra",
                        plugin_id=ECHO_PLUGIN,
                        config={"marker": "ADDED"},
                    )
                ],
                "add-1",
                1,
            )
        assert excinfo.value.code == "POLICY_DENIED"

    manager = make_manager(tmp_path, descriptor, allow_add=True, db_name="open.db")
    async with manager.open_session(definition) as session:
        snapshot = await session.update_configuration(
            [AddChange(binding_id="extra", plugin_id=ECHO_PLUGIN, config={"marker": "ADDED"})],
            "add-2",
            1,
        )
        assert snapshot.bindings["extra"].enabled is True
        descriptors, _ = await session.capabilities()
        added = next(descriptor for descriptor in descriptors if descriptor.binding_id == "extra")
        assert added.policy.client_can_disable is True
        assert added.change_mode == "next_turn"
        result = await session.run("added")
        assert "ADDED" in instructions_of(result)
        assert result.usage.tool_calls == 1
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [AddChange(binding_id="extra", plugin_id=ECHO_PLUGIN)], "add-3", 2
            )
        assert excinfo.value.code == "INVALID_CONFIGURATION"


async def test_new_session_plugin_can_only_be_added_at_creation(tmp_path):
    descriptor = echo_descriptor(change_mode="new_session")
    manager = make_manager(tmp_path, descriptor, allow_add=True, db_name="new-session-add.db")
    definition = definition_with_echo(bindings=[])
    session = await manager.open_session(
        definition,
        initial_configuration=[
            AddChange(binding_id="later", plugin_id=ECHO_PLUGIN, config={"marker": "INITIAL"})
        ],
    )
    try:
        assert session.snapshot.bindings["later"].enabled is True
        descriptors, _ = await session.capabilities()
        added = next(item for item in descriptors if item.binding_id == "later")
        assert added.change_mode == "new_session"
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [AddChange(binding_id="other", plugin_id=ECHO_PLUGIN)],
                "late-add",
                session.snapshot.selected_revision,
            )
        assert excinfo.value.code == "NEW_SESSION_REQUIRED"
    finally:
        await session.close()
        await manager.close()


async def test_configure_editable_field_and_schema_validation(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        snapshot = await session.update_configuration(
            [ConfigureChange(binding_id="echo", fields={"marker": "CHANGED"})],
            "conf-1",
            1,
        )
        assert snapshot.bindings["echo"].config["marker"] == "CHANGED"
        result = await session.run("changed")
        assert "CHANGED" in instructions_of(result)

        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [ConfigureChange(binding_id="echo", fields={"unknown": 1})], "conf-2", 2
            )
        assert excinfo.value.code == "POLICY_DENIED"
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [ConfigureChange(binding_id="echo", fields={"limit": "not-an-int"})],
                "conf-3",
                2,
            )
        assert excinfo.value.code == "INVALID_CONFIGURATION"
        assert excinfo.value.details["errors"]
        # A rejected transaction is atomic: the selected revision is unchanged.
        assert session.snapshot.selected_revision == 2
        assert session.snapshot.bindings["echo"].config["marker"] == "CHANGED"


async def test_capabilities_listing_pagination_and_codes(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        items, cursor = await session.capabilities(limit=1)
        assert len(items) == 1 and cursor is None
        with pytest.raises(HarnessError) as excinfo:
            await session.capabilities(limit=500)
        assert excinfo.value.code == "INVALID_CONFIGURATION"


async def test_concurrent_turn_rejected(tmp_path):
    support.slow_tool_started = asyncio.Event()
    manager = make_manager(tmp_path, echo_descriptor(factory=build_slow_capability))
    definition = definition_with_echo()
    session = await manager.open_session(definition)
    try:
        turn = asyncio.create_task(session.run("first"))
        await asyncio.wait_for(support.slow_tool_started.wait(), 5)
        with pytest.raises(HarnessError) as excinfo:
            await session.run("second")
        assert excinfo.value.code == "SESSION_BUSY"
        await session.cancel()
        with pytest.raises(RunCancelled):
            await turn
    finally:
        await session.close()


async def test_closed_session_rejects_turns(tmp_path):
    manager = make_manager(tmp_path)
    session = await manager.open_session(definition_with_echo())
    await session.close()
    assert session.closed
    with pytest.raises(HarnessError) as excinfo:
        await session.run("hello")
    assert excinfo.value.code == "SESSION_CLOSED"
    with pytest.raises(HarnessError) as excinfo:
        await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], "closed-1", 1
        )
    assert excinfo.value.code == "SESSION_CLOSED"


async def test_initial_configuration_validation_before_creation(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(
            definition,
            initial_configuration=[EnableChange(binding_id="missing", enabled=False)],
        )
    assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
    assert await manager.list_sessions("local") == []
