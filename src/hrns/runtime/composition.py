"""Native agent composition shared by every hrns entrypoint.

A definition is composed when a session is created or loaded and recomposed when its
configuration changes. The result is a native Pydantic AI ``Agent`` plus the per-session run
configuration the upstream ACP adapter applies: capabilities, resources, and an optional local
workspace. Nothing here owns history, transcripts, turns, or ACP execution.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from acp import schema
from pydantic import BaseModel, JsonValue
from pydantic_ai import Agent
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.agent.spec import AgentSpec as NativeAgentSpec
from pydantic_ai.capabilities import AbstractCapability, AgentCapability, DynamicCapability
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import AgentDepsT, RunContext
from typing_extensions import TypeIs

from hrns.config import EFFORT_OFF, THINKING_LEVELS, EffortLevel
from hrns.protocol import (
    BindingChange,
    BindingDescriptor,
    ConfigurationSnapshot,
    HrnsError,
)

from ._model_usage import normalize_model_usage
from .commands import CommandRegistry, build_command_registry
from .definitions import (
    AGENT_FACTORY_PARAM,
    AgentDefinition,
    AgentFactory,
    AgentFactoryContext,
    factory_declared_params,
    factory_managed_param,
)
from .deps import HrnsDeps
from .policy import PolicyCatalog
from .presentation import PresentationObserver, ToolPresentationSnapshot
from .registry import PluginRegistry
from .resources import (
    MODEL,
    WORKSPACE,
    ModelInput,
    ResolvedWorkspace,
    ResourceBindings,
    ResourceSet,
    resolve_workspace_value,
)

if TYPE_CHECKING:
    from pydantic_ai_harness.experimental.acp import AcpSessionConfig

__all__ = [
    "Composition",
    "EffortState",
    "compose_definition",
    "compose_session_config",
]

#: Where a host publishes tool-presentation snapshots for the connected client.
PresentationSink = Callable[[ToolPresentationSnapshot], Awaitable[None]]


class _ProviderSettings(ModelSettings, total=False):
    """Model settings plus the provider-specific reasoning-effort key.

    ``openai_reasoning_effort`` is not part of the unified ``ModelSettings`` surface — it is
    the explicit key providers without a model profile accept — so it needs declaring here
    rather than widening the whole mapping to a loose dict.
    """

    openai_reasoning_effort: Literal["none", EffortLevel] | None


def _is_effort_level(value: object) -> TypeIs[EffortLevel]:
    """Whether a spelling is one of the unified thinking levels."""
    return value in THINKING_LEVELS


def _effort_or_none(value: str) -> EffortLevel | None:
    """The literal level for a spelling, or ``None`` for one pydantic-ai does not define."""
    return value if _is_effort_level(value) else None


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
    base: ModelSettings = field(default_factory=ModelSettings)
    #: Model-id prefix -> Pydantic AI provider id, when the prefix names a provider entry.
    provider_types: Mapping[str, str] = field(default_factory=dict)

    def set_model(self, model: str | None, support: str | None) -> None:
        self.model = model
        self.support = support
        if self.effort is not None and support is None:
            self.effort = None

    def settings(self, _ctx: RunContext[AgentDepsT] | None = None) -> ModelSettings:
        """Return the merged model settings for the next request."""
        merged: _ProviderSettings = {**self.base}
        merged.pop("thinking", None)
        if self.support is None or self.effort is None:
            return merged
        off = self.effort == EFFORT_OFF
        if off:
            merged["thinking"] = False
        elif _is_effort_level(self.effort):
            merged["thinking"] = self.effort
        prefix = (self.model or "").partition(":")[0]
        provider = self.provider_types.get(prefix, prefix)
        if self.support == "custom" and provider in _OPENAI_EFFORT_PROVIDERS:
            merged["openai_reasoning_effort"] = "none" if off else _effort_or_none(self.effort)
        return merged


@dataclass(kw_only=True)
class _EffortCapability(AbstractCapability[AgentDepsT]):
    """Contribute the session's current effort to every model request.

    The capability is constructed once; ``get_model_settings`` returns a callable so a
    change made while the session is open applies from the next request.
    """

    state: EffortState
    id: str | None = "hrns.effort"

    def get_model_settings(self) -> Callable[[RunContext[AgentDepsT]], ModelSettings]:
        return self.state.settings


@dataclass
class Composition:
    """One session's native agent and its ACP run configuration."""

    agent: AbstractAgent
    catalog: PolicyCatalog
    snapshot: ConfigurationSnapshot
    resources: ResourceSet
    effort: EffortState
    config: AcpSessionConfig[object]
    capabilities: list[AbstractCapability[object]] = field(default_factory=list)
    commands: CommandRegistry = field(default_factory=CommandRegistry)
    presentation: PresentationObserver | None = None
    workspace: ResolvedWorkspace | None = None
    deps: object = None

    def descriptors(self) -> list[BindingDescriptor]:
        return list(self.catalog.descriptors())


def _capability_tool_names(capability: AgentCapability) -> set[str]:
    """Best-effort tool names contributed by one capability instance."""
    if not isinstance(capability, AbstractCapability):
        return set()
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
    definition: AgentDefinition, composed: Sequence[tuple[str, AgentCapability]]
) -> None:
    """Reject duplicate capability ids/tools before Pydantic AI silently merges them."""
    seen_ids: dict[str, str] = {}
    seen_tools: dict[str, str] = {}
    fixed = list(definition.fixed_capabilities)
    all_capabilities: list[tuple[str, AgentCapability]] = [
        ("fixed", capability) for capability in fixed
    ]
    all_capabilities.extend(composed)
    for owner, capability in all_capabilities:
        capability_id = getattr(capability, "id", None)
        if isinstance(capability_id, str) and capability_id:
            previous = seen_ids.get(capability_id)
            if previous is not None and previous != owner:
                raise HrnsError(
                    "DEPENDENCY_CONFLICT",
                    f"Capabilities {previous!r} and {owner!r} both use id {capability_id!r}.",
                    details={"capability_id": capability_id, "owners": [previous, owner]},
                )
            seen_ids[capability_id] = owner
        for tool_name in _capability_tool_names(capability):
            previous = seen_tools.get(tool_name)
            if previous is not None and previous != owner:
                raise HrnsError(
                    "DEPENDENCY_CONFLICT",
                    f"Capabilities {previous!r} and {owner!r} both contribute tool {tool_name!r}.",
                    details={"tool_name": tool_name, "owners": [previous, owner]},
                )
            seen_tools[tool_name] = owner


def _factory_kwargs(
    factory: AgentFactory,
    *,
    resources: ResourceSet,
    snapshot: ConfigurationSnapshot,
    composed: Sequence[tuple[str, AgentCapability]],
    managed_capabilities: list[AgentCapability] | None,
    model: ModelInput | None,
    session_id: str,
    principal: str,
    deployment_id: str,
) -> dict[str, object]:
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
    values: dict[str, object] = {
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
    composed: Sequence[tuple[str, AgentCapability]],
    snapshot: ConfigurationSnapshot,
    resources: ResourceSet,
    model: ModelInput | None,
    session_id: str,
    principal: str,
    deployment_id: str,
    deps_type: type[HrnsDeps] | None = None,
) -> tuple[AbstractAgent[Any, Any], list[AgentCapability]]:
    """Construct the native agent and any capabilities that must be applied per run.

    ``deps_type`` is the run's dependency type when hrns owns the agent (spec or plain
    construction). A factory-created agent keeps whatever type it declared, since a
    deployment's own agent may expect its own dependencies.
    """
    capabilities = [capability for _, capability in composed]
    _validate_composed_identities(definition, composed)
    if definition.spec is not None:
        spec: NativeAgentSpec | dict[str, Any] = definition.spec
        if isinstance(spec, BaseModel):
            spec = spec.model_dump(mode="json", exclude_defaults=True)
        declared: list[AgentCapability] = [*definition.fixed_capabilities, *capabilities]
        if deps_type is None:
            agent = Agent.from_spec(spec, model=model, capabilities=declared)
        else:
            agent = Agent.from_spec(spec, model=model, capabilities=declared, deps_type=deps_type)
        return agent, []
    if definition.factory is not None:
        factory = definition.factory
        managed_at_agent = factory_managed_param(factory)
        factory_caps = capabilities if managed_at_agent else None
        kwargs = _factory_kwargs(
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
            raise HrnsError(
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
    declared: list[AgentCapability] = [*definition.fixed_capabilities, *capabilities]
    if deps_type is None:
        agent = Agent(model=model, capabilities=declared)
    else:
        agent = Agent(model=model, capabilities=declared, deps_type=deps_type)
    return agent, []


def _workspace_value(
    definition: AgentDefinition, resources: ResourceSet
) -> ResolvedWorkspace | None:
    """The run workspace, when the definition asked for one.

    Declaration drives injection: a definition that never declares a workspace requirement
    receives no workspace, even if the deployment bound one. A declared-and-bound value is
    passed through :func:`resolve_workspace_value`, so a plain path becomes a local backend
    and a remote or sandboxed backend reaches the run unchanged.
    """
    if not definition.declares(WORKSPACE):
        return None
    value = resources.get(WORKSPACE)
    return resolve_workspace_value(value) if value is not None else None


def _validate_resource_needs(
    definition: AgentDefinition,
    catalog: PolicyCatalog,
    resources: ResourceSet,
) -> None:
    """Reject an enabled binding whose typed resources are undeclared or unbound.

    This is the runtime side of a plugin's ``needs`` declaration: the definition must ask for
    the slot and the deployment must fill it before the factory runs, so the capability can
    read the value without a defensive check of its own.
    """
    for offering in catalog.enabled_ordered():
        for slot in offering.descriptor.needs:
            if not definition.declares(slot):
                raise HrnsError(
                    "INVALID_CONFIGURATION",
                    f"Binding {offering.binding_id!r} (plugin {offering.plugin_id!r}) needs the "
                    f"{slot.kind} resource {slot.name!r}, which definition "
                    f"{definition.agent_id!r} does not declare.",
                    details={
                        "binding_id": offering.binding_id,
                        "plugin_id": offering.plugin_id,
                        "resource": slot.name,
                        "kind": slot.kind,
                    },
                )
            if resources.get(slot) is None:
                raise HrnsError(
                    slot.missing_code,
                    f"Binding {offering.binding_id!r} needs the {slot.kind} resource "
                    f"{slot.name!r}, which this deployment does not bind.",
                    details={
                        "binding_id": offering.binding_id,
                        "resource": slot.name,
                        "kind": slot.kind,
                        "description": slot.description,
                    },
                )


def _session_capability(capability: AgentCapability) -> AbstractCapability[object]:
    """The capability object the ACP session config can hold.

    ``AgentCapability`` also admits a plain callable, which ``Agent.run`` wraps in a
    ``DynamicCapability``. The ACP adapter inspects ``AcpSessionConfig.capabilities``
    before a run exists, so the wrapper has to be applied here rather than left to the run.
    """
    if isinstance(capability, AbstractCapability):
        return capability
    return DynamicCapability[object](capability_func=capability)


async def compose_definition(
    definition: AgentDefinition,
    registry: PluginRegistry,
    *,
    resources: ResourceSet | ResourceBindings | None = None,
    session_id: str,
    principal: str = "local",
    deployment_id: str = "local",
    initial_configuration: Sequence[BindingChange] | None = None,
    saved_bindings: Mapping[str, Mapping[str, JsonValue]] | None = None,
    model: ModelInput | None = None,
    model_settings: ModelSettings | None = None,
    effort: str | None = None,
    effort_support: str | None = None,
    provider_types: Mapping[str, str] | None = None,
    deps: object = None,
    presentation_sink: PresentationSink | None = None,
    client_capabilities: schema.ClientCapabilities | None = None,
    accepts_client_capabilities: bool = False,
) -> Composition:
    """Compose a definition into a native agent and an ACP session configuration.

    ``initial_configuration`` applies once, at session creation. ``saved_bindings`` restores
    the selected bindings of an opened session. The two are mutually exclusive.
    """
    from pydantic_ai_harness.experimental.acp import AcpSessionConfig

    if initial_configuration and saved_bindings:
        raise HrnsError(
            "INVALID_CONFIGURATION",
            "A session composition cannot be both initial and restored.",
        )
    definition = definition.resolve(registry)
    catalog = PolicyCatalog(
        definition,
        registry,
        session_id=session_id,
        principal=principal,
        deployment_id=deployment_id,
        client_capabilities=client_capabilities,
        accepts_client_capabilities=accepts_client_capabilities,
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
    resource_set = resources if isinstance(resources, ResourceSet) else ResourceSet(resources)
    resource_set.validate_requirements(definition.resources)
    _validate_resource_needs(definition, catalog, resource_set)
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
        or normalize_model_usage(resource_set.get(MODEL))
        or normalize_model_usage(definition.model)
    )
    base_settings: ModelSettings = {**(model_settings or {})}
    base_settings.pop("thinking", None)
    effort_state = EffortState(
        effort=effort if effort in (*THINKING_LEVELS, EFFORT_OFF) else None,
        support=effort_support,
        model=selected_model if isinstance(selected_model, str) else None,
        base=base_settings,
        provider_types=provider_types or {},
    )
    if effort_state.effort is not None and effort_state.support is None:
        effort_state.effort = None
    workspace = _workspace_value(definition, resource_set)
    # The run's dependencies: the deployment's own object when it supplied one, otherwise the
    # typed projection of this session's resources. Handing resources to the framework's own
    # injection keeps `ctx.deps` typed for tools, instructions and capabilities.
    run_deps = (
        deps if deps is not None else HrnsDeps.from_resources(resource_set, workspace=workspace)
    )
    deps_type = HrnsDeps if isinstance(run_deps, HrnsDeps) else None
    agent, run_capabilities = await _build_agent(
        definition,
        composed=composed,
        snapshot=snapshot,
        resources=resource_set,
        model=selected_model,
        session_id=session_id,
        principal=principal,
        deployment_id=deployment_id,
        deps_type=deps_type,
    )
    commands = build_command_registry(
        registry=registry,
        enabled=catalog.enabled_ordered(),
        resources=resource_set,
        snapshot=snapshot,
        session_id=session_id,
        principal=principal,
        deployment_id=deployment_id,
        revision=snapshot.selected_revision,
    )
    observer: PresentationObserver | None = None
    if presentation_sink is not None:
        observer = PresentationObserver(session_id=session_id, publish=presentation_sink)
    capabilities: list[AbstractCapability[object]] = [
        _EffortCapability(state=effort_state),
        *([observer] if observer is not None else []),
        *(_session_capability(capability) for capability in run_capabilities),
    ]
    workspace = _workspace_value(definition, resource_set)
    config = AcpSessionConfig(
        deps=run_deps,
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
        commands=commands,
        presentation=observer,
        workspace=workspace,
        deps=run_deps,
    )


def compose_session_config(composition: Composition) -> AcpSessionConfig[object]:
    """Return the adapter-facing run configuration for a composition."""
    return composition.config
