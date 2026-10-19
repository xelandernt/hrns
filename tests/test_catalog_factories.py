"""Behavioral tests for managed catalog factories.

The contract under test: factories perform real Pydantic validation, non-serializable
dependencies arrive only through named resources, and a missing requirement raises the
runtime's availability error instead of inventing host state.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from catalog_support import example_config, make_binding, offline_resource_set
from harness_protocol import HarnessError
from harness_runtime import catalog
from harness_runtime.registry import PluginRegistry
from harness_runtime.resources import ResourceSet


def _config(plugin_id: str, **overrides):
    entry = catalog.entry(plugin_id)
    assert entry.config_model is not None
    payload = {**example_config(plugin_id), **overrides}
    return entry.config_model(**payload)


def test_workspace_capabilities_never_fall_back_to_host_execution(binding_for) -> None:
    empty = ResourceSet({})
    for plugin_id in ("harness.filesystem", "harness.shell", "harness.coder"):
        entry = catalog.entry(plugin_id)
        with pytest.raises(HarnessError) as caught:
            entry.factory(_config(plugin_id), make_binding(plugin_id, empty))
        assert caught.value.code == "WORKSPACE_UNAVAILABLE", plugin_id
        assert "workspace" in caught.value.message
        assert "no host fallback" in caught.value.message


def test_resource_missing_errors_are_actionable(binding_for) -> None:
    cases = {
        "harness.memory": "memory.store",
        "harness.step_persistence": "step.store",
        "harness.conversation_search": "conversations",
        "harness.ask_user": "user_answers",
        "pydantic.prefix_tools": "wrapped.capability",
        "pydantic.use_thread_executor": "thread.executor",
        "pydantic.handle_deferred_tool_calls": "deferred.handler",
        "harness.tool_call_judge": "model.judge",
        "harness.trajectory_judge": "model.judge",
    }
    empty = ResourceSet({})
    for plugin_id, resource in cases.items():
        entry = catalog.entry(plugin_id)
        with pytest.raises(HarnessError) as caught:
            entry.factory(_config(plugin_id), make_binding(plugin_id, empty))
        assert caught.value.code == "RESOURCE_UNAVAILABLE", plugin_id
        assert resource in caught.value.message
        assert "no host fallback" in caught.value.message


def test_local_workspace_uses_the_bound_directory(binding_for, tmp_path) -> None:
    from pydantic_ai.workspaces import LocalWorkspaceBackend

    capability = catalog.entry("pydantic.local_workspace").factory(
        _config("pydantic.local_workspace"),
        make_binding("pydantic.local_workspace", ResourceSet({"workspace": str(tmp_path)})),
    )
    assert str(capability.working_dir) == str(tmp_path)

    backend = LocalWorkspaceBackend(str(tmp_path / "backend"))
    from_backend = catalog.entry("pydantic.local_workspace").factory(
        _config("pydantic.local_workspace"),
        make_binding("pydantic.local_workspace", ResourceSet({"workspace": backend})),
    )
    assert str(from_backend.working_dir) == str(tmp_path / "backend")


def test_memory_binds_the_exact_store(binding_for) -> None:
    entry = catalog.entry("harness.memory")
    config = _config("harness.memory", agent_name="notes", max_tokens=1_500)
    capability = entry.factory(config, binding_for("harness.memory"))
    from pydantic_ai_harness.memory import InMemoryStore

    assert isinstance(capability.store, InMemoryStore)
    assert capability.agent_name == "notes"
    assert capability.max_tokens == 1_500


def test_tool_output_limits_store_modes(binding_for, catalog_resources) -> None:
    entry = catalog.entry("harness.tool_output_limits")

    # Default bands cannot spill, so no workspace resource is required.
    truncate_only = entry.factory(
        _config("harness.tool_output_limits"), make_binding("harness.tool_output_limits", ResourceSet({}))
    )
    assert truncate_only.store is None

    # A spill band with store='workspace' requires the workspace resource.
    with pytest.raises(HarnessError) as caught:
        entry.factory(
            entry.config_model(
                bands=[{"over": 1_000, "action": {"kind": "spill"}}],
                store="workspace",
            ),
            make_binding("harness.tool_output_limits", ResourceSet({})),
        )
    assert caught.value.code == "WORKSPACE_UNAVAILABLE"

    # store='local_file' is an explicit host choice and needs no workspace.
    local_file = entry.factory(
        entry.config_model(
            bands=[{"over": 1_000, "action": {"kind": "spill"}}],
            store="local_file",
        ),
        make_binding("harness.tool_output_limits", ResourceSet({})),
    )
    from pydantic_ai_harness.tool_output_limits import LocalFileStore

    assert isinstance(local_file.store, LocalFileStore)

    # store='resource' requires its named resource, never a silent fallback.
    with pytest.raises(HarnessError) as caught:
        entry.factory(
            entry.config_model(store="resource"),
            make_binding("harness.tool_output_limits", ResourceSet({})),
        )
    assert caught.value.code == "RESOURCE_UNAVAILABLE"
    assert "tool_overflow.store" in caught.value.message


def test_planning_run_store_and_resource_store(binding_for, tmp_path) -> None:
    entry = catalog.entry("harness.planning")
    # Upstream treats store=None as a fresh per-run in-memory plan (created lazily);
    # the catalog does not substitute a process-global store.
    run_scoped = entry.factory(
        _config("harness.planning"), make_binding("harness.planning", ResourceSet({}))
    )
    assert run_scoped.store is None

    from pydantic_ai_harness.planning import SqlitePlanStore

    durable = SqlitePlanStore(str(tmp_path / "plan.db"), session="test")
    capability = entry.factory(
        _config("harness.planning", store="resource"),
        make_binding("harness.planning", ResourceSet({"plan.store": durable})),
    )
    assert capability.store is durable


def test_step_persistence_requires_explicit_store(binding_for) -> None:
    entry = catalog.entry("harness.step_persistence")
    from pydantic_ai_harness.step_persistence import InMemoryStepStore

    store = InMemoryStepStore()
    capability = entry.factory(
        _config("harness.step_persistence", agent_name="demo"),
        make_binding("harness.step_persistence", ResourceSet({"step.store": store})),
    )
    assert capability.store is store
    assert capability.agent_name == "demo"


def test_guardrail_configuration_and_detector_chain(binding_for) -> None:
    entry = catalog.entry("harness.input_guardrail")
    config = entry.config_model(
        detectors=[
            {"kind": "redact_secrets"},
            {"kind": "blocked_keywords", "keywords": ["project-x"], "whole_words": True},
        ]
    )
    capability = entry.factory(config, binding_for("harness.input_guardrail"))
    assert len(capability.guard) == 2

    with pytest.raises(Exception):
        entry.config_model(detectors=[{"kind": "blocked_keywords"}])


def test_judge_requires_question_and_model(binding_for) -> None:
    entry = catalog.entry("harness.tool_call_judge")
    config = entry.config_model(question="Would this delete data?", tools=["run_command"])
    capability = entry.factory(config, binding_for("harness.tool_call_judge"))
    assert capability.question == "Would this delete data?"


def test_spend_limits_validate_budgets(binding_for) -> None:
    entry = catalog.entry("harness.spend_limits")
    config = entry.config_model(
        budgets=[
            {"usd": "5.00", "window": "run"},
            {"tokens": 100_000, "window": "day", "warn_at": 0.8},
        ]
    )
    capability = entry.factory(config, binding_for("harness.spend_limits"))
    assert len(capability.budgets) == 2
    assert capability.budgets[0].usd == Decimal("5.00")


def test_web_search_local_fallback_reports_missing_extra(binding_for) -> None:
    entry = catalog.entry("pydantic.web_search")
    config = entry.config_model(local=True)
    with pytest.raises(HarnessError) as caught:
        entry.factory(config, binding_for("pydantic.web_search"))
    assert caught.value.code == "CAPABILITY_UNAVAILABLE"
    assert "duckduckgo" in caught.value.message


def test_config_shim_is_used_for_no_config_plugins(binding_for) -> None:
    """Registry passes a dict shim when a plugin has no config model."""
    entry = catalog.entry("harness.repair_tool_arguments")
    assert entry.config_model is not None  # _NoConfig: unknown keys are still rejected
    capability = entry.factory(entry.config_model(), binding_for("harness.repair_tool_arguments"))
    assert type(capability).__name__ == "RepairToolArguments"


def test_descriptor_round_trip_and_schema() -> None:
    entry = catalog.entry("pydantic.web_search")
    descriptor = entry.descriptor()
    assert descriptor.plugin_id == entry.plugin_id
    assert descriptor.config_model is entry.config_model
    assert descriptor.change_mode == "next_turn"
    assert descriptor.to_info().managed is True
    assert descriptor.config_schema()["additionalProperties"] is False
    assert "max_uses" in descriptor.config_schema()["properties"]
    defaults = descriptor.config_defaults()
    assert defaults["native"] is True


def test_registry_instantiates_verified_plugins_through_real_context() -> None:
    registry = PluginRegistry()
    catalog.register_catalog(registry)
    resources = offline_resource_set("/tmp/harness-catalog-test")
    plugin_id = "harness.planning"
    context = make_binding(plugin_id, resources)
    capability = registry.instantiate(
        registry.require(plugin_id), {"enable_subtasks": True}, context
    )
    assert type(capability).__name__ == "Planning"

    with pytest.raises(HarnessError) as caught:
        registry.instantiate(registry.require(plugin_id), {"nope": True}, context)
    assert caught.value.code == "INVALID_CONFIGURATION"
    assert "nope" in caught.value.message


def test_resource_requirements_are_typed() -> None:
    requirements = catalog.resource_requirements(catalog.entry("harness.memory"))
    assert requirements[0].name == "memory.store"
    assert requirements[0].kind == "memory"
    assert requirements[0].required is True


def test_media_store_resources_construct_from_deployment_bindings(tmp_path) -> None:
    """The four media stores are verified resource factories, not agent bindings."""
    resources = ResourceSet(
        {
            "media.s3_credentials": {"access_key_id": "k", "secret_access_key": "s"},
            "media.postgres_pool": object(),
        }
    )
    configs = {
        "harness.media.disk": {"directory": str(tmp_path)},
        "harness.media.sqlite": {"database": str(tmp_path / "media.db")},
        "harness.media.s3": {
            "bucket": "bucket",
            "endpoint": "http://localhost:9000",
            "region": "us-east-1",
        },
        "harness.media.postgres": {},
    }
    built = {}
    for entry in catalog.media_store_entries():
        config = entry.config_model(**configs[entry.plugin_id])
        built[entry.plugin_id] = entry.factory(config, make_binding(entry.plugin_id, resources))
    assert type(built["harness.media.disk"]).__name__ == "DiskMediaStore"
    assert type(built["harness.media.sqlite"]).__name__ == "SqliteMediaStore"

    for plugin_id, missing in (
        ("harness.media.s3", "media.s3_credentials"),
        ("harness.media.postgres", "media.postgres_pool"),
    ):
        entry = catalog.entry(plugin_id)
        config = entry.config_model(**configs[plugin_id])
        with pytest.raises(HarnessError) as caught:
            entry.factory(config, make_binding(plugin_id, ResourceSet({})))
        assert caught.value.code == "RESOURCE_UNAVAILABLE"
        assert missing in caught.value.message


def test_every_config_default_is_json_safe() -> None:
    import json

    for entry in catalog.iter_plugins():
        if entry.config_model is None:
            continue
        config = entry.config_model(**example_config(entry.plugin_id))
        json.dumps(config.model_dump(mode="json"))
