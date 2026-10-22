"""Native agent composition shared by every Harness entrypoint.

A definition is composed exactly once per session. The result is a native Pydantic AI
``Agent`` plus the per-session run configuration the upstream ACP adapter applies:
capabilities, resources, and an optional local workspace. Nothing here owns history,
transcripts, turns, or ACP execution.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.settings import ModelSettings

from harness.config import EFFORT_OFF, THINKING_LEVELS
from harness.protocol import BindingChange, ConfigurationSnapshot, HarnessError

from ._model_usage import normalize_model_usage
from .definitions import (
    AGENT_FACTORY_PARAM,
    AgentDefinition,
    AgentFactoryContext,
    factory_declared_params,
    factory_managed_param,
)
from .policy import PolicyCatalog
from .registry import PluginRegistry
from .resources import ResourceSet, resolve_workspace_value

__all__ = [
    "Composition",
    "EffortState",
    "compose_definition",
    "compose_session_config",
]

#: Providers whose model classes accept the explicit OpenAI reasoning-effort key.
#: Custom model ids use it because the unified ``thinking`` setting is gated on a
#: model profile the registry does not have for them.
_OPENAI_EFFORT_PROVIDERS = frozenset(
    {"openai", "openai-chat", "openai-responses", "azure", "azure-responses", "deepseek"}
)


@dataclass
class EffortState:
    """Mutable per-session reasoning effort read by a native capability per run."""

    effort: str | None = None
    support: str | None = None
    model: str | None = None
    base: dict[str, Any] = field(default_factory=dict)

    def set_model(self, model: str | None, support: str | None) -> None:
        self.model = model
        self.support = support
        if self.effort is not None and support is None:
            self.effort = None

    def settings(self, _ctx: Any = None) -> ModelSettings:
        """Return the merged model settings for the next request."""
        settings: dict[str, Any] = dict(self.base)
        settings.pop("thinking", None)
        if self.support is None or self.effort is None:
            return settings  # type: ignore[return-value]
        off = self.effort == EFFORT_OFF
        settings["thinking"] = False if off else self.effort
        provider = (self.model or "").partition(":")[0]
        if self.support == "custom" and provider in _OPENAI_EFFORT_PROVIDERS:
            settings["openai_reasoning_effort"] = "none" if off else self.effort
        return settings  # type: ignore[return-value]


@dataclass(kw_only=True)
class _EffortCapability(AbstractCapability[Any]):
    """Contribute the session's current effort to every model request.

    The capability is constructed once; ``get_model_settings`` returns a callable so a
    change made while the session is open applies from the next request.
    """

    state: EffortState
    id: str | None = "harness.effort"

    def get_model_settings(self) -> Any:
        return self.state.settings


@dataclass
class Composition:
    """One session's native agent and its ACP run configuration."""

    agent: Any
    catalog: PolicyCatalog
    snapshot: ConfigurationSnapshot
    resources: ResourceSet
    effort: EffortState
    config: Any
    capabilities: list[AbstractCapability[Any]] = field(default_factory=list)
    workspace: Any = None
    deps: Any = None

    def descriptors(self) -> list[Any]:
        return list(self.catalog.descriptors())


def _capability_tool_names(capability: Any) -> set[str]:
    """Best-effort tool names contributed by one capability instance."""
    names: set[str] = set()
    try:
        toolset = capability.get_toolset()
    except Exception:
        toolset = None
    tools = getattr(toolset, "tools", None)
    if isinstance(tools, Mapping):
        names.update(str(name) for name in tools)
    return names


def _validate_composed_identities(
    definition: AgentDefinition, composed: Sequence[tuple[str, Any]]
) -> None:
    """Reject duplicate capability ids/tools before Pydantic AI silently merges them."""
    seen_ids: dict[str, str] = {}
    seen_tools: dict[str, str] = {}
    fixed = list(definition.fixed_capabilities)
    all_capabilities: list[tuple[str, Any]] = [("fixed", capability) for capability in fixed]
    all_capabilities.extend(composed)
    for owner, capability in all_capabilities:
        capability_id = getattr(capability, "id", None)
        if isinstance(capability_id, str) and capability_id:
            previous = seen_ids.get(capability_id)
            if previous is not None and previous != owner:
                raise HarnessError(
                    "DEPENDENCY_CONFLICT",
                    f"Capabilities {previous!r} and {owner!r} both use id {capability_id!r}.",
                    details={"capability_id": capability_id, "owners": [previous, owner]},
                )
            seen_ids[capability_id] = owner
        for tool_name in _capability_tool_names(capability):
            previous = seen_tools.get(tool_name)
            if previous is not None and previous != owner:
                raise HarnessError(
                    "DEPENDENCY_CONFLICT",
                    f"Capabilities {previous!r} and {owner!r} both contribute tool {tool_name!r}.",
                    details={"tool_name": tool_name, "owners": [previous, owner]},
                )
            seen_tools[tool_name] = owner


def _factory_kwargs(
    definition: AgentDefinition,
    factory: Any,
    *,
    resources: ResourceSet,
    snapshot: ConfigurationSnapshot,
    composed: Sequence[tuple[str, Any]],
    managed_capabilities: list[Any] | None,
    model: Any,
    session_id: str,
    principal: str,
    deployment_id: str,
) -> dict[str, Any]:
    declared = set(factory_declared_params(factory))
    try:
        parameters = inspect.signature(factory).parameters.values()
        accepts_var_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
    except (TypeError, ValueError):
        accepts_var_kwargs = True
    context = AgentFactoryContext(
        session_id=session_id,
        principal=principal,
        deployment_id=deployment_id,
        binding_ids=tuple(binding_id for binding_id, _ in composed),
    )
    values: dict[str, Any] = {
        "resources": resources,
        "active_config": snapshot,
        "model": model,
        AGENT_FACTORY_PARAM: managed_capabilities,
        "context": context,
        "principal": principal,
        "deployment_id": deployment_id,
    }
    return {name: value for name, value in values.items() if accepts_var_kwargs or name in declared}


async def _build_agent(
    definition: AgentDefinition,
    *,
    composed: Sequence[tuple[str, Any]],
    snapshot: ConfigurationSnapshot,
    resources: ResourceSet,
    model: Any,
    session_id: str,
    principal: str,
    deployment_id: str,
) -> tuple[Any, list[Any]]:
    """Construct the native agent and any capabilities that must be applied per run."""
    capabilities = [capability for _, capability in composed]
    _validate_composed_identities(definition, composed)
    if definition.spec is not None:
        spec: Any = definition.spec
        if isinstance(spec, BaseModel):
            spec = spec.model_dump(mode="json", exclude_defaults=True)
        agent = Agent.from_spec(
            spec,
            model=model,
            capabilities=[*definition.fixed_capabilities, *capabilities],
        )
        return agent, []
    if definition.factory is not None:
        factory = definition.factory
        managed_at_agent = factory_managed_param(factory)
        factory_caps = capabilities if managed_at_agent else None
        kwargs = _factory_kwargs(
            definition,
            factory,
            resources=resources,
            snapshot=snapshot,
            composed=composed,
            managed_capabilities=factory_caps,
            model=model,
            session_id=session_id,
            principal=principal,
            deployment_id=deployment_id,
        )
        agent = factory(**kwargs)
        if inspect.isawaitable(agent):
            agent = await agent
        if not hasattr(agent, "run_stream_events") or not hasattr(agent, "run"):
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Definition factory for {definition.agent_id!r} did not return a native agent.",
                details={"returned": type(agent).__name__},
            )
        if isinstance(agent, Agent):
            agent.model = normalize_model_usage(agent.model)
        fixed = list(definition.fixed_capabilities)
        if managed_at_agent:
            return agent, fixed
        return agent, [*fixed, *capabilities]
    agent = Agent(
        model=model,
        capabilities=[*definition.fixed_capabilities, *capabilities],
    )
    return agent, []


def _workspace_value(definition: AgentDefinition, resources: ResourceSet) -> Any:
    for requirement in definition.resources:
        if requirement.kind != "workspace":
            continue
        found, value = resources.try_resolve(requirement)
        return resolve_workspace_value(value) if found else None
    return None


async def compose_definition(
    definition: AgentDefinition,
    registry: PluginRegistry,
    *,
    resources: ResourceSet | Mapping[str, Any] | None = None,
    session_id: str,
    principal: str = "local",
    deployment_id: str = "local",
    initial_configuration: Sequence[BindingChange] | None = None,
    saved_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    model: Any = None,
    model_settings: Mapping[str, Any] | None = None,
    effort: str | None = None,
    effort_support: str | None = None,
    deps: Any = None,
) -> Composition:
    """Compose a definition into a native agent and an ACP session configuration.

    ``initial_configuration`` applies once, at session creation. ``saved_bindings`` restores
    the selected bindings of an opened session. The two are mutually exclusive.
    """
    from pydantic_ai_harness.experimental.acp import AcpSessionConfig

    if initial_configuration and saved_bindings:
        raise HarnessError(
            "INVALID_CONFIGURATION",
            "A session composition cannot be both initial and restored.",
        )
    catalog = PolicyCatalog(
        definition,
        registry,
        session_id=session_id,
        principal=principal,
        deployment_id=deployment_id,
    )
    if initial_configuration:
        outcome = catalog.apply_changes(list(initial_configuration), initial_configuration=True)
        catalog.commit(outcome)
    if saved_bindings:
        catalog.restore_bindings(saved_bindings)
    catalog.validate_selected_state()
    snapshot = catalog.snapshot(
        selected_revision=1,
        active_revision=1,
        application_status="applied",
    )
    resource_values = (
        resources.values if isinstance(resources, ResourceSet) else dict(resources or {})
    )
    resource_set = ResourceSet(resource_values)
    resource_set.validate_requirements(definition.resources)
    composed = catalog.compose(
        resources=resource_set,
        active_configuration=snapshot,
        agent_context=AgentFactoryContext(
            session_id=session_id,
            principal=principal,
            deployment_id=deployment_id,
            binding_ids=tuple(offering.binding_id for offering in catalog.enabled_ordered()),
        ),
    )
    selected_model = (
        normalize_model_usage(model)
        or normalize_model_usage(resource_set.get_model())
        or normalize_model_usage(definition.model)
    )
    base_settings = dict(model_settings or {})
    base_settings.pop("thinking", None)
    effort_state = EffortState(
        effort=effort if effort in (*THINKING_LEVELS, EFFORT_OFF) else None,
        support=effort_support,
        model=selected_model if isinstance(selected_model, str) else None,
        base=base_settings,
    )
    if effort_state.effort is not None and effort_state.support is None:
        effort_state.effort = None
    agent, run_capabilities = await _build_agent(
        definition,
        composed=composed,
        snapshot=snapshot,
        resources=resource_set,
        model=selected_model,
        session_id=session_id,
        principal=principal,
        deployment_id=deployment_id,
    )
    capabilities = [_EffortCapability(state=effort_state), *run_capabilities]
    workspace = _workspace_value(definition, resource_set)
    config = AcpSessionConfig(
        deps=None,
        capabilities=capabilities,
        toolsets=None,
        workspace=workspace,
    )
    return Composition(
        agent=agent,
        catalog=catalog,
        snapshot=snapshot,
        resources=resource_set,
        effort=effort_state,
        config=config,
        capabilities=capabilities,
        workspace=workspace,
        deps=deps,
    )


def compose_session_config(composition: Composition) -> Any:
    """Return the adapter-facing run configuration for a composition."""
    return composition.config
