"""Capability policy: offering state, atomic transactions, and turn composition.

A session maintains a monotonically increasing *selected* configuration revision; every
turn captures one immutable *active* revision before execution starts. A requested change
is validated against host policy, dependencies, schemas, and resource requirements as one
atomic transaction: either every change applies or none does.

"Turning off" means *omitting* the capability from the next turn's agent: enabled bindings
are resolved into a stable topological order and instantiated fresh from plugin factories,
so tools, instructions, hooks, and model settings of disabled bindings are all absent.
Requests that already match the selected state are idempotent no-ops; toggle
permissions apply only to actual transitions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic_ai.capabilities import AgentCapability

from harness.protocol import (
    AddChange,
    BindingChange,
    BindingState,
    CapabilityPolicy,
    ConfigurationSnapshot,
    ConfigureChange,
    EnableChange,
    HarnessError,
)

from ..registry import BindingContext, PluginDescriptor, PluginRegistry
from ..resources import ResourceSet
from .binding import (
    BindingOffering,
    ChangeOutcome,
    jsonable_config,
)
from .validation import (
    check_config_schema,
    effective_policy,
    host_editable_fields,
    order_enabled_offerings,
    validate_add,
    validate_all_configs,
    validate_configure,
    validate_enable,
    validate_graph_references,
    validate_graph_states,
    validate_initial_state,
)

if TYPE_CHECKING:
    from ..definitions import AgentDefinition

__all__ = ["PolicyCatalog"]


class PolicyCatalog:
    """Session-scoped capability management state.

    Operates on the session's offered bindings plus the plugin registry; raises
    :class:`~harness.protocol.HarnessError` for every policy violation so the session and
    host layers can forward structured codes unchanged.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        registry: PluginRegistry,
        *,
        session_id: str,
        principal: str,
        deployment_id: str,
        validate_defaults: bool = True,
    ) -> None:
        self.definition = definition
        self.registry = registry
        self.session_id = session_id
        self.principal = principal
        self.deployment_id = deployment_id
        self.offerings: dict[str, BindingOffering] = {}
        self.policy_revision = 1
        self._sequence = 0
        self._build_offerings(validate_defaults=validate_defaults)

    # --- construction -------------------------------------------------

    def _build_offerings(self, *, validate_defaults: bool = True) -> None:
        missing_plugins: list[str] = []
        for binding in self.definition.bindings:
            descriptor = binding.plugin or self.registry.lookup(binding.plugin_id)
            if descriptor is None:
                missing_plugins.append(binding.resolved_plugin_id)
                continue
            self._sequence += 1
            self.offerings[binding.binding_id] = BindingOffering(
                binding_id=binding.binding_id,
                plugin_id=descriptor.plugin_id,
                descriptor=descriptor,
                policy=effective_policy(descriptor, binding.policy),
                enabled=binding.enabled,
                config=dict(binding.config),
                sequence=self._sequence,
            )
        if missing_plugins:
            raise HarnessError(
                "DEPLOYMENT_UNAVAILABLE",
                "Definition bindings reference plugins that are not registered on this host: "
                + ", ".join(sorted(set(missing_plugins))),
                details={"plugins": sorted(set(missing_plugins))},
            )
        validate_graph_references(self)
        if validate_defaults:
            validate_all_configs(self)
            validate_initial_state(self)

    # --- persistence view ---------------------------------------------

    def export_bindings(self) -> dict[str, dict[str, Any]]:
        """Full selected state for persistence (includes plugin ids for runtime-added bindings)."""
        payload: dict[str, dict[str, Any]] = {}
        for binding_id, offering in self.offerings.items():
            payload[binding_id] = {
                "enabled": offering.enabled,
                "config": jsonable_config(offering.config),
                "plugin_id": offering.plugin_id,
            }
        return payload

    def restore_bindings(self, payload: Mapping[str, Mapping[str, Any]]) -> None:
        """Rehydrate selected state (including runtime-added offerings) from persistence.

        Current host policy is rechecked: a saved selection that now enables an unavailable
        binding, violates dependencies/conflicts, or no longer matches a configuration
        schema is rejected rather than silently carried forward. The caller must correct the
        saved selection (or the deployment) before the session can run.
        """
        for binding_id, state in payload.items():
            enabled = bool(state.get("enabled", False))
            config = dict(state.get("config") or {})
            offering = self.offerings.get(binding_id)
            if offering is not None:
                offering.enabled = enabled
                offering.config = config
                continue
            plugin_id = state.get("plugin_id")
            if not plugin_id:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Persisted binding {binding_id!r} has no plugin id and is not part of the definition.",
                    details={"binding_id": binding_id},
                )
            try:
                descriptor = self.registry.require(str(plugin_id))
            except HarnessError as exc:
                if exc.code != "CAPABILITY_UNAVAILABLE":
                    raise
                raise HarnessError(
                    "DEPLOYMENT_UNAVAILABLE",
                    f"Persisted binding {binding_id!r} depends on plugin {plugin_id!r}, "
                    "which is not installed on this host.",
                    details={"binding_id": binding_id, "plugin_id": str(plugin_id)},
                ) from exc
            self.add_offering(binding_id, descriptor, enabled, config)
        self.validate_selected_state()

    def validate_selected_state(self) -> None:
        """Reject a selected composition that is invalid under current host policy.

        Used after restoring a saved revision, where deployment defaults may have changed
        since the session was created. Unavailable enabled bindings and broken dependency
        graphs must block new work; the runtime never quietly grants a revoked capability.
        """
        validate_all_configs(self)
        enabled = [offering for offering in self.offerings.values() if offering.enabled]
        for offering in enabled:
            if not offering.available:
                raise HarnessError(
                    "CAPABILITY_UNAVAILABLE",
                    f"Binding {offering.binding_id!r} is enabled by the saved configuration "
                    f"but is unavailable on this host: {offering.availability_reason}",
                    details={
                        "binding_id": offering.binding_id,
                        "reason": offering.availability_reason,
                        "required_changes": [
                            {"type": "enable", "bindingId": offering.binding_id, "enabled": False}
                        ],
                    },
                )
        work = {
            binding_id: (offering.enabled, dict(offering.config))
            for binding_id, offering in self.offerings.items()
        }
        validate_graph_states(self, work)

    # --- snapshots ----------------------------------------------------

    def ordered_offerings(self) -> list[BindingOffering]:
        """Definition order, then creation order, then binding id for determinism."""
        return sorted(self.offerings.values(), key=lambda o: (o.sequence, o.binding_id))

    def selected_bindings(self) -> dict[str, BindingState]:
        return {binding_id: offering.state() for binding_id, offering in self.offerings.items()}

    def application_status(
        self, active_revision: int | None, selected_revision: int
    ) -> Literal["applied", "pending"]:
        if active_revision is None or selected_revision == active_revision:
            return "applied"
        return "pending"

    def snapshot(
        self,
        *,
        selected_revision: int,
        active_revision: int | None,
        application_status: Literal["applied", "failed", "pending"] | None = None,
    ) -> ConfigurationSnapshot:
        """Selected configuration snapshot.

        ``bindings`` always carries the *selected* state, matching ACP
        ``configOptions.currentValue``; ``active_revision`` plus descriptors'
        ``active_enabled`` expose what the running turn keeps using.
        """
        status = application_status or self.application_status(active_revision, selected_revision)
        return ConfigurationSnapshot(
            session_id=self.session_id,
            selected_revision=selected_revision,
            active_revision=active_revision,
            policy_revision=self.policy_revision,
            application_status=status,
            bindings=self.selected_bindings(),
        )

    def descriptors(
        self,
        *,
        active_revision: int | None = None,
        selected_revision: int | None = None,
    ) -> list[Any]:
        """Binding descriptors for the extension's capability listing.

        The revision arguments are advisory: descriptors always report the live selected
        state plus ``active_enabled`` from the turn's captured revision.
        """
        del active_revision, selected_revision
        from harness.protocol import BindingDescriptor

        result: list[BindingDescriptor] = []
        for offering in self.ordered_offerings():
            active_enabled: bool | None = None
            result.append(
                BindingDescriptor(
                    binding_id=offering.binding_id,
                    plugin_id=offering.plugin_id or None,
                    name=offering.binding_id,
                    description=offering.descriptor.description,
                    version=offering.descriptor.version,
                    config_schema=offering.descriptor.config_schema(),
                    config=jsonable_config(offering.config) if offering.config else {},
                    defaults=jsonable_config(offering.descriptor.config_defaults()),
                    enabled=offering.enabled,
                    active_enabled=active_enabled,
                    policy=offering.policy,
                    state_mode=offering.descriptor.state_mode,
                    requires=offering.requirements,
                    needs=offering.descriptor.resource_needs(),
                    conflicts=offering.conflicts,
                    availability_reason=offering.availability_reason,
                    presentation=list(offering.descriptor.presentation),
                )
            )
        return result

    # --- atomic transactions ------------------------------------------

    def apply_changes(
        self,
        changes: Sequence[BindingChange],
        *,
        initial_configuration: bool = False,
    ) -> ChangeOutcome:
        """Validate an atomic change list and return the prospective outcome.

        No state is mutated. Raises :class:`~harness.protocol.HarnessError` on the first
        violation (policy, schema, dependency, conflict, lifecycle); the caller persists
        the outcome before committing it to live state.
        """
        if not changes:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "A configuration transaction must contain at least one change.",
            )
        duplicate_ids = [
            change.binding_id
            for change in changes
            if sum(1 for c in changes if c.binding_id == change.binding_id) > 1
        ]
        if duplicate_ids:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"A transaction may reference each binding at most once; repeated: {sorted(set(duplicate_ids))}",
                details={"binding_ids": sorted(set(duplicate_ids))},
            )
        work: dict[str, tuple[bool, dict[str, Any]]] = {
            binding_id: (offering.enabled, dict(offering.config))
            for binding_id, offering in self.offerings.items()
        }
        added_descriptors: dict[str, PluginDescriptor] = {}
        changed = False
        for change in changes:
            if isinstance(change, EnableChange):
                changed |= validate_enable(self, change, work, initial_configuration)
            elif isinstance(change, ConfigureChange):
                changed |= validate_configure(self, change, work, initial_configuration)
            elif isinstance(change, AddChange):
                changed |= validate_add(
                    self,
                    change,
                    work,
                    added_descriptors,
                    initial_configuration=initial_configuration,
                )
            else:  # pragma: no cover - the discriminated union prevents this
                raise HarnessError(
                    "INVALID_CONFIGURATION", f"Unsupported change type {type(change).__name__}"
                )
        validate_graph_states(self, work, added_descriptors)
        for binding_id, (_, config) in work.items():
            descriptor = (
                added_descriptors[binding_id]
                if binding_id in added_descriptors
                else self.offerings[binding_id].descriptor
            )
            check_config_schema(self, descriptor, binding_id, config)
        added = [
            (binding_id, descriptor.plugin_id, work[binding_id][0], work[binding_id][1])
            for binding_id, descriptor in added_descriptors.items()
        ]
        return ChangeOutcome(states=work, added=added, changed=changed, noop=not changed)

    # --- dependency graph and conflicts -------------------------------

    # --- commit -------------------------------------------------------

    def commit(self, outcome: ChangeOutcome) -> None:
        """Apply a validated transaction to live offering state (after persistence)."""
        for binding_id, plugin_id, enabled, config in outcome.added:
            self.add_offering(binding_id, self.registry.require(plugin_id), enabled, config)
        for binding_id, (enabled, config) in outcome.states.items():
            offering = self.offerings.get(binding_id)
            if offering is None:
                continue
            offering.enabled = enabled
            offering.config = dict(config)

    def add_offering(
        self,
        binding_id: str,
        descriptor: PluginDescriptor,
        enabled: bool,
        config: dict[str, Any],
    ) -> BindingOffering:
        """Register a runtime-added offering with host-assigned policy."""
        policy = CapabilityPolicy(
            available=descriptor.default_policy.available
            and descriptor.compatibility.runtime_compatible,
            enabled_by_default=False,
            client_can_toggle=True,
            editable_fields=host_editable_fields(descriptor),
            # Preserve the plugin's lifecycle; a host cannot promote a new_session
            # integration into a safe next-turn toggle merely by adding it.
            change_mode=descriptor.default_policy.change_mode,
        )
        self._sequence += 1
        offering = BindingOffering(
            binding_id=binding_id,
            plugin_id=descriptor.plugin_id,
            descriptor=descriptor,
            policy=policy,
            enabled=enabled,
            config=dict(config),
            session_added=True,
            sequence=self._sequence,
        )
        self.offerings[binding_id] = offering
        return offering

    # --- composition --------------------------------------------------

    def enabled_ordered(self) -> list[BindingOffering]:
        """Enabled offerings in stable topological order (dependencies first)."""
        return order_enabled_offerings(
            self.ordered_offerings(),
            [offering.binding_id for offering in self.ordered_offerings() if offering.enabled],
        )

    def instantiate(
        self,
        offering: BindingOffering,
        *,
        resources: ResourceSet,
        active_configuration: ConfigurationSnapshot | None = None,
        agent_context: Any = None,
    ) -> AgentCapability:
        """Create one binding's native capability via its plugin factory."""
        context = BindingContext(
            binding_id=offering.binding_id,
            plugin_id=offering.plugin_id,
            session_id=self.session_id,
            principal=self.principal,
            deployment_id=self.deployment_id,
            binding_config=jsonable_config(offering.config),
            resources=resources,
            active_configuration=active_configuration,
            agent_context=agent_context,
        )
        return self.registry.instantiate(offering.descriptor, offering.config, context)

    def compose(
        self,
        *,
        resources: ResourceSet,
        active_configuration: ConfigurationSnapshot | None = None,
        agent_context: Any = None,
    ) -> list[tuple[str, AgentCapability]]:
        """Instantiate every enabled binding, in stable order, exactly once."""
        composed: list[tuple[str, AgentCapability]] = []
        for offering in self.enabled_ordered():
            capability = self.instantiate(
                offering,
                resources=resources,
                active_configuration=active_configuration,
                agent_context=agent_context,
            )
            composed.append((offering.binding_id, capability))
        return composed
