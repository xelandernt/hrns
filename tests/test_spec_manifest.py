"""Concept §6/§16.2: a declarative envelope preserves binding policy."""

import pytest
from harness_protocol import CapabilityPolicy, HarnessError
from harness_runtime import (
    AgentDefinition,
    DefinitionBinding,
    parse_manifest_data,
    parse_manifest_text,
)


def test_manifest_binding_policy_roundtrips_through_python_and_yaml():
    policy = CapabilityPolicy(
        available=True,
        enabled_by_default=False,
        client_can_enable=False,
        client_can_disable=True,
        editable_fields={"max_results"},
        change_mode="next_turn",
    )
    definition = AgentDefinition(
        agent_id="example.research",
        name="Research",
        definition_version="1.2.0",
        bindings=[
            DefinitionBinding(
                binding_id="search",
                plugin_id="pydantic.web_search",
                config={"max_results": 5},
                enabled=False,
                policy=policy,
            )
        ],
    )
    manifest = definition.to_manifest()
    assert manifest.bindings[0].policy == policy
    rebuilt = manifest.to_definition()
    assert rebuilt.bindings[0].policy == policy

    # The concept's YAML uses the policy object inside the binding.
    text = """
schema_version: 1
agent_id: example.research
name: Research
bindings:
  - binding_id: search
    plugin_id: pydantic.web_search
    enabled: false
    policy:
      available: true
      enabled_by_default: false
      client_can_enable: false
      client_can_disable: true
      editable_fields: [max_results]
      change_mode: next_turn
"""
    parsed = parse_manifest_text(text, fmt="yaml")
    assert parsed.bindings[0].policy is not None
    assert parsed.bindings[0].policy.client_can_disable is True
    assert parsed.bindings[0].policy.editable_fields == {"max_results"}


def test_manifest_rejects_unknown_policy_fields():
    with pytest.raises(HarnessError):
        parse_manifest_data({
            "schema_version": 1,
            "agent_id": "x",
            "name": "x",
            "bindings": [{
                "binding_id": "b",
                "plugin_id": "p",
                "policy": {"available": True, "secret": "nope"},
            }],
        })
