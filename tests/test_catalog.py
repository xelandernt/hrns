"""Coverage-integrity tests for the bundled lazy capability catalog.

These tests are deliberately about *honesty*: every advertised import target must exist in
the pinned environment, verified entries must actually construct offline, install-required
entries must fail closed with the exact install command, and unavailable upstream features
must never be registered as usable plugins.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from catalog_support import example_config, make_binding, offline_resource_set  # noqa: F401
from harness_protocol import HarnessError
from harness_runtime import catalog
from harness_runtime.catalog import CatalogKind, CatalogTier, iter_catalog, iter_plugins
from harness_runtime.registry import PluginRegistry
from pydantic import ValidationError


def test_plugin_ids_are_unique_and_valid() -> None:
    entries = catalog.iter_catalog()
    ids = [entry.plugin_id for entry in entries]
    assert len(ids) == len(set(ids)), "duplicate plugin ids are configuration errors"
    for plugin_id in ids:
        # The same pattern as the wire contract's BindingId.
        assert re.fullmatch(r"[a-zA-Z0-9_.:-]{1,200}", plugin_id), plugin_id


def test_every_entry_has_a_real_import_target_or_an_explicit_gap() -> None:
    for entry in iter_catalog():
        if entry.tier is CatalogTier.UNAVAILABLE:
            assert entry.upstream_import is None, entry.plugin_id
            assert entry.factory is None
            assert not entry.managed
            assert entry.notes, f"{entry.plugin_id} must document why it is absent"
            continue
        assert entry.upstream_import, entry.plugin_id
        module_name, _, attribute = entry.upstream_import.partition(":")
        assert module_name and attribute, entry.upstream_import
        # find_spec imports only parent packages, so this validates the module path without
        # executing the optional submodule (and therefore without optional dependencies).
        assert importlib.util.find_spec(module_name) is not None, (
            f"{entry.plugin_id}: module {module_name!r} does not exist in the pinned upstream"
        )
        expected_integration = (
            "pydantic-ai-harness"
            if module_name.startswith("pydantic_ai_harness.")
            else "pydantic-ai-slim"
        )
        assert entry.integration == expected_integration, (
            f"{entry.plugin_id}: integration {entry.integration!r} does not match "
            f"import target {module_name!r}"
        )
        assert entry.version == catalog.PLUGIN_VERSION[entry.integration], entry.plugin_id


def test_catalog_import_loads_no_optional_integrations() -> None:
    optional = [
        "modal",
        "e2b",
        "smolmachines",
        "sprites",
        "pydantic_monty",
        "browser_use",
        "playwright",
        "exa_py",
        "youdotcom",
        "temporalio",
        "dbos",
        "prefect",
        "render",
        "pgtask",
        "absurd_sdk",
        "logfire",
        "stackone_defender",
        "ddgs",
        "fastmcp",
    ]
    script = (
        "import json, sys; import harness_runtime.catalog; "
        f"print(json.dumps(sorted(m for m in sys.modules if m in {optional!r})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    loaded = json.loads(completed.stdout)
    assert loaded == [], f"catalog import pulled in optional integrations: {loaded}"


def test_verified_entries_construct_offline(binding_for) -> None:
    constructed: list[str] = []
    for entry in iter_plugins(tiers=[CatalogTier.VERIFIED]):
        assert entry.managed, f"{entry.plugin_id} is verified but unmanaged"
        config = entry.config_model(**example_config(entry.plugin_id))
        capability = entry.factory(config, binding_for(entry.plugin_id))
        assert capability is not None
        constructed.append(entry.plugin_id)
    assert "harness.filesystem" in constructed
    assert "harness.planning" in constructed
    assert "pydantic.web_search" in constructed


def test_verified_factories_return_native_capabilities_or_callables(binding_for) -> None:
    from pydantic_ai.capabilities import AbstractCapability

    for entry in iter_plugins(tiers=[CatalogTier.VERIFIED]):
        config = entry.config_model(**example_config(entry.plugin_id))
        value = entry.factory(config, binding_for(entry.plugin_id))
        assert isinstance(value, AbstractCapability) or callable(value), entry.plugin_id


def test_install_required_entries_fail_closed_through_the_registry(binding_for) -> None:
    """Composing a missing-extra plugin raises CAPABILITY_UNAVAILABLE; it never downgrades."""
    from harness_runtime.registry import PluginRegistry

    registry = PluginRegistry()
    checked = 0
    for entry in iter_plugins(tiers=[CatalogTier.INSTALL_REQUIRED]):
        if entry.factory is None:
            continue
        registry.register(entry.descriptor())
        config = entry.config_model(**example_config(entry.plugin_id)) if entry.config_model else {}
        with pytest.raises(HarnessError) as caught:
            registry.instantiate(
                entry.descriptor(),
                config.model_dump() if hasattr(config, "model_dump") else config,
                binding_for(entry.plugin_id),
            )
        assert caught.value.code == "CAPABILITY_UNAVAILABLE", entry.plugin_id
        message = caught.value.message
        assert "optional" in message.lower() or "Install" in message, message
        checked += 1
    assert checked >= 10, f"expected a broad install-required inventory, checked {checked}"


def test_unavailable_entries_are_not_registered() -> None:
    registry = catalog.build_catalog_registry()
    unavailable = catalog.unavailable_entries()
    assert {entry.plugin_id for entry in unavailable} == {
        "pydantic.restate_durability",
        "pydantic.kitaru_durability",
        "pydantic.airflow_durability",
    }
    for entry in unavailable:
        assert registry.lookup(entry.plugin_id) is None
        with pytest.raises(HarnessError) as caught:
            entry.descriptor()
        assert caught.value.code == "CAPABILITY_UNAVAILABLE"
        assert "no factory" in caught.value.message.lower()


def test_plugin_infos_are_honest() -> None:
    infos = {info.plugin_id: info for info in catalog.plugin_infos()}
    assert all(info.managed for info in infos.values())
    assert all(info.runtime_compatible for info in infos.values())
    for plugin_id, info in infos.items():
        entry = catalog.entry(plugin_id)
        assert info.version == entry.version
        assert info.change_mode == entry.change_mode
        if entry.config_model is not None:
            schema = info.config_schema
            assert schema["additionalProperties"] is False, plugin_id
            if entry.config_model.model_fields:
                assert schema.get("properties"), plugin_id
    # Documented-only entries are visible in the inventory but never presented as managed.
    for entry in iter_catalog():
        if entry.factory is None or entry.config_model is None:
            assert not entry.managed, entry.plugin_id


def test_config_models_reject_unknown_and_required_fields() -> None:
    for entry in iter_plugins():
        if entry.config_model is None:
            continue
        config = entry.config_model(**example_config(entry.plugin_id))
        with pytest.raises(ValidationError):
            entry.config_model.model_validate({**config.model_dump(), "definitely_unknown": 1})


@pytest.mark.parametrize(
    ("plugin_id", "config"),
    [
        ("pydantic.web_search", {"max_uses": -1}),
        ("harness.clear_tool_results", {"max_tokens": 10, "max_fraction": 0.5}),
        ("harness.clamp_oversized", {}),
        ("harness.spend_limits", {"budgets": [{"usd": "-1"}]}),
        ("harness.input_guardrail", {"detectors": [{"kind": "blocked_keywords"}]}),
        ("harness.skills", {"include": ["a"], "exclude": ["b"]}),
        ("harness.localstack", {"allowed_services": ["s3"], "denied_services": ["dynamodb"]}),
    ],
)
def test_management_config_validation_is_real(plugin_id: str, config: dict) -> None:
    entry = catalog.entry(plugin_id)
    assert entry.config_model is not None
    with pytest.raises(ValidationError):
        entry.config_model.model_validate(config)


def test_registering_a_duplicate_id_is_a_configuration_error() -> None:
    registry = PluginRegistry()
    descriptor = catalog.entry("harness.filesystem").descriptor()
    registry.register(descriptor)
    conflicting = descriptor.model_copy(update={"description": "different descriptor"})
    with pytest.raises(HarnessError) as caught:
        registry.register(conflicting)
    assert caught.value.code == "INVALID_CONFIGURATION"
    assert "already registered" in caught.value.message


def test_policy_is_the_authoritative_lifecycle_mirror() -> None:
    for entry in iter_catalog():
        policy = entry.policy()
        assert policy.change_mode == entry.change_mode, entry.plugin_id
        if entry.kind is CatalogKind.CAPABILITY and entry.tier is not CatalogTier.UNAVAILABLE:
            if not entry.managed:
                continue  # documented-only gap: no descriptor to build
            descriptor = entry.descriptor()
            assert descriptor.change_mode == descriptor.default_policy.change_mode
            assert descriptor.change_mode == entry.change_mode
            assert entry.info().change_mode == entry.change_mode


def test_catalog_registry_contains_all_managed_plugins() -> None:
    registry = catalog.build_catalog_registry()
    expected = {
        entry.plugin_id
        for entry in iter_plugins(tiers=[CatalogTier.VERIFIED, CatalogTier.INSTALL_REQUIRED])
    }
    expected.add("example.tone")  # bundled demo plugin
    assert {descriptor.plugin_id for descriptor in registry.ordered()} == expected
    assert len(expected) >= 80


def test_media_stores_are_resource_factories_not_bindings() -> None:
    stores = catalog.media_store_entries()
    assert {entry.plugin_id for entry in stores} == {
        "harness.media.disk",
        "harness.media.sqlite",
        "harness.media.s3",
        "harness.media.postgres",
    }
    assert all(entry.kind is CatalogKind.RESOURCE for entry in stores)
    registry = catalog.build_catalog_registry()
    for entry in stores:
        assert registry.lookup(entry.plugin_id) is None
