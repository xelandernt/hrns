"""Starter definitions, manifest parity, and the deterministic offline demo.

The demo is the acceptance test for the vertical slice: a real native Pydantic AI agent,
managed native capability instructions, real configuration transactions, and visibly
different model input after toggling — all without credentials or network access.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from harness_protocol import EnableChange, HarnessError
from harness_runtime import catalog, examples, starters
from harness_runtime.definitions import AgentDefinition
from harness_runtime.resources import ResourceSet
from harness_runtime.session import SessionManager
from pydantic_ai.capabilities import Capability
from pydantic_ai.workspaces import LocalWorkspaceBackend

REPO_ROOT = Path(__file__).resolve().parents[1]


def _manager(tmp_path, definition: AgentDefinition) -> SessionManager:
    return SessionManager(
        examples.demo_registry(),
        store=str(tmp_path / "sessions.db"),
        definitions=[definition],
    )


def test_developer_starter_requires_an_explicit_workspace() -> None:
    definition = starters.developer_definition()
    requirements = {requirement.name: requirement for requirement in definition.resources}
    assert requirements["workspace"].required is True
    assert requirements["workspace"].kind == "workspace"
    assert requirements["model.default"].required is True
    workspace_binding = definition.binding("workspace")
    assert workspace_binding is not None
    assert workspace_binding.policy is not None
    # Mandatory: visible in the catalog, not client-toggleable.
    assert workspace_binding.policy.client_can_enable is False
    assert workspace_binding.policy.client_can_disable is False
    assert workspace_binding.enabled is True


@pytest.mark.asyncio
async def test_developer_starter_runs_offline_in_a_workspace(tmp_path) -> None:
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")

    def script(_messages, _info):
        return ModelResponse(parts=[TextPart("workspace listed")])

    async def stream(messages, _info):
        if len(messages) == 1:
            yield {
                0: DeltaToolCall(
                    name="list_directory",
                    json_args='{"path": "."}',
                    tool_call_id="call-list",
                )
            }
        else:
            yield "workspace listed"

    model = FunctionModel(script, stream_function=stream, model_name="developer-demo")
    definition = starters.developer_definition()
    manager = _manager(tmp_path, definition)
    resources = ResourceSet(
        {"model.default": model, "workspace": LocalWorkspaceBackend(str(workspace))}
    )
    async with manager.open_session(definition, resources=resources) as session:
        result = await session.run("List the files in the workspace.")
        assert str(result.output) == "workspace listed"
        enabled = {bid for bid, state in session.snapshot.bindings.items() if state.enabled}
        assert {"workspace", "filesystem", "shell", "planning", "repo_context"} <= enabled
        assert "memory" not in enabled
        assert "subagents" not in enabled
        # Native tool activity is streamed and persisted with the turn.
        transcript = await session.transcript()
        assert any(
            event.get("kind") == "tool_call"
            and event.get("data", {}).get("tool_name") == "list_directory"
            for event in transcript
            if isinstance(event, dict)
        )
        assert any(
            event.get("kind") == "capability"
            and "directory_listed"
            in str(event.get("data", {}).get("event", {}).get("kind", ""))
            for event in transcript
            if isinstance(event, dict)
        )


@pytest.mark.asyncio
async def test_developer_starter_without_workspace_fails_closed(tmp_path) -> None:
    definition = starters.developer_definition()
    manager = _manager(tmp_path, definition)
    with pytest.raises(HarnessError) as caught:
        await manager.open_session(
            definition, resources=ResourceSet({"model.default": "test"})
        )
    assert caught.value.code == "WORKSPACE_UNAVAILABLE"
    assert "workspace" in caught.value.message


@pytest.mark.asyncio
async def test_research_starter_runs_without_a_workspace(tmp_path) -> None:
    definition = starters.research_definition(web=False)
    assert definition.resource_requirement("workspace") is None
    manager = _manager(tmp_path, definition)
    async with manager.open_session(
        definition, resources=ResourceSet({"model.default": "test"})
    ) as session:
        result = await session.run("Compare two approaches and keep a plan.")
        assert str(result.output)
        enabled = {bid for bid, state in session.snapshot.bindings.items() if state.enabled}
        assert "planning" in enabled
        assert "search" not in enabled  # web=False keeps the offline definition runnable
        assert "memory" not in enabled


def test_starter_binding_ids_are_stable() -> None:
    assert tuple(b.binding_id for b in starters.developer_bindings()) == (
        starters.DEVELOPER_BINDING_IDS
    )
    assert tuple(b.binding_id for b in starters.research_bindings()) == (
        starters.RESEARCH_BINDING_IDS
    )
    assert tuple(b.binding_id for b in starters.research_bindings(web=False)) == (
        starters.RESEARCH_BINDING_IDS
    )


def test_starter_manifests_match_builders() -> None:
    for name, builder in (
        ("developer", starters.developer_definition),
        ("research", starters.research_definition),
    ):
        path = starters.starter_manifest_path(name)
        loaded = starters.load_starter_manifest(path, starter=name)
        built = builder()
        assert loaded.agent_id == built.agent_id
        assert loaded.definition_version == built.definition_version
        loaded_bindings = [(b.binding_id, b.plugin_id, b.enabled) for b in loaded.bindings]
        built_bindings = [(b.binding_id, b.plugin_id, b.enabled) for b in built.bindings]
        assert loaded_bindings == built_bindings
        # Configurations validate against the same real schemas and compare equal.
        for loaded_binding, built_binding in zip(loaded.bindings, built.bindings, strict=True):
            model = catalog.entry(loaded_binding.plugin_id).config_model
            if model is not None:
                assert model.model_validate(loaded_binding.config).model_dump() == (
                    model.model_validate(built_binding.config).model_dump()
                )
        assert [(r.name, r.required) for r in loaded.resources] == [
            (r.name, r.required) for r in built.resources
        ]


def test_research_manifest_defaults_web_on() -> None:
    definition = starters.load_starter_manifest(
        starters.starter_manifest_path("research"), starter="research"
    )
    by_id = {binding.binding_id: binding for binding in definition.bindings}
    assert by_id["search"].enabled is True
    assert by_id["fetch"].enabled is True
    assert by_id["memory"].enabled is False
    assert by_id["mcp"].enabled is False
    assert by_id["context_tool_output_limits"].config["store"] == "local_file"


def test_manifest_rejects_unknown_or_invalid_bindings() -> None:
    manifest = starters.starter_manifest("research")
    data = manifest.model_dump(mode="json", exclude_none=True)
    data["bindings"][0]["plugin_id"] = "example.does_not_exist"
    with pytest.raises(HarnessError) as caught:
        starters.definition_from_manifest(starters.DefinitionManifest.model_validate(data))
    assert caught.value.code == "CAPABILITY_UNAVAILABLE"
    assert "does_not_exist" in caught.value.message or "not bundled" in caught.value.message

    data = starters.starter_manifest("research").model_dump(mode="json", exclude_none=True)
    data["bindings"][0]["config"] = {"max_uses": -1}
    with pytest.raises(HarnessError) as caught:
        starters.definition_from_manifest(starters.DefinitionManifest.model_validate(data))
    assert caught.value.code == "INVALID_CONFIGURATION"


def test_capability_creation_is_disabled_in_starters() -> None:
    creation = catalog.entry("harness.capability_creation")
    assert creation.disabled_in_starters is True
    for definition in (starters.developer_definition(), starters.research_definition()):
        assert all(binding.plugin_id != creation.plugin_id for binding in definition.bindings)


def test_starter_manifest_paths_are_editable_yaml() -> None:
    for name in ("developer", "research"):
        path = starters.starter_manifest_path(name)
        assert path.suffix == ".yaml"
        text = path.read_text(encoding="utf-8")
        assert "schema_version: 1" in text
        assert "plugin_id:" in text
        assert "Runtime Capability Creation" in text or "capability_creation" not in text


def test_demo_definition_is_importable_and_native() -> None:
    definition = examples.demo_definition()
    assert isinstance(definition, AgentDefinition)
    assert definition.agent_id == examples.DEMO_AGENT_ID
    assert definition.factory is examples.build_demo_agent
    assert definition.resources == []
    binding = definition.binding(examples.DEMO_BINDING_ID)
    assert binding is not None
    assert binding.plugin_id == examples.DEMO_PLUGIN_ID
    assert binding.enabled is True
    assert binding.policy is not None and binding.policy.change_mode == "next_turn"
    assert examples.demo_definition_factory().agent_id == examples.DEMO_AGENT_ID


def test_demo_factory_builds_fresh_agents_and_instructions_are_visible() -> None:
    first = examples.build_demo_agent(
        managed_capabilities=(Capability(instructions="[[tone=concise]]"),)
    )
    second = examples.build_demo_agent(
        managed_capabilities=(Capability(instructions="[[tone=enthusiastic]]"),)
    )
    assert first is not second
    assert "tone=concise" in str(first.run_sync("hello").output)
    assert "tone=enthusiastic" in str(second.run_sync("hello").output)
    assert "tone=none" in str(examples.build_demo_agent().run_sync("hello").output)


def test_demo_plugin_descriptor_validates_configuration() -> None:
    descriptor = examples.demo_plugin_descriptor()
    assert descriptor.plugin_id == examples.DEMO_PLUGIN_ID
    assert descriptor.config_model is examples.ToneConfig
    assert descriptor.config_schema()["additionalProperties"] is False
    with pytest.raises(Exception):
        examples.ToneConfig(style="sarcastic")
    registry = examples.demo_registry()
    assert registry.lookup(examples.DEMO_PLUGIN_ID) is not None
    assert registry.lookup("harness.filesystem") is not None


@pytest.mark.asyncio
async def test_demo_toggles_through_real_configuration_transactions(tmp_path) -> None:
    result = await examples.run_demo(store=str(tmp_path / "demo.db"))
    assert [turn["tone"] for turn in result["turns"]] == ["concise", "none", "concise"]
    assert result["toggled"] is True
    assert result["selected_revision"] == 3
    assert result["turns"][0]["output"] == "offline demo: tone=concise"
    assert result["turns"][1]["output"] == "offline demo: tone=none"


@pytest.mark.asyncio
async def test_demo_toggle_applies_to_the_next_turn_only(tmp_path) -> None:
    """A turn started under one revision never sees a change made afterwards."""
    definition = examples.demo_definition()
    manager = _manager(tmp_path, definition)
    async with manager.open_session(definition) as session:
        first = await session.run("hello")
        assert "tone=concise" in str(first.output)
        disabled = await session.update_configuration(
            [EnableChange(binding_id=examples.DEMO_BINDING_ID, enabled=False)],
            request_id="off",
            expected_revision=session.snapshot.selected_revision,
        )
        assert disabled.bindings[examples.DEMO_BINDING_ID].enabled is False
        second = await session.run("hello")
        assert "tone=none" in str(second.output)
        # Idempotent re-disable is a no-op with no new revision.
        again = await session.update_configuration(
            [EnableChange(binding_id=examples.DEMO_BINDING_ID, enabled=False)],
            request_id="off-again",
            expected_revision=disabled.selected_revision,
        )
        assert again.selected_revision == disabled.selected_revision


def test_demo_runs_as_a_subprocess_module(tmp_path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness_runtime.examples",
            "--store",
            str(tmp_path / "cli.db"),
            "--json",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYDANTIC_AI_NO_BANNER": "1"},
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["toggled"] is True
    assert payload["transport"] == "python-session"


def test_host_definition_loader_resolves_the_demo() -> None:
    from harness_runtime.host import load_definition

    definition = load_definition("harness_runtime.examples:demo_definition")
    assert isinstance(definition, AgentDefinition)
    assert definition.agent_id == examples.DEMO_AGENT_ID


def test_host_definition_loader_resolves_starter_manifests() -> None:
    from harness_runtime.host import load_definition

    definition = load_definition(str(starters.starter_manifest_path("research")))
    assert definition.agent_id == starters.RESEARCH_AGENT_ID
    assert all(
        binding.plugin_id in catalog.CATALOG_PLUGIN_IDS
        for binding in definition.bindings
    )


@pytest.mark.asyncio
async def test_demo_definition_runs_and_toggles_through_the_host_bundle(tmp_path) -> None:
    """The host wiring path (import reference -> bundle -> session) serves the same demo."""
    from harness_runtime.session import build_host_bundle

    class Setup:
        definition = "harness_runtime.examples:demo_definition"
        hosted = False
        store_path = str(tmp_path / "host.db")

    bundle = build_host_bundle(Setup())
    assert bundle.definition.agent_id == examples.DEMO_AGENT_ID
    async with bundle.manager.open_session(
        bundle.definition, principal="host-test", deployment_id="local"
    ) as session:
        first = await session.run("hello")
        assert "tone=concise" in str(first.output)
        await session.update_configuration(
            [EnableChange(binding_id=examples.DEMO_BINDING_ID, enabled=False)],
            request_id="host-disable-tone",
            expected_revision=session.snapshot.selected_revision,
        )
        second = await session.run("hello again")
        assert "tone=none" in str(second.output)
