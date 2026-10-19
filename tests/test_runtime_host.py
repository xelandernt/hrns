"""Host adapter contract: bundle shapes, manager calls, records, and ACP event mapping.

The ACP host is owned by another component; these tests pin the runtime-facing surface it
uses so the two cannot drift silently.
"""

from __future__ import annotations

import sys
import textwrap
import types

import pytest
from harness_protocol import BindingDescriptor, EnableChange, HarnessError
from harness_runtime import acp as acp_map
from harness_runtime.host import HostSetup, build_local_bundle
from harness_runtime.session import build_host_bundle
from pydantic_ai.models.test import TestModel
from test_runtime_support import (
    ECHO_PLUGIN,
    definition_with_echo,
    echo_descriptor,
)


def install_definition_module(name: str = "host_contract_agent") -> types.ModuleType:
    module = types.ModuleType(name)
    state: list[object] = []

    def factory(*, model=None, managed_capabilities=()):
        from pydantic_ai import Agent

        state.append(model)
        return Agent(
            TestModel(call_tools="all", custom_output_text="host-done"),
            name="host-contract-agent",
            capabilities=list(managed_capabilities),
        )

    from harness_runtime import DefinitionBinding

    module.state = state  # type: ignore[attr-defined]
    module.definition = definition_with_echo(
        factory=factory,
        bindings=[DefinitionBinding(binding_id="echo", plugin_id=ECHO_PLUGIN)],
    )
    sys.modules[name] = module
    return module


@pytest.fixture()
def host_bundle(tmp_path):
    install_definition_module()
    setup = HostSetup(
        definition="host_contract_agent:definition",
        store_path=str(tmp_path / "host.db"),
        model="test:initial-model",
    )
    bundle = build_local_bundle(setup)
    bundle.registry.register(echo_descriptor())
    try:
        yield bundle
    finally:
        sys.modules.pop("host_contract_agent", None)


async def test_bundle_shape_matches_host_contract(host_bundle):
    assert host_bundle.definition.agent_id == "test.agent"
    assert host_bundle.manager.store is not None
    assert host_bundle.store_path is not None
    descriptors = [info.plugin_id for info in host_bundle.registry.catalog()]
    assert ECHO_PLUGIN in descriptors


async def test_host_opening_records_cwd_and_resumes(host_bundle, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resources = {"workspace": str(workspace), "model": "test:initial-model"}
    session = await host_bundle.manager.open_session(
        host_bundle.definition,
        resources=resources,
        principal="local",
        deployment_id="local",
        initial_configuration=None,
    )
    try:
        result = await session.run("hello")
        assert result.output == "host-done"
        session_id = session.session_id
        summaries = await host_bundle.manager.list_sessions("local")
        summary = next(item for item in summaries if item.session_id == session_id)
        assert summary.cwd == str(workspace)  # host reloads this authoritative value
        assert summary.agent_id == host_bundle.definition.agent_id
        assert summary.deployment_id == "local"
        record = await host_bundle.manager.store.load_record(session_id)
        assert record is not None and record.transcript
    finally:
        await session.close()

    reopened = await host_bundle.manager.open_saved_session(
        "local", session_id, resources=resources, deployment_id="local"
    )
    try:
        assert len(await reopened.history()) >= 2
        again = await reopened.run("again")
        assert again.output == "host-done"
    finally:
        await reopened.close()

    await host_bundle.manager.delete_session("local", session_id)
    assert await host_bundle.manager.list_sessions("local") == []


async def test_host_ownership_checks_on_load_and_delete(host_bundle):
    session = await host_bundle.manager.open_session(
        host_bundle.definition,
        resources={"workspace": str(host_bundle.manager.store.path.parent)},
        principal="local",
        deployment_id="local",
    )
    session_id = session.session_id
    await session.close()
    with pytest.raises(HarnessError) as excinfo:
        await host_bundle.manager.open_saved_session("intruder", session_id)
    assert excinfo.value.code == "POLICY_DENIED"
    with pytest.raises(HarnessError) as excinfo:
        await host_bundle.manager.delete_session("intruder", session_id)
    assert excinfo.value.code == "POLICY_DENIED"


async def test_host_update_configuration_keyword_contract(host_bundle):
    session = await host_bundle.manager.open_session(
        host_bundle.definition,
        resources={"workspace": str(host_bundle.manager.store.path.parent)},
        principal="local",
        deployment_id="local",
    )
    try:
        # Current ACP adapter call shape.
        snapshot = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)],
            request_id="host-keyword-1",
            expected_revision=1,
        )
        assert snapshot.selected_revision == 2
        # Historical positional order still accepted for compatibility.
        retried = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], 2, "host-keyword-2"
        )
        assert retried.selected_revision == 2
        assert retried.bindings["echo"].enabled is False
    finally:
        await session.close()


async def test_host_descriptors_and_snapshot_formats(host_bundle):
    session = await host_bundle.manager.open_session(
        host_bundle.definition,
        resources={"workspace": str(host_bundle.manager.store.path.parent)},
        principal="local",
        deployment_id="local",
    )
    try:
        descriptors = session.catalog.descriptors(
            active_revision=session.snapshot.active_revision,
            selected_revision=session.snapshot.selected_revision,
        )
        assert len(descriptors) == 1
        assert isinstance(descriptors[0], BindingDescriptor)
        assert descriptors[0].binding_id == "echo"
        assert descriptors[0].change_mode == "next_turn"
        assert descriptors[0].policy.client_can_disable is True
        snapshot = session.snapshot
        assert snapshot.session_id == session.session_id
        payload = snapshot.to_wire()
        assert payload["sessionId"] == session.session_id
        assert payload["selectedRevision"] == 1
        assert payload["applicationStatus"] == "applied"
        page, cursor = await session.capabilities(limit=1)
        assert len(page) == 1 and cursor is None
    finally:
        await session.close()


async def test_per_run_model_option_reaches_factory(host_bundle):
    module = sys.modules["host_contract_agent"]
    state = module.state  # type: ignore[attr-defined]
    session = await host_bundle.manager.open_session(
        host_bundle.definition,
        resources={"workspace": str(host_bundle.manager.store.path.parent)},
        principal="local",
        deployment_id="local",
    )
    try:
        await session.run("first")
        selected = TestModel(call_tools="all", custom_output_text="host-done")
        await session.run("second", model=selected)
        assert state[-1] is selected  # the factory sees the turn's model selection
    finally:
        await session.close()


async def test_session_set_model_preference(host_bundle):
    module = sys.modules["host_contract_agent"]
    state = module.state  # type: ignore[attr-defined]
    session = await host_bundle.manager.open_session(
        host_bundle.definition,
        resources={"workspace": str(host_bundle.manager.store.path.parent)},
        principal="local",
        deployment_id="local",
    )
    try:
        selected = TestModel(call_tools="all", custom_output_text="host-done")
        await session.set_model(selected, expected_revision=1, request_id=None)
        assert session.snapshot.selected_revision == 1
        await session.run("after-selection")
        assert state[-1] is selected
    finally:
        await session.close()


def test_runtime_events_map_onto_acp_updates():
    presenter = acp_map.default_presenter()
    tool_call = {
        "tool_call_id": "call-1",
        "tool_name": "echo_tool",
        "args": {"text": "hello"},
        "kind": "function",
        "thought": False,
    }
    start = acp_map.event_to_update("tool_call", tool_call, cwd="/tmp", presenter=presenter)
    assert start is not None
    assert start.tool_call_id == "call-1"
    update = {
        "tool_call_id": "call-1",
        "tool_name": "echo_tool",
        "status": "completed",
        "outcome": "success",
        "content": "echo:hello",
        "output": "echo:hello",
    }
    progress = acp_map.event_to_update("tool_update", update, cwd="/tmp", presenter=presenter)
    assert progress is not None
    assert progress.status == "completed"
    assert progress.raw_output == "echo:hello"
    thought = acp_map.event_to_update(
        "text_delta",
        {"text": "hi", "kind": "thinking", "thought": True},
        cwd="/tmp",
        presenter=presenter,
    )
    assert thought is not None


async def test_catalog_backed_runtime_bundle(tmp_path):
    module = install_definition_module("host_catalog_agent")
    try:
        setup = HostSetup(
            definition="host_catalog_agent:definition",
            store_path=str(tmp_path / "catalog.db"),
        )
        bundle = build_host_bundle(setup)
        assert bundle.registry.lookup("harness.coder") is not None  # bundled catalog present
        assert bundle.manager.store is not None
        assert bundle.definition.agent_id == module.definition.agent_id
    finally:
        sys.modules.pop("host_catalog_agent", None)


async def test_manifest_definition_through_host_bundle(tmp_path):
    manifest_path = tmp_path / "agent.yaml"
    manifest_path.write_text(
        textwrap.dedent(
            """
            schema_version: 1
            agent_id: manifest.host
            name: Manifest host agent
            model: test:manifest
            spec:
              instructions: MANIFEST_SPEC
            """
        ),
        encoding="utf-8",
    )
    setup = HostSetup(definition=str(manifest_path), store_path=str(tmp_path / "manifest.db"))
    bundle = build_local_bundle(setup)
    assert bundle.definition.agent_id == "manifest.host"
    session = await bundle.manager.open_session(
        bundle.definition,
        resources={"model": TestModel(call_tools=[], custom_output_text="manifest-done")},
        principal="local",
        deployment_id="local",
    )
    try:
        result = await session.run("hello")
        assert result.output == "manifest-done"
    finally:
        await session.close()
