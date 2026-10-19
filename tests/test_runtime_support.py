"""Shared fixtures for runtime tests (no tests are collected from this module).

Small, explicit plugins/definitions keep every test exercising the real Pydantic AI
execution path: native ``Capability`` objects, real ``Agent`` composition, real SQLite
persistence. ``TestModel`` provides deterministic offline model behavior.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from harness_protocol import CapabilityPolicy
from harness_runtime import (
    AgentDefinition,
    DefinitionBinding,
    PluginDescriptor,
    PluginRegistry,
    ResourceRequirement,
    ResourceSet,
    SessionManager,
)
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.capabilities import Capability
from pydantic_ai.models.test import TestModel

ECHO_PLUGIN = "test.echo"
MARKER = "ECHO_MARKER"


class EchoConfig(BaseModel):
    marker: str = MARKER
    limit: int = 3


def echo_tool(text: str) -> str:
    """Echo the provided text."""
    return f"echo:{text}"


slow_tool_started: asyncio.Event | None = None
SLOW_TOOL_TIMEOUT = 30.0


async def slow_tool(seconds: float = 20.0) -> str:
    """A deliberately slow tool used to hold a turn open for concurrency tests."""
    if slow_tool_started is not None:
        slow_tool_started.set()
    await asyncio.sleep(seconds)
    return "slow-done"


def approval_tool(text: str) -> str:
    """An action that needs explicit human approval."""
    return f"approved:{text}"


def build_approval_capability(config: EchoConfig, binding: Any) -> Capability[Any]:
    from pydantic_ai.tools import Tool

    return Capability(
        id=binding.binding_id,
        instructions="APPROVAL active.",
        tools=[Tool(approval_tool, requires_approval=True)],
    )


def approval_definition() -> AgentDefinition:
    from pydantic_ai.tools import DeferredToolRequests

    def factory(*, managed_capabilities: Sequence[Any] = ()) -> Agent[Any, Any]:
        return Agent(
            TestModel(call_tools=["approval_tool"], custom_output_text="approved-done"),
            output_type=str | DeferredToolRequests,
            capabilities=list(managed_capabilities),
        )

    return definition_with_echo(
        factory=factory,
        bindings=[DefinitionBinding(binding_id="approval", plugin_id=ECHO_PLUGIN)],
    )


def build_slow_capability(config: EchoConfig, binding: Any) -> Capability[Any]:
    return Capability(id=binding.binding_id, instructions="SLOW active.", tools=[slow_tool])


def build_failing_capability(config: EchoConfig, binding: Any) -> Capability[Any]:
    if config.marker == "boom":
        raise RuntimeError("capability factory exploded")
    return build_echo_capability(config, binding)


def build_echo_capability(config: EchoConfig, binding: Any) -> Capability[Any]:
    return Capability(
        id=binding.binding_id,
        instructions=f"{config.marker} is active.",
        tools=[echo_tool],
    )


def echo_descriptor(
    *,
    policy: CapabilityPolicy | None = None,
    change_mode: str = "next_turn",
    requirements: Sequence[str] = (),
    conflicts: Sequence[str] = (),
    plugin_id: str = ECHO_PLUGIN,
    config_model: type[BaseModel] | None = EchoConfig,
    factory: Any = build_echo_capability,
) -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id=plugin_id,
        version="1.0.0",
        description="Test echo capability",
        config_model=config_model,
        factory=factory,
        requirements=list(requirements),
        conflicts=list(conflicts),
        change_mode=change_mode,  # type: ignore[arg-type]
        presentation=["test.echo"],
        default_policy=policy
        or CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields={"marker", "limit"},
            change_mode=change_mode,  # type: ignore[arg-type]
        ),
    )


def registry_with(*descriptors: PluginDescriptor, allow_add: bool = False) -> PluginRegistry:
    registry = PluginRegistry(allow_local_binding_creation=allow_add)
    for descriptor in descriptors:
        registry.register(descriptor)
    return registry


# --- factories ---------------------------------------------------------


class FactoryRecorder:
    """Records managed capabilities the definition factory receives at Agent construction."""

    def __init__(self) -> None:
        self.managed_per_call: list[int] = []
        self.agents: list[Agent[Any, Any]] = []


def make_factory(
    recorder: FactoryRecorder,
    *,
    output_type: Any = str,
    call_tools: Any = "all",
    text: str = "done",
    async_factory: bool = False,
) -> Any:
    def build_agent(*, managed_capabilities: Sequence[Any] = ()) -> Agent[Any, Any]:
        recorder.managed_per_call.append(len(managed_capabilities))
        agent = Agent(
            TestModel(call_tools=call_tools, custom_output_text=text),
            output_type=output_type,
            name="runtime-test-agent",
            capabilities=list(managed_capabilities),
        )
        recorder.agents.append(agent)
        return agent

    async def build_agent_async(*, managed_capabilities: Sequence[Any] = ()) -> Agent[Any, Any]:
        return build_agent(managed_capabilities=managed_capabilities)

    if async_factory:
        return build_agent_async
    return build_agent


def run_level_factory(recorder: FactoryRecorder | None = None, **factory_kwargs: Any) -> Any:
    """A factory that does *not* declare ``managed_capabilities`` (runtime injects per run)."""

    def build_agent(*, resources: Any = None, active_config: Any = None) -> Agent[Any, Any]:
        if recorder is not None:
            recorder.managed_per_call.append(0)
        return Agent(
            TestModel(
                call_tools=factory_kwargs.get("call_tools", "all"),
                custom_output_text=factory_kwargs.get("text", "done"),
            ),
            output_type=factory_kwargs.get("output_type", str),
            name="runtime-run-level-agent",
        )

    return build_agent


def definition_with_echo(
    *,
    factory: Any | None = None,
    recorder: FactoryRecorder | None = None,
    bindings: Sequence[DefinitionBinding] | None = None,
    resources: Sequence[ResourceRequirement] = (),
    agent_id: str = "test.agent",
    version: str = "1.0.0",
    fixed_capabilities: Sequence[Any] = (),
    spec: dict[str, Any] | None = None,
) -> AgentDefinition:
    recorder = recorder or FactoryRecorder()
    return AgentDefinition(
        agent_id=agent_id,
        name="Test agent",
        description="Runtime test definition",
        definition_version=version,
        factory=None if spec is not None else (factory or make_factory(recorder)),
        spec=spec,
        fixed_capabilities=list(fixed_capabilities),
        bindings=list(
            bindings
            if bindings is not None
            else [DefinitionBinding(binding_id="echo", plugin_id=ECHO_PLUGIN)]
        ),
        resources=list(resources),
    )


def make_manager(
    tmp_path: Path,
    *descriptors: PluginDescriptor,
    allow_add: bool = False,
    db_name: str = "sessions.db",
) -> SessionManager:
    registry = registry_with(*descriptors or [echo_descriptor()], allow_add=allow_add)
    return SessionManager(registry, store=tmp_path / db_name)


def plain_model(text: str = "done", *, call_tools: Any = "all") -> TestModel:
    return TestModel(call_tools=call_tools, custom_output_text=text)


def resources(**values: Any) -> ResourceSet:
    return ResourceSet(values)
