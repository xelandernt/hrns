import pytest
from harness_protocol import EnableChange, HarnessError
from harness_runtime.examples import demo_definition, demo_registry
from harness_runtime.session import SessionManager


async def _session(tmp_path):
    definition = demo_definition()
    manager = SessionManager(
        demo_registry(include_catalog=False),
        store=tmp_path / "state.sqlite3",
        definitions=[definition],
        allow_local_binding_creation=False,
    )
    return manager, await manager.open_session(definition, principal="alice", deployment_id="local")


async def test_state_survives_disable_and_reenable(tmp_path):
    manager, session = await _session(tmp_path)
    try:
        await session.set_binding_state("tone", {"cursor": 3, "notes": ["a"]}, schema_version=1)
        assert await session.get_binding_state("tone", schema_version=1) == {
            "cursor": 3, "notes": ["a"],
        }
        await session.update_configuration(
            [EnableChange(binding_id="tone", enabled=False)],
            request_id="disable-state",
            expected_revision=session.snapshot.selected_revision,
        )
        assert session.snapshot.bindings["tone"].enabled is False
        assert (await session.get_binding_state("tone"))["cursor"] == 3
        await session.update_configuration(
            [EnableChange(binding_id="tone", enabled=True)],
            request_id="enable-state",
            expected_revision=session.snapshot.selected_revision,
        )
        assert (await session.get_binding_state("tone"))["notes"] == ["a"]
    finally:
        await session.close()
        await manager.close()


async def test_schema_change_is_rejected_and_clear_is_explicit(tmp_path):
    manager, session = await _session(tmp_path)
    try:
        await session.set_binding_state("tone", {"value": 1}, schema_version=1)
        with pytest.raises(HarnessError) as error:
            await session.set_binding_state("tone", {"value": 2}, schema_version=2)
        assert error.value.code == "INVALID_CONFIGURATION"
        with pytest.raises(HarnessError):
            await session.get_binding_state("tone", schema_version=2)

        await session.clear_binding_state("tone")
        assert await session.get_binding_state("tone") is None
        with pytest.raises(HarnessError) as unknown:
            await session.get_binding_state("not-offered")
        assert unknown.value.code == "CAPABILITY_UNAVAILABLE"
    finally:
        await session.close()
        await manager.close()


async def test_state_requires_json_and_is_removed_on_session_deletion(tmp_path):
    manager, session = await _session(tmp_path)
    session_id = session.session_id
    try:
        with pytest.raises(HarnessError) as error:
            await session.set_binding_state("tone", {"bad": object()})
        assert error.value.code == "INVALID_CONFIGURATION"
        await session.set_binding_state("tone", {"safe": True})
    finally:
        await session.close()

    await manager.delete_session("alice", session_id)
    assert await manager.store.get_plugin_state(session_id, "tone") is None
    await manager.close()
