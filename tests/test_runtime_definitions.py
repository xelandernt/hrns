"""Definitions, manifests, plugin discovery, resources, and policy catalog ordering."""

from __future__ import annotations

import importlib.metadata
import sys
import textwrap

import pytest
import yaml
from harness_protocol import CapabilityPolicy, EnableChange, HarnessError
from harness_runtime import (
    AgentDefinition,
    BindingContext,
    CompatibilityConstraints,
    DefinitionBinding,
    PluginDescriptor,
    PluginRegistry,
    ResourceRequirement,
    ResourceSet,
    manifest_from_file,
    parse_manifest_data,
    parse_manifest_text,
)
from harness_runtime.resources import resolve_workspace_value
from pydantic import ValidationError
from pydantic_ai.capabilities import Capability
from pydantic_ai.models.test import TestModel
from pydantic_ai.workspaces import LocalWorkspaceBackend
from test_runtime_support import (
    ECHO_PLUGIN,
    MARKER,
    definition_with_echo,
    echo_descriptor,
    make_manager,
    registry_with,
)


def binding_context(binding_id: str = "lazy") -> BindingContext:
    return BindingContext(
        binding_id=binding_id,
        plugin_id="test.lazy",
        session_id="s",
        principal="local",
        deployment_id="local",
        binding_config={},
        resources=ResourceSet(),
    )


# --- registry -----------------------------------------------------------


def test_duplicate_plugin_ids_are_configuration_errors():
    registry = registry_with(echo_descriptor())
    duplicate = echo_descriptor()
    duplicate.version = "2.0.0"  # type: ignore[misc]  # pydantic assignment allowed
    with pytest.raises(HarnessError) as excinfo:
        registry.register(duplicate)
    assert excinfo.value.code == "INVALID_CONFIGURATION"


def test_config_model_schema_and_defaults():
    descriptor = echo_descriptor()
    schema = descriptor.config_schema()
    assert set(schema["properties"]) == {"marker", "limit"}
    assert descriptor.config_defaults() == {"marker": MARKER, "limit": 3}
    info = descriptor.to_info()
    assert info.managed is True
    assert info.presentation == "test.echo"


def test_missing_plugin_is_capability_unavailable():
    registry = PluginRegistry()
    with pytest.raises(HarnessError) as excinfo:
        registry.require("missing.plugin")
    assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"


def test_import_reference_is_lazy_and_reports_failures(tmp_path):
    module_path = tmp_path / "lazy_plugin_fixture.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from pydantic_ai.capabilities import Capability

            def make(config, binding):
                return Capability(id=binding.binding_id, instructions='LAZY_ACTIVE')
            """
        ),
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        sys.modules.pop("lazy_plugin_fixture", None)
        registry = PluginRegistry()
        registry.register_import("test.lazy", "lazy_plugin_fixture:make")
        assert "lazy_plugin_fixture" not in sys.modules
        descriptor = registry.require("test.lazy")
        capability = registry.instantiate(descriptor, {}, binding_context())
        assert "lazy_plugin_fixture" in sys.modules
        assert isinstance(capability, Capability)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("lazy_plugin_fixture", None)


class _FakeEntryPoints(list):
    def select(self, *, group: str):
        return type(self)(entry for entry in self if entry.group == group)


def test_entry_point_discovery_is_metadata_only(tmp_path, monkeypatch):
    module_path = tmp_path / "entrypoint_plugin_fixture.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from pydantic_ai.capabilities import Capability
            def make(config, binding):
                return Capability(id=binding.binding_id, instructions='ENTRY_ACTIVE')
            """
        ),
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        sys.modules.pop("entrypoint_plugin_fixture", None)
        entry_point = importlib.metadata.EntryPoint(
            name="test.entrypoint",
            value="entrypoint_plugin_fixture:make",
            group="harness.capabilities",
        )
        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda: _FakeEntryPoints([entry_point]),
        )
        registry = PluginRegistry()
        assert registry.discover() == 1
        descriptor = registry.require("test.entrypoint")
        assert descriptor.factory == "entrypoint_plugin_fixture:make"
        assert "entrypoint_plugin_fixture" not in sys.modules
        registry.instantiate(descriptor, {}, binding_context())
        assert "entrypoint_plugin_fixture" in sys.modules
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("entrypoint_plugin_fixture", None)


def test_discovery_does_not_import_duplicate_metadata(monkeypatch):
    entry_point = importlib.metadata.EntryPoint(
        name="test.entrypoint",
        value="nowhere:make",
        group="harness.capabilities",
    )
    fake = _FakeEntryPoints([entry_point])
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda: fake)
    registry = PluginRegistry()
    assert registry.discover() == 1
    assert registry.discover() == 0  # existing ids are not re-registered


def test_bad_import_reference_surfaces_capability_unavailable():
    registry = PluginRegistry()
    registry.register_import("test.broken", "nonexistent_module_xyz:factory")
    with pytest.raises(HarnessError) as excinfo:
        registry.instantiate(
            registry.require("test.broken"), {}, binding_context("broken")
        )
    assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"


def test_compatibility_constraint_missing_extra():
    descriptor = PluginDescriptor(
        plugin_id="test.extra",
        version="1.0.0",
        factory=lambda config, ctx: Capability(id="x"),
        compatibility=CompatibilityConstraints(requires_extras=["definitely_missing_extra_xyz"]),
    )
    registry = PluginRegistry()
    registry.register(descriptor)
    with pytest.raises(HarnessError) as excinfo:
        registry.instantiate(descriptor, {}, binding_context("extra"))
    assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
    assert "definitely_missing_extra_xyz" in excinfo.value.message


def test_registry_catalog_is_stable_order():
    registry = registry_with(
        echo_descriptor(plugin_id="test.b"), echo_descriptor(plugin_id="test.a")
    )
    catalog = [info.plugin_id for info in registry.catalog()]
    assert catalog == ["test.b", "test.a"]  # explicit registration order, no bundled noise


def test_bundled_catalog_resolves_without_explicit_registration(tmp_path):
    registry = PluginRegistry()  # no explicit registrations
    descriptor = registry.require("harness.planning")
    assert descriptor.plugin_id == "harness.planning"
    context = BindingContext(
        binding_id="planning",
        plugin_id="harness.planning",
        session_id="s",
        principal="local",
        deployment_id="local",
        binding_config={},
        resources=ResourceSet(),
    )
    capability = registry.instantiate(descriptor, {}, context)
    from pydantic_ai.capabilities import AbstractCapability

    assert isinstance(capability, AbstractCapability)


def test_bundled_catalog_skipped_by_entry_point_discovery(monkeypatch):
    entry_point = importlib.metadata.EntryPoint(
        name="harness.planning",  # same id as a bundled catalog entry
        value="nowhere:make",
        group="harness.capabilities",
    )
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda: _FakeEntryPoints([entry_point])
    )
    registry = PluginRegistry()
    assert registry.discover() == 0
    assert registry.require("harness.planning").plugin_id == "harness.planning"


# --- manifests ----------------------------------------------------------


def manifest_payload() -> dict:
    return {
        "schema_version": 1,
        "agent_id": "manifest.agent",
        "name": "Manifest agent",
        "description": "Declarative",
        "definition_version": "2.1.0",
        "model": "test:test",
        "spec": {"instructions": "SPEC_MAGIC", "name": "spec-agent"},
        "bindings": [
            {
                "binding_id": "echo",
                "plugin_id": ECHO_PLUGIN,
                "config": {"marker": "MANIFEST_MARKER"},
                "enabled": True,
            }
        ],
        "resources": [
            {"name": "model.default", "kind": "model", "required": True},
            {"name": "workspace.main", "kind": "workspace", "required": False},
        ],
        "interaction_features": ["agent_plan"],
    }


def test_manifest_yaml_and_json_parse_equally(tmp_path):
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(yaml.safe_dump(manifest_payload()), encoding="utf-8")
    json_path = tmp_path / "agent.json"
    json_path.write_text(
        parse_manifest_text(
            yaml.safe_dump(manifest_payload()), fmt="yaml"
        ).model_dump_json(),
        encoding="utf-8",
    )
    from_yaml = manifest_from_file(yaml_path)
    from_json = manifest_from_file(json_path)
    assert from_yaml.agent_id == from_json.agent_id == "manifest.agent"
    assert from_yaml.bindings[0].config == {"marker": "MANIFEST_MARKER"}
    definition = from_yaml.to_definition()
    assert definition.definition_version == "2.1.0"
    assert definition.binding("echo") is not None
    assert definition.resource_requirement("workspace.main") is not None
    assert definition.spec["instructions"] == "SPEC_MAGIC"


def test_invalid_manifest_rejected_with_actionable_error():
    payload = manifest_payload()
    payload["bindings"][0]["plugin_id"] = ""  # violates min_length
    with pytest.raises(HarnessError) as excinfo:
        parse_manifest_data(payload)
    assert excinfo.value.code == "INVALID_CONFIGURATION"
    assert "bindings.0.plugin_id" in excinfo.value.message
    assert excinfo.value.details["errors"]


def test_invalid_agent_spec_inside_manifest():
    payload = manifest_payload()
    payload["spec"] = {"instructions": {"not": "a string or list"}}
    with pytest.raises(HarnessError) as excinfo:
        parse_manifest_data(payload)
    assert excinfo.value.code == "INVALID_CONFIGURATION"


def test_manifest_export_roundtrip_for_spec_definition():
    manifest = parse_manifest_data(manifest_payload())
    definition = manifest.to_definition()
    exported = definition.to_manifest()
    assert exported.agent_id == manifest.agent_id
    assert exported.spec == manifest.spec
    assert exported.resources == manifest.resources
    assert [binding.binding_id for binding in exported.bindings] == ["echo"]


def test_manifest_export_refuses_non_portable_factory():
    definition = definition_with_echo()
    with pytest.raises(HarnessError) as excinfo:
        definition.to_manifest()
    assert excinfo.value.code == "INVALID_CONFIGURATION"
    assert "factory" in excinfo.value.details["non_portable"]


def test_spec_and_factory_are_mutually_exclusive():
    with pytest.raises(ValidationError):
        AgentDefinition(
            agent_id="both",
            name="Both",
            spec={"instructions": "x"},
            factory=lambda: None,
        )


def test_duplicate_definition_bindings_rejected():
    with pytest.raises(ValidationError):
        definition_with_echo(
            bindings=[
                DefinitionBinding(binding_id="echo", plugin_id=ECHO_PLUGIN),
                DefinitionBinding(binding_id="echo", plugin_id=ECHO_PLUGIN),
            ]
        )


async def test_manifest_definition_runs_spec_and_managed_capabilities(tmp_path):
    manifest = parse_manifest_data(manifest_payload())
    definition = manifest.to_definition()
    manager = make_manager(tmp_path, echo_descriptor())
    async with manager.open_session(
        definition, resources=ResourceSet({"model": TestModel(call_tools="all", custom_output_text="done")})
    ) as session:
        result = await session.run("hello")
        assert result.output == "done"
        assert "SPEC_MAGIC" in "\n".join(
            str(message.instructions)
            for message in result.new_messages()
            if getattr(message, "instructions", None)
        )
        assert "MANIFEST_MARKER" in "\n".join(
            str(message.instructions)
            for message in result.new_messages()
            if getattr(message, "instructions", None)
        )
        assert result.usage.tool_calls == 1


# --- resources ----------------------------------------------------------


async def test_missing_required_workspace_is_workspace_unavailable(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo(
        resources=[ResourceRequirement(name="workspace.main", kind="workspace")]
    )
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(definition)
    assert excinfo.value.code == "WORKSPACE_UNAVAILABLE"
    assert excinfo.value.details["resource"] == "workspace.main"


async def test_missing_model_resource_is_resource_unavailable(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo(
        resources=[ResourceRequirement(name="model.default", kind="model")]
    )
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(definition)
    assert excinfo.value.code == "RESOURCE_UNAVAILABLE"


async def test_host_aliases_resolve_named_requirements(tmp_path):
    requirements = [
        ResourceRequirement(name="workspace.main", kind="workspace"),
        ResourceRequirement(name="model.default", kind="model"),
    ]
    definition = definition_with_echo(resources=requirements)
    manager = make_manager(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    model = TestModel(call_tools="all", custom_output_text="done")
    async with manager.open_session(
        definition, resources=ResourceSet({"workspace": str(workspace), "model": model})
    ) as session:
        assert (await session._store.get_session(session.session_id)).cwd == str(workspace)
        result = await session.run("hello")
        assert result.output == "done"


def test_workspace_paths_become_local_backends(tmp_path):
    backend = resolve_workspace_value(str(tmp_path))
    assert isinstance(backend, LocalWorkspaceBackend)
    ref = object()
    assert resolve_workspace_value(ref) is ref


def test_resource_set_merge_does_not_mutate():
    original = ResourceSet({"a": 1})
    merged = original.merge({"b": 2})
    assert original.values == {"a": 1}
    assert merged.values == {"a": 1, "b": 2}


# --- definition behavior ------------------------------------------------


async def test_factory_returning_non_agent_fails_preparation(tmp_path):
    definition = definition_with_echo(factory=lambda: "not an agent")
    manager = make_manager(tmp_path)
    async with manager.open_session(definition) as session:
        with pytest.raises(HarnessError) as excinfo:
            await session.run("hello")
        assert excinfo.value.code == "INVALID_CONFIGURATION"
        assert session.snapshot.application_status == "failed"


async def test_enabled_ordering_is_stable_and_topological(tmp_path):
    a = echo_descriptor(plugin_id="test.a")
    b = echo_descriptor(plugin_id="test.b", requirements=["c"])
    c = echo_descriptor(plugin_id="test.c")
    manager = make_manager(tmp_path, a, b, c)
    definition = definition_with_echo(
        bindings=[
            DefinitionBinding(binding_id="b", plugin_id="test.b"),
            DefinitionBinding(binding_id="a", plugin_id="test.a"),
            DefinitionBinding(binding_id="c", plugin_id="test.c"),
        ]
    )
    session = await manager.open_session(definition)
    try:
        # Definition order is b, a, c but b requires c, so c must precede b.
        assert [offering.binding_id for offering in session._catalog.enabled_ordered()] == [
            "a",
            "c",
            "b",
        ]
    finally:
        await session.close()


async def test_requirement_cycle_fails_deployment_validation(tmp_path):
    a = echo_descriptor(plugin_id="test.a", requirements=["b"])
    b = echo_descriptor(plugin_id="test.b", requirements=["a"])
    manager = make_manager(tmp_path, a, b)
    definition = definition_with_echo(
        bindings=[
            DefinitionBinding(binding_id="a", plugin_id="test.a"),
            DefinitionBinding(binding_id="b", plugin_id="test.b"),
        ]
    )
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(definition)
    assert excinfo.value.code == "INVALID_CONFIGURATION"
    assert "cycle" in excinfo.value.message.lower()


async def test_unknown_requirement_resource_fails_validation(tmp_path):
    a = echo_descriptor(plugin_id="test.a", requirements=["mystery"])
    manager = make_manager(tmp_path, a)
    definition = definition_with_echo(
        bindings=[DefinitionBinding(binding_id="a", plugin_id="test.a")]
    )
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(definition)
    assert excinfo.value.code == "INVALID_CONFIGURATION"
    assert excinfo.value.details["requirement"] == "mystery"


async def test_policy_override_fields_merge_over_plugin_defaults(tmp_path):
    descriptor = echo_descriptor(
        policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_enable=False,
            client_can_disable=False,
            editable_fields={"marker"},
            change_mode="next_turn",
        )
    )
    manager = make_manager(tmp_path, descriptor)
    definition = definition_with_echo(
        bindings=[
            DefinitionBinding(
                binding_id="echo",
                plugin_id=ECHO_PLUGIN,
                policy=CapabilityPolicy(client_can_disable=True),
            )
        ]
    )
    async with manager.open_session(definition) as session:
        descriptors, _ = await session.capabilities()
        echo = descriptors[0]
        assert echo.policy.client_can_disable is True  # definition override
        assert echo.policy.client_can_enable is False  # plugin default retained
        assert echo.policy.editable_fields == {"marker"}
        await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], "override-1", 1
        )
