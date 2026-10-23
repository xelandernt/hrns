"""Typed agent definitions.

An :class:`AgentDefinition` is the reusable, versioned description of one agent: its fixed
behavior (an upstream Pydantic AI ``AgentSpec``, or a Python factory) plus the *managed*
capability bindings the runtime composes per turn. Definitions are authored in Python or
declared in a JSON/YAML :class:`DefinitionManifest` whose bindings reference plugins by
``plugin_id``.

Definitions never hold live capability objects: managed capabilities are reconstructed from
the plugin registry's factories for every turn, so "turning off means omitting the
capability" (hooks, instructions, toolsets, everything) and fresh compositions are the
default. Non-serializable callbacks and output classes are Python-only; exporting to a
manifest fails with an actionable list of non-portable fields instead of dropping behavior.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_ai.agent.spec import AgentSpec as NativeAgentSpec
from pydantic_ai.capabilities import AgentCapability

from harness.protocol import BindingId, CapabilityPolicy, HarnessError

from .registry import PluginConfigT, PluginDescriptor
from .resources import SLOTS, ResourceRequirement, ResourceSlot

__all__ = [
    "AGENT_FACTORY_PARAM",
    "AGENT_FACTORY_PARAMS",
    "AgentDefinition",
    "AgentFactory",
    "AgentFactoryContext",
    "AgentSpec",
    "CapabilityFactory",
    "DefinitionBinding",
    "DefinitionManifest",
    "DefinitionManifestBinding",
    "DefinitionManifestResource",
    "MANIFEST_SCHEMA_VERSION",
    "definition_to_manifest",
    "factory_managed_param",
    "load_agent_spec",
    "manifest_from_file",
    "parse_manifest_data",
    "parse_manifest_text",
]

MANIFEST_SCHEMA_VERSION = 1

AgentSpec = NativeAgentSpec
"""Re-exported upstream ``AgentSpec`` for manifest authoring."""

#: Parameter name a :data:`AgentFactory` may declare to receive the turn's managed binding
#: capabilities for binding at ``Agent(...)`` construction time. ``SubAgents(include_self=True)``
#: (a default of ``Coder``) delegates to the agent itself, so it must see everything bound to
#: the ``Agent`` rather than per-run; factories that declare this parameter get that native
#: behavior. Everything else is merged per run via ``agent.run(capabilities=...)``. Either
#: way, every managed binding is added exactly once per run.
AGENT_FACTORY_PARAM = "managed_capabilities"

#: Standard keyword parameters a definition factory may declare.
AGENT_FACTORY_PARAMS = frozenset(
    {
        "resources",
        "active_config",
        "model",
        AGENT_FACTORY_PARAM,
        "context",
        "principal",
        "deployment_id",
    }
)


class AgentFactoryContext(BaseModel):
    """Immutable context handed to a definition factory for one turn."""

    model_config = {"frozen": True}

    session_id: str
    principal: str
    deployment_id: str
    binding_ids: tuple[str, ...]
    """Binding IDs enabled for this turn, in stable topological order."""


@runtime_checkable
class AgentFactory(Protocol):
    """Protocol for definition factories.

    The factory receives whichever keyword parameters from :data:`AGENT_FACTORY_PARAMS` it
    declares in its signature (nothing else is passed). It returns a native Pydantic AI
    ``Agent`` carrying the definition's fixed behavior. Capabilities embedded privately
    inside the factory remain fixed behavior; they are not advertised as toggleable.

    - ``resources``: :class:`~harness.runtime.resources.ResourceSet` of resolved bindings.
    - ``active_config``: the turn's immutable
      :class:`~harness.protocol.ConfigurationSnapshot`.
    - ``model``: resolved model selection (a ``pydantic_ai.models.Model``, a provider
      string, or ``None`` when nothing was bound).
    - ``managed_capabilities``: the turn's enabled managed-binding capabilities, in stable
      topological order; include them in ``Agent(capabilities=[...])``.
    - ``context``: :class:`AgentFactoryContext`.
    - ``principal`` / ``deployment_id``: the session's owner and deployment pin.
    """

    def __call__(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class CapabilityFactory(Protocol):
    """Protocol for plugin capability factories.

    Called with the *validated* configuration object (an empty ``Capability``-style mapping
    dict when the plugin has no ``config_model``) and a
    :class:`~harness.runtime.registry.BindingContext`; returns a native Pydantic AI
    :class:`~pydantic_ai.capabilities.AbstractCapability` (or a ``CapabilityFunc`` callable
    accepted by ``Agent(capabilities=...)``).
    """

    def __call__(self, config: Any, binding: Any) -> Any: ...


class DefinitionBinding(BaseModel):
    """One managed binding declared by an :class:`AgentDefinition`.

    A binding is a *named instance* of a capability in the agent definition with its own
    stable identity, configuration, and policy, so two separately configured MCP connections
    or service accounts remain distinguishable.
    """

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    binding_id: str = Field(min_length=1, max_length=200)
    plugin: PluginDescriptor[Any] | None = None
    """Direct Python plugin descriptor. Mutually exclusive with ``plugin_id`` lookup."""
    plugin_id: str | None = Field(default=None, min_length=1, max_length=200)
    """The registered plugin this binding instantiates when ``plugin`` is omitted."""
    config: dict[str, Any] = Field(default_factory=dict)
    """Initial configuration, validated against the plugin's ``config_model``."""
    enabled: bool = True
    """Whether the binding is enabled by default in new sessions."""
    policy: CapabilityPolicy | None = None
    """Definition-level policy override; plugin defaults apply beneath this."""

    @model_validator(mode="after")
    def _validate_plugin_source(self) -> DefinitionBinding:
        if self.plugin is None and self.plugin_id is None:
            raise ValueError("A definition binding requires either plugin or plugin_id.")
        if self.plugin is not None:
            if self.plugin_id is not None and self.plugin_id != self.plugin.plugin_id:
                raise ValueError(
                    "Definition binding plugin_id must match the supplied plugin descriptor."
                )
            self.plugin_id = self.plugin.plugin_id
        return self

    @classmethod
    def from_plugin(
        cls,
        plugin: PluginDescriptor[PluginConfigT],
        *,
        binding_id: str,
        config: PluginConfigT | Mapping[str, Any] | None = None,
        enabled: bool = True,
        policy: CapabilityPolicy | None = None,
    ) -> DefinitionBinding:
        """Bind a plugin descriptor directly, with a config typed by its model.

        ``config`` accepts the plugin's Pydantic config instance or a mapping. The generic
        ``PluginDescriptor`` parameter is inferred from ``plugin``, so a mismatched model is
        rejected by the type checker before runtime validation.
        """
        if config is None:
            payload: dict[str, Any] = {}
        elif isinstance(config, Mapping):
            payload = dict(config)
        elif isinstance(config, BaseModel):
            payload = config.model_dump(mode="json", exclude_none=False)
        else:  # pragma: no cover - the type checker should reject this first
            raise TypeError("config must be a Pydantic model instance or a mapping")
        return cls(
            binding_id=binding_id,
            plugin=plugin,
            plugin_id=plugin.plugin_id,
            config=payload,
            enabled=enabled,
            policy=policy,
        )

    @property
    def resolved_plugin_id(self) -> str:
        """Stable plugin id, whether the binding was declared inline or by registry id."""
        if self.plugin_id is not None:
            return self.plugin_id
        if self.plugin is not None:
            return self.plugin.plugin_id
        raise ValueError("Definition binding has no plugin source.")


class DefinitionManifestBinding(BaseModel):
    """Manifest (serializable) representation of a :class:`DefinitionBinding`."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    binding_id: BindingId
    plugin_id: BindingId
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    policy: CapabilityPolicy | None = None
    """Definition-level policy override; never silently dropped on export."""


class DefinitionManifestResource(BaseModel):
    """Manifest (serializable) representation of a resource requirement."""

    model_config = {"extra": "forbid"}

    name: str
    kind: Literal["model", "workspace", "memory", "store", "service", "custom"] = "custom"
    description: str = ""
    required: bool = True


class DefinitionManifest(BaseModel):
    """The project envelope around an upstream ``AgentSpec``.

    ``spec`` capabilities are fixed behavior; a capability must not appear both as a fixed
    spec contribution and a managed binding — manifest loading enforces this. Manifest-only
    definitions may be standalone files, but their referenced plugins must be installed.
    """

    model_config = {"extra": "forbid", "populate_by_name": True}

    schema_version: Literal[1] = 1
    agent_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1)
    description: str = ""
    definition_version: str = Field(default="1.0.0", min_length=1, max_length=100)
    spec: dict[str, Any] | None = None
    """Upstream ``AgentSpec`` fields, validated by :func:`load_agent_spec`."""
    bindings: list[DefinitionManifestBinding] = Field(default_factory=list)
    resources: list[DefinitionManifestResource] = Field(default_factory=list)
    model: str | None = None
    """Fallback model when neither the spec nor the host resources provide one."""
    interaction_features: list[str] = Field(default_factory=list)

    def to_definition(self) -> AgentDefinition:
        """Build the runtime definition this manifest describes."""
        return AgentDefinition(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            definition_version=self.definition_version,
            spec=self.spec,
            fixed_capabilities=[],
            bindings=[
                DefinitionBinding(
                    binding_id=binding.binding_id,
                    plugin_id=binding.plugin_id,
                    config=dict(binding.config),
                    enabled=binding.enabled,
                    policy=binding.policy,
                )
                for binding in self.bindings
            ],
            resources=[
                ResourceRequirement(
                    name=resource.name,
                    kind=resource.kind,
                    description=resource.description,
                    required=resource.required,
                )
                for resource in self.resources
            ],
            model=self.model,
            interaction_features=list(self.interaction_features),
        )


class AgentDefinition(BaseModel):
    """Typed, versioned agent definition.

    Exactly one of ``spec`` (manifest path) or ``factory`` (Python path) drives the base
    agent, except for the common degenerate case of neither: then ``fixed_capabilities``
    and ``bindings`` alone define the agent.
    """

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True, "validate_assignment": True}

    agent_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1)
    description: str = ""
    definition_version: str = Field(default="1.0.0", min_length=1, max_length=100)

    spec: dict[str, Any] | NativeAgentSpec | None = None
    """Upstream ``AgentSpec`` (dict form is validated at load time)."""
    factory: Callable[..., Any] | None = None
    """Python factory returning a native Pydantic AI ``Agent``."""
    fixed_capabilities: list[AgentCapability] = Field(default_factory=list)
    """Native capabilities bound to the agent every turn without offered management."""

    bindings: list[DefinitionBinding] = Field(default_factory=list)
    resources: list[ResourceRequirement] = Field(default_factory=list)
    model: str | None = None
    """Definition-level fallback model."""
    interaction_features: list[str] = Field(default_factory=list)
    """ACP interaction feature identifiers (host-owned list)."""

    def snapshot(self) -> AgentDefinition:
        """Copy editable configuration while retaining native host objects."""
        clone = self.model_copy(deep=False)
        clone.bindings = [binding.model_copy(deep=True) for binding in self.bindings]
        clone.resources = [requirement.model_copy(deep=True) for requirement in self.resources]
        clone.interaction_features = list(self.interaction_features)
        clone.fixed_capabilities = list(self.fixed_capabilities)
        if isinstance(self.spec, BaseModel):
            clone.spec = self.spec.model_copy(deep=True)
        elif isinstance(self.spec, dict):
            clone.spec = copy.deepcopy(self.spec)
        return clone

    # --- validation -------------------------------------------------------

    @field_validator("spec", mode="before")
    def _coerce_spec(cls, value: Any) -> Any:
        if (
            value is None
            or isinstance(value, dict | NativeAgentSpec)
            or isinstance(value, BaseModel)
        ):
            return value
        raise ValueError(f"Unsupported definition spec of type {type(value).__name__!r}")

    @model_validator(mode="after")
    def _validate_agent_shape(self) -> AgentDefinition:
        if self.spec is not None and self.factory is not None:
            raise ValueError(
                "AgentDefinition 'spec' (manifest path) and 'factory' (Python path) are "
                "mutually exclusive; the fixed agent behavior must have one owner."
            )
        if isinstance(self.spec, dict):
            try:  # Fail fast on malformed specs so they never surface as turn-time surprises.
                NativeAgentSpec.model_validate(self.spec)
            except ValidationError as exc:
                raise ValueError(
                    f"invalid AgentSpec on definition {self.agent_id!r}: {exc.errors()[:2]}"
                ) from exc
        ids = [binding.binding_id for binding in self.bindings]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate binding IDs in definition {self.agent_id!r}")
        seen: set[str] = set()
        for requirement in self.resources:
            if requirement.name in seen:
                raise ValueError(
                    f"duplicate resource requirement {requirement.name!r} on {self.agent_id!r}"
                )
            seen.add(requirement.name)
        return self

    # --- runtime accessors ------------------------------------------------

    @property
    def version(self) -> str:
        """Alias for :attr:`definition_version`."""
        return self.definition_version

    def binding(self, binding_id: str) -> DefinitionBinding | None:
        """Return the definition binding with this id, or ``None``."""
        for binding in self.bindings:
            if binding.binding_id == binding_id:
                return binding
        return None

    def resource_requirement(self, name: str) -> ResourceRequirement | None:
        """Return the named resource requirement, or ``None``."""
        for requirement in self.resources:
            if requirement.name == name:
                return requirement
        return None

    def declares(self, slot: ResourceSlot[Any]) -> bool:
        """Whether any requirement of this definition addresses ``slot``.

        A definition that enables a plugin declaring ``needs`` must declare the matching slot;
        composition checks this before a turn starts, so a capability never runs against a
        resource its definition never asked for.
        """
        return any(SLOTS.of(requirement).name == slot.name for requirement in self.resources)

    def to_manifest(self) -> DefinitionManifest:
        """Export to a serializable manifest, failing loudly on non-portable fields."""
        non_portable: list[str] = []
        if self.factory is not None:
            non_portable.append("factory")
        if self.fixed_capabilities:
            non_portable.append("fixed_capabilities")
        if any(binding.plugin is not None for binding in self.bindings):
            non_portable.append("bindings[].plugin")
        if non_portable:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "AgentDefinition has fields that cannot round-trip through a manifest: "
                + ", ".join(non_portable)
                + "; these are Python-only behavior.",
                details={"non_portable": non_portable},
            )
        spec_value: dict[str, Any] | None
        if isinstance(self.spec, NativeAgentSpec):
            spec_value = self.spec.model_dump(mode="json", exclude_defaults=True)
        else:
            spec_value = dict(self.spec) if self.spec else None
        return DefinitionManifest(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            definition_version=self.definition_version,
            spec=spec_value,
            bindings=[
                DefinitionManifestBinding(
                    binding_id=binding.binding_id,
                    plugin_id=binding.resolved_plugin_id,
                    config=dict(binding.config),
                    enabled=binding.enabled,
                    policy=binding.policy,
                )
                for binding in self.bindings
            ],
            resources=[
                DefinitionManifestResource(
                    name=requirement.name,
                    kind=requirement.kind,
                    description=requirement.description,
                    required=requirement.required,
                )
                for requirement in self.resources
            ],
            model=self.model,
            interaction_features=list(self.interaction_features),
        )


def parse_manifest_data(data: Mapping[str, Any], *, source: str = "manifest") -> DefinitionManifest:
    """Validate a raw manifest mapping into :class:`DefinitionManifest`."""
    try:
        manifest = DefinitionManifest.model_validate(dict(data))
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        raise HarnessError(
            "INVALID_CONFIGURATION",
            f"{source} is not a valid agent definition manifest: {errors}",
            details={"source": source, "errors": exc.errors()[:10]},
        ) from exc
    if manifest.spec is not None:
        load_agent_spec(manifest.spec, source=source)
    return manifest


def parse_manifest_text(
    text: str,
    *,
    fmt: Literal["yaml", "json"] | None = None,
    source: str = "manifest",
) -> DefinitionManifest:
    """Parse YAML/JSON manifest text; format inferred from the ``source`` suffix when omitted."""
    resolved = fmt
    if resolved is None:
        lowered = source.lower()
        if lowered.endswith(".json"):
            resolved = "json"
        elif lowered.endswith((".yaml", ".yml")):
            resolved = "yaml"
        else:
            resolved = "yaml"
    if resolved == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HarnessError(
                "INVALID_CONFIGURATION", f"{source} is not valid JSON: {exc}"
            ) from exc
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise HarnessError(
                "INVALID_CONFIGURATION", f"{source} is not valid YAML: {exc}"
            ) from exc
    if not isinstance(data, dict):
        raise HarnessError(
            "INVALID_CONFIGURATION", f"{source} must parse to an object, got {type(data).__name__}"
        )
    return parse_manifest_data(data, source=source)


def manifest_from_file(
    path: str | Path, *, fmt: Literal["yaml", "json"] | None = None
) -> DefinitionManifest:
    """Load a manifest from a file, inferring format from the extension."""
    source = str(path)
    return parse_manifest_text(Path(path).read_text(encoding="utf-8"), fmt=fmt, source=source)


def load_agent_spec(spec: Mapping[str, Any], *, source: str = "manifest spec") -> dict[str, Any]:
    """Validate a mapping against the upstream ``AgentSpec`` model, normalized for ``Agent.from_spec``."""
    try:
        return NativeAgentSpec.model_validate(dict(spec)).model_dump(
            mode="json", exclude_defaults=True
        )
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:3]
        )
        raise HarnessError(
            "INVALID_CONFIGURATION", f"invalid AgentSpec in {source}: {errors}"
        ) from exc


def definition_to_manifest(definition: AgentDefinition) -> DefinitionManifest:
    """Export a definition to a :class:`DefinitionManifest`."""
    return definition.to_manifest()


def factory_managed_param(factory: Callable[..., Any]) -> bool:
    """Whether a definition factory declares the ``managed_capabilities`` parameter.

    Factories that accept ``**kwargs`` or a parameter with this name get the turn's enabled
    managed capabilities bound at ``Agent(...)`` construction; all other factories receive
    them per run via ``agent.run(capabilities=...)``.
    """
    import inspect

    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):  # builtins without introspectable signatures
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD or parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            if parameter.name == AGENT_FACTORY_PARAM:
                return True
    return False


def factory_declared_params(factory: Callable[..., Any]) -> tuple[str, ...]:
    """Names of the standard parameters a factory declares (used to build its call kwargs)."""
    import inspect

    names = {
        parameter.name
        for parameter in inspect.signature(factory).parameters.values()
        if parameter.name in AGENT_FACTORY_PARAMS
    }
    return tuple(sorted(names))
