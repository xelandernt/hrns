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

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic_ai.capabilities import AgentCapability
from pydantic_core import to_jsonable_python

from harness.protocol import (
    AddChange,
    BindingChange,
    BindingState,
    CapabilityPolicy,
    ConfigurationSnapshot,
    ConfigureChange,
    EnableChange,
    HarnessError,
    canonical_hash,
)

from .registry import BindingContext, PluginDescriptor, PluginRegistry
from .resources import ResourceSet

if TYPE_CHECKING:
    from .definitions import AgentDefinition

__all__ = [
    "BindingOffering",
    "PolicyCatalog",
    "changes_payload_hash",
    "configuration_payload_hash",
    "is_noop_change",
    "order_enabled_offerings",
    "payload_hash_matches",
]


@dataclass
class BindingOffering:
    """Live policy state for one offered binding in a session.

    ``enabled``/``config`` are the *selected* state, mutated only through accepted atomic
    transactions. The static parts come from the definition binding and its plugin
    descriptor. ``policy`` is the effective policy after precedence: plugin defaults,
    then definition values, constrained by host policy.
    """

    binding_id: str
    plugin_id: str
    descriptor: PluginDescriptor
    policy: CapabilityPolicy
    enabled: bool
    config: dict[str, Any]
    session_added: bool = False
    """Binding created at runtime through an ``add`` change (host-authorized)."""
    sequence: int = 0
    """Stable definition-then-creation ordering index for topological ties."""

    @property
    def change_mode(self) -> str:
        return self.policy.change_mode

    @property
    def state_mode(self) -> str:
        return self.descriptor.state_mode

    @property
    def requirements(self) -> list[str]:
        return list(self.descriptor.requirements)

    @property
    def conflicts(self) -> list[str]:
        return list(self.descriptor.conflicts)

    @property
    def available(self) -> bool:
        return self.policy.available and self.descriptor.compatibility.runtime_compatible

    @property
    def availability_reason(self) -> str | None:
        if not self.policy.available:
            return "host policy marks this binding unavailable in this deployment"
        if not self.descriptor.compatibility.runtime_compatible:
            return "upstream compatibility constraints do not hold on this host"
        return None

    def state(self) -> BindingState:
        return BindingState(enabled=self.enabled, config=jsonable_config(self.config))

    def with_state(self, enabled: bool, config: Mapping[str, Any]) -> BindingOffering:
        return BindingOffering(
            binding_id=self.binding_id,
            plugin_id=self.plugin_id,
            descriptor=self.descriptor,
            policy=self.policy,
            enabled=enabled,
            config=dict(config),
            session_added=self.session_added,
            sequence=self.sequence,
        )


def jsonable_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return to_jsonable_python(dict(config))  # type: ignore[return-value]


def changes_payload_hash(changes: Sequence[BindingChange]) -> str:
    """Canonical payload hash used for idempotent retry identity."""
    payload = [
        change.model_dump(mode="json", by_alias=True, exclude_none=True) for change in changes
    ]
    try:
        return canonical_hash(payload)
    except (TypeError, ValueError) as exc:
        raise HarnessError(
            "INVALID_CONFIGURATION",
            "Configuration values must be finite, JSON-serializable values.",
            details={"reason": type(exc).__name__},
        ) from exc


def configuration_payload_hash(
    session_id: str,
    request_id: str,
    expected_revision: int | None,
    changes: Sequence[BindingChange],
) -> str:
    """Canonical identity of one complete configuration transaction.

    The body (changes) is the legacy canonical payload hash. The expected revision is
    appended as a suffix so a retry that omits it (``None`` means "skip the revision
    check") can still be recognised as the same completed transaction. ``session_id`` and
    ``request_id`` are already part of the transaction key in the store.

    Reusing a request id with changed changes, a different *explicit* expected revision,
    or a different session is rejected (CONCEPT §10.3).
    """
    del session_id, request_id  # already the transaction key
    expected = "*" if expected_revision is None else str(expected_revision)
    return f"{changes_payload_hash(changes)}|{expected}"


def payload_hash_matches(stored: str, incoming: str) -> bool:
    """Whether a retry payload matches a stored transaction identity.

    New hashes carry an ``expectedRevision`` suffix. ``*`` (an omitted expected revision)
    matches any committed expected revision; legacy hashes without a suffix match any
    expected revision for backward compatibility with stores written before the suffix.
    """
    if stored == incoming:
        return True
    stored_base, stored_sep, stored_expected = stored.partition("|")
    incoming_base, incoming_sep, incoming_expected = incoming.partition("|")
    if stored_base != incoming_base:
        return False
    if not stored_sep or not incoming_sep:
        return True
    return stored_expected == incoming_expected or incoming_expected == "*"


def _path_value(config: Mapping[str, Any], path: str) -> Any:
    value: Any = config
    for segment in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
    return value


def _has_path(config: Mapping[str, Any], path: str) -> bool:
    value: Any = config
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return False
        value = value[segment]
    return True


def _set_path(config: dict[str, Any], path: str, value: Any) -> None:
    segments = path.split(".")
    root: dict[str, Any] = config
    for segment in segments[:-1]:
        nested = root.get(segment)
        if not isinstance(nested, dict):
            nested = {}
            root[segment] = nested
        root = nested
    root[segments[-1]] = value


def is_noop_change(change: BindingChange, current: Mapping[str, BindingOffering]) -> bool:
    """Whether one change already matches the selected state (toggle permissions don't apply)."""
    offering = current.get(change.binding_id)
    if isinstance(change, EnableChange):
        return offering is not None and change.enabled == offering.enabled
    if isinstance(change, ConfigureChange):
        if offering is None:
            return False
        return all(
            _has_path(offering.config, path) and _path_value(offering.config, path) == value
            for path, value in change.fields.items()
            if not isinstance(value, float) or value == value  # NaN never equals itself
        )
    return False  # 'add' always has an effect


@dataclass
class ChangeOutcome:
    """The validated projection of one atomic transaction (not yet committed)."""

    states: dict[str, tuple[bool, dict[str, Any]]]
    """Final selected state for every offered binding, including additions."""

    added: list[tuple[str, str, bool, dict[str, Any]]]
    """``(binding_id, plugin_id, enabled, config)`` created by this transaction."""

    changed: bool
    """Whether any change actually altered the selected state."""

    noop: bool
    """All changes were idempotent no-ops against the selected state."""


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
        self._active_bindings: dict[str, dict[str, Any]] | None = None
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
                policy=self._effective_policy(descriptor, binding.policy),
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
        self._validate_graph_references()
        if validate_defaults:
            self._validate_all_configs()
            self._validate_initial_state()

    def _validate_initial_state(self) -> None:
        """Defaults must form a legal deployment composition (fail at session open)."""
        enabled = [o for o in self.offerings.values() if o.enabled]
        for offering in enabled:
            if not offering.available:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Binding {offering.binding_id!r} is enabled by default but unavailable: "
                    f"{offering.availability_reason}.",
                    details={"binding_id": offering.binding_id},
                )
        enabled_ids = {o.binding_id for o in enabled}
        for offering in enabled:
            missing = [
                req
                for req in offering.requirements
                if req in self.offerings and req not in enabled_ids
            ]
            if missing:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Binding {offering.binding_id!r} is enabled by default but its requirement(s) "
                    f"{missing} are disabled by default.",
                    details={"binding_id": offering.binding_id, "missing": missing},
                )
            for conflict in offering.conflicts:
                if conflict in enabled_ids:
                    raise HarnessError(
                        "INVALID_CONFIGURATION",
                        f"Default configuration enables conflicting bindings {offering.binding_id!r} "
                        f"and {conflict!r}.",
                        details={"bindings": sorted([offering.binding_id, conflict])},
                    )
        try:
            order_enabled_offerings(self.ordered_offerings(), sorted(enabled_ids))
        except HarnessError as exc:
            if exc.code == "DEPENDENCY_CONFLICT":
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    "Default configuration has a requirement cycle: " + exc.message,
                    details=exc.details,
                ) from exc
            raise

    @staticmethod
    def _effective_policy(
        descriptor: PluginDescriptor, override: CapabilityPolicy | None
    ) -> CapabilityPolicy:
        policy = descriptor.default_policy.model_copy(deep=True)
        if override is not None:
            for name in override.model_fields_set:
                value = getattr(override, name)
                if name == "available":
                    # An agent definition can narrow host availability, never grant it.
                    policy.available = bool(policy.available and value)
                elif name == "editable_fields":
                    # A definition may hide fields the host exposes, not expose new ones.
                    policy.editable_fields = set(policy.editable_fields) & set(value)
                elif name == "change_mode":
                    # The stricter lifecycle wins; a definition cannot promote a
                    # new_session integration into a next-turn hot swap.
                    if "new_session" in {policy.change_mode, value}:
                        policy.change_mode = "new_session"
                    else:
                        policy.change_mode = "next_turn"
                else:
                    setattr(policy, name, value)
        return policy

    def _validate_graph_references(self) -> None:
        for binding_id, offering in self.offerings.items():
            for requirement in offering.requirements:
                if (
                    requirement in self.offerings
                    or self.definition.resource_requirement(requirement) is not None
                ):
                    continue
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Binding {binding_id!r} requires {requirement!r}, which is neither an offered binding "
                    "nor a resource of this definition.",
                    details={"binding_id": binding_id, "requirement": requirement},
                )
            for conflict in offering.conflicts:
                if conflict not in self.offerings:
                    raise HarnessError(
                        "INVALID_CONFIGURATION",
                        f"Binding {binding_id!r} conflicts with {conflict!r}, which is not an offered binding.",
                        details={"binding_id": binding_id, "conflict": conflict},
                    )

    def _validate_all_configs(self) -> None:
        for offering in self.offerings.values():
            self._check_config_schema(offering.descriptor, offering.binding_id, offering.config)

    def _check_config_schema(
        self,
        descriptor: PluginDescriptor,
        binding_id: str,
        config: Mapping[str, Any],
    ) -> None:
        if descriptor.config_model is None or not config:
            return
        try:
            descriptor.config_model.model_validate(dict(config))
        except Exception as exc:
            errors = simple_errors(exc)
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Configuration for binding {binding_id!r} (plugin {descriptor.plugin_id!r}) is invalid: "
                f"{errors}",
                details={
                    "binding_id": binding_id,
                    "plugin_id": descriptor.plugin_id,
                    "errors": errors,
                },
            ) from exc

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
        self._validate_all_configs()
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
        self._validate_graph_states(work)

    def active_bindings_view(self) -> dict[str, dict[str, Any]]:
        """The binding dict shown for the current active revision."""
        return self.export_bindings()

    # --- snapshots ----------------------------------------------------

    def ordered_offerings(self) -> list[BindingOffering]:
        """Definition order, then creation order, then binding id for determinism."""
        return sorted(self.offerings.values(), key=lambda o: (o.sequence, o.binding_id))

    def selected_bindings(self) -> dict[str, BindingState]:
        return {binding_id: offering.state() for binding_id, offering in self.offerings.items()}

    def set_active_bindings(self, bindings: dict[str, dict[str, Any]] | None) -> None:
        """Record the selected-binding dict of the currently active revision."""
        self._active_bindings = bindings

    def _active_state_for(self, binding_id: str, fallback: BindingState) -> BindingState:
        if self._active_bindings is None or binding_id not in self._active_bindings:
            return fallback
        return BindingState(**self._active_bindings[binding_id])

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
            if self._active_bindings is not None and offering.binding_id in self._active_bindings:
                active_state = self._active_bindings[offering.binding_id]
                active_enabled = bool(active_state.get("enabled", offering.enabled))
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
                changed |= self._validate_enable(change, work, initial_configuration)
            elif isinstance(change, ConfigureChange):
                changed |= self._validate_configure(change, work, initial_configuration)
            elif isinstance(change, AddChange):
                changed |= self._validate_add(
                    change,
                    work,
                    added_descriptors,
                    initial_configuration=initial_configuration,
                )
            else:  # pragma: no cover - the discriminated union prevents this
                raise HarnessError(
                    "INVALID_CONFIGURATION", f"Unsupported change type {type(change).__name__}"
                )
        self._validate_graph_states(work, added_descriptors)
        for binding_id, (enabled, config) in work.items():
            descriptor = (
                added_descriptors[binding_id]
                if binding_id in added_descriptors
                else self.offerings[binding_id].descriptor
            )
            self._check_config_schema(descriptor, binding_id, config)
        added = [
            (binding_id, descriptor.plugin_id, work[binding_id][0], work[binding_id][1])
            for binding_id, descriptor in added_descriptors.items()
        ]
        return ChangeOutcome(states=work, added=added, changed=changed, noop=not changed)

    def _offering_unknown(self, binding_id: str) -> HarnessError:
        return HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Binding {binding_id!r} is not offered by this session.",
            details={"binding_id": binding_id, "session_id": self.session_id},
        )

    def _check_lifecycle(self, offering: BindingOffering, *, initial_configuration: bool) -> None:
        if initial_configuration:
            return
        if offering.policy.change_mode == "new_session":
            raise HarnessError(
                "NEW_SESSION_REQUIRED",
                f"Binding {offering.binding_id!r} can only be reconfigured in a new session.",
                details={"binding_id": offering.binding_id, "change_mode": "new_session"},
            )

    def _validate_enable(
        self,
        change: EnableChange,
        work: dict[str, tuple[bool, dict[str, Any]]],
        initial: bool,
    ) -> bool:
        offering = self.offerings.get(change.binding_id)
        if offering is None:
            raise self._offering_unknown(change.binding_id)
        current_enabled, config = work[change.binding_id]
        if change.enabled == current_enabled:
            return False  # idempotent no-op
        self._check_lifecycle(offering, initial_configuration=initial)
        if change.enabled:
            if not offering.available:
                raise HarnessError(
                    "CAPABILITY_UNAVAILABLE",
                    f"Binding {offering.binding_id!r} cannot be enabled: {offering.availability_reason}.",
                    details={
                        "binding_id": offering.binding_id,
                        "reason": offering.availability_reason,
                    },
                )
        # Initial selections get a lifecycle exception, but host toggle policy still applies.
        if not (offering.policy.client_can_toggle or offering.session_added):
            raise HarnessError(
                "POLICY_DENIED",
                f"Client policy forbids toggling binding {offering.binding_id!r}.",
                details={"binding_id": offering.binding_id},
            )
        work[change.binding_id] = (change.enabled, config)
        return True

    def _validate_configure(
        self,
        change: ConfigureChange,
        work: dict[str, tuple[bool, dict[str, Any]]],
        initial: bool,
    ) -> bool:
        offering = self.offerings.get(change.binding_id)
        if offering is None:
            raise self._offering_unknown(change.binding_id)
        enabled, config = work[change.binding_id]
        # Deep copy: path replacement must never mutate the live selected value before the
        # whole transaction has been validated and committed.
        mutated = copy.deepcopy(config)
        changed = False
        for path, value in change.fields.items():
            previous = _path_value(mutated, path)
            if _has_path(mutated, path) and previous == value:
                continue  # fields are replacements; the same value is a no-op
            editable = path in offering.policy.editable_fields or any(
                path.startswith(f"{candidate}.") for candidate in offering.policy.editable_fields
            )
            if not editable:
                raise HarnessError(
                    "POLICY_DENIED",
                    f"Field {path!r} of binding {change.binding_id!r} is not editable by clients.",
                    details={"binding_id": change.binding_id, "field": path},
                )
            if not initial:
                self._check_lifecycle(offering, initial_configuration=False)
            _set_path(mutated, path, value)
            changed = True
        if not changed:
            return False
        work[change.binding_id] = (enabled, mutated)
        return True

    def _validate_add(
        self,
        change: AddChange,
        work: dict[str, tuple[bool, dict[str, Any]]],
        added: dict[str, PluginDescriptor],
        *,
        initial_configuration: bool = False,
    ) -> bool:
        if change.binding_id in self.offerings or change.binding_id in added:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Binding {change.binding_id!r} already exists in this session.",
                details={"binding_id": change.binding_id},
            )
        if not self.registry.allow_local_binding_creation:
            raise HarnessError(
                "POLICY_DENIED",
                "This host does not advertise local binding creation; enable offered bindings instead.",
                details={"binding_id": change.binding_id, "plugin_id": change.plugin_id},
            )
        descriptor = self.registry.registered(change.plugin_id)
        if descriptor is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Plugin {change.plugin_id!r} is not registered for local binding creation; "
                "it cannot back a new binding.",
                details={"plugin_id": change.plugin_id, "binding_id": change.binding_id},
            )
        if not descriptor.compatibility.runtime_compatible:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Plugin {change.plugin_id!r} is not runtime-compatible on this host.",
                details={
                    "plugin_id": change.plugin_id,
                    "binding_id": change.binding_id,
                    "constraint": descriptor.compatibility.constraint,
                },
            )
        if not descriptor.default_policy.available:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Plugin {change.plugin_id!r} is marked unavailable by host policy.",
                details={
                    "plugin_id": change.plugin_id,
                    "binding_id": change.binding_id,
                    "reason": "host policy marks this plugin unavailable in this deployment",
                },
            )
        try:
            self.registry.check_compatibility(descriptor)
        except HarnessError:
            raise
        if not initial_configuration and descriptor.default_policy.change_mode == "new_session":
            raise HarnessError(
                "NEW_SESSION_REQUIRED",
                f"Plugin {change.plugin_id!r} can only be added when a new session is created.",
                details={
                    "plugin_id": change.plugin_id,
                    "binding_id": change.binding_id,
                    "change_mode": descriptor.default_policy.change_mode,
                },
            )
        self._check_config_schema(descriptor, change.binding_id, change.config)
        added[change.binding_id] = descriptor
        work[change.binding_id] = (change.enabled, dict(change.config))
        return True

    # --- dependency graph and conflicts -------------------------------

    def _validate_graph_states(
        self,
        work: dict[str, tuple[bool, dict[str, Any]]],
        added_descriptors: Mapping[str, PluginDescriptor] | None = None,
    ) -> None:
        added = added_descriptors or {}
        # A runtime-added plugin must declare only references this session can actually
        # resolve. Otherwise enabling it would silently accept an unmet requirement or an
        # inert conflict declaration.
        for binding_id, descriptor in added.items():
            for requirement in descriptor.requirements:
                if (
                    requirement not in work
                    and self.definition.resource_requirement(requirement) is None
                ):
                    raise HarnessError(
                        "INVALID_CONFIGURATION",
                        f"Binding {binding_id!r} requires {requirement!r}, which is neither an "
                        "offered binding nor a resource of this definition.",
                        details={"binding_id": binding_id, "requirement": requirement},
                    )
            for conflict in descriptor.conflicts:
                if conflict not in work:
                    raise HarnessError(
                        "INVALID_CONFIGURATION",
                        f"Binding {binding_id!r} conflicts with {conflict!r}, which is not an "
                        "offered binding.",
                        details={"binding_id": binding_id, "conflict": conflict},
                    )
        enabled_ids = {binding_id for binding_id, (enabled, _) in work.items() if enabled}
        for binding_id in sorted(enabled_ids):
            offering = self.offerings.get(binding_id)
            if offering is not None:
                requirements = offering.requirements
                conflicts = offering.conflicts
            elif binding_id in added:
                descriptor = added[binding_id]
                requirements = list(descriptor.requirements)
                conflicts = list(descriptor.conflicts)
            else:  # pragma: no cover - defensive
                continue
            for requirement in requirements:
                if requirement in work and requirement not in enabled_ids:
                    raise HarnessError(
                        "DEPENDENCY_CONFLICT",
                        f"Enabling {binding_id!r} requires {requirement!r}, which stays disabled; "
                        "submit one atomic change enabling the complete set.",
                        details={"binding_id": binding_id, "missing": [requirement]},
                    )
            for conflict in conflicts:
                if conflict in enabled_ids:
                    pair = sorted((binding_id, conflict))
                    raise HarnessError(
                        "DEPENDENCY_CONFLICT",
                        f"Bindings {pair[0]!r} and {pair[1]!r} conflict; they cannot both be enabled.",
                        details={"conflicts": [pair]},
                    )
        self._validate_no_cycles(work, enabled_ids, added)

    def _validate_no_cycles(
        self,
        work: dict[str, tuple[bool, dict[str, Any]]],
        enabled_ids: set[str],
        added: Mapping[str, PluginDescriptor],
    ) -> None:
        pending: dict[str, set[str]] = {}
        for binding_id in enabled_ids:
            offering = self.offerings.get(binding_id)
            if offering is not None:
                requirements = offering.requirements
            elif binding_id in added:
                requirements = list(added[binding_id].requirements)
            else:  # pragma: no cover - defensive
                requirements = []
            pending[binding_id] = {
                requirement for requirement in requirements if requirement in enabled_ids
            }
        ready = [binding_id for binding_id, deps in pending.items() if not deps]
        resolved: set[str] = set()
        while ready:
            binding_id = ready.pop()
            resolved.add(binding_id)
            for other, deps in pending.items():
                if binding_id in deps:
                    deps.discard(binding_id)
                    if not deps and other not in resolved and other not in ready:
                        ready.append(other)
        if resolved != set(pending):
            stuck = sorted(set(pending) - resolved)
            raise HarnessError(
                "DEPENDENCY_CONFLICT",
                "Enabled bindings form a requirement cycle between: " + ", ".join(stuck),
                details={"bindings": stuck},
            )

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


def host_editable_fields(descriptor: PluginDescriptor) -> set[str]:
    """Configuration paths the host lets clients edit for a runtime-added binding."""
    if descriptor.config_model is None:
        return set()
    return set(descriptor.default_policy.editable_fields)


def order_enabled_offerings(
    offerings: Sequence[BindingOffering],
    enabled_ids: Sequence[str],
) -> list[BindingOffering]:
    """Stable topological order over enabled bindings.

    Definition order breaks ties; requirement edges order dependents after their
    dependencies; a cycle is a configuration error and raises ``DEPENDENCY_CONFLICT``.
    """
    wanted = set(enabled_ids)
    enabled = [offering for offering in offerings if offering.binding_id in wanted]
    by_id = {offering.binding_id: offering for offering in offerings}
    position = {offering.binding_id: index for index, offering in enumerate(offerings)}
    indegree: dict[str, int] = {offering.binding_id: 0 for offering in enabled}
    edges: dict[str, list[str]] = {offering.binding_id: [] for offering in enabled}
    for offering in enabled:
        for requirement in offering.requirements:
            if requirement in wanted:
                edges[requirement].append(offering.binding_id)
                indegree[offering.binding_id] += 1
    queue = sorted(
        (binding_id for binding_id, degree in indegree.items() if degree == 0),
        key=lambda binding_id: (position.get(binding_id, 0), binding_id),
    )
    ordered: list[BindingOffering] = []
    while queue:
        binding_id = queue.pop(0)
        ordered.append(by_id[binding_id])
        for dependent in edges[binding_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
        queue.sort(key=lambda item: (position.get(item, 0), item))
    if len(ordered) != len(enabled):
        stuck = sorted(binding_id for binding_id, degree in indegree.items() if degree > 0)
        raise HarnessError(
            "DEPENDENCY_CONFLICT",
            "Enabled bindings form a requirement cycle between: " + ", ".join(stuck),
            details={"bindings": stuck},
        )
    return ordered


def simple_errors(exc: BaseException, *, limit: int = 5) -> list[dict[str, Any]]:
    """JSON-safe projection of pydantic validation errors (never leaks exception objects)."""
    raw = getattr(exc, "errors", None)
    if not callable(raw):
        return [{"msg": str(exc), "type": type(exc).__name__}]
    try:
        entries = raw()[:limit]
    except Exception:  # pragma: no cover - defensive
        return [{"msg": str(exc), "type": type(exc).__name__}]
    return [
        {
            "loc": [str(part) for part in entry.get("loc", ())],
            "msg": str(entry.get("msg", "")),
            "type": str(entry.get("type", "")),
        }
        for entry in entries
    ]
