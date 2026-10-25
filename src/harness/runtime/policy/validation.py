"""Graph, change, and descriptor validation for capability policy."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from harness.protocol import (
    AddChange,
    CapabilityPolicy,
    ConfigureChange,
    EnableChange,
    HarnessError,
)

from ..registry import PluginDescriptor
from .binding import BindingOffering, _has_path, _path_value, _set_path

if TYPE_CHECKING:
    from .catalog import PolicyCatalog


def validate_initial_state(catalog: PolicyCatalog) -> None:
    """Defaults must form a legal deployment composition (fail at session open)."""
    enabled = [o for o in catalog.offerings.values() if o.enabled]
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
            if req in catalog.offerings and req not in enabled_ids
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
        order_enabled_offerings(catalog.ordered_offerings(), sorted(enabled_ids))
    except HarnessError as exc:
        if exc.code == "DEPENDENCY_CONFLICT":
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "Default configuration has a requirement cycle: " + exc.message,
                details=exc.details,
            ) from exc
        raise


def effective_policy(
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


def validate_graph_references(catalog: PolicyCatalog) -> None:
    for binding_id, offering in catalog.offerings.items():
        for requirement in offering.requirements:
            if (
                requirement in catalog.offerings
                or catalog.definition.resource_requirement(requirement) is not None
            ):
                continue
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Binding {binding_id!r} requires {requirement!r}, which is neither an offered binding "
                "nor a resource of this definition.",
                details={"binding_id": binding_id, "requirement": requirement},
            )
        for conflict in offering.conflicts:
            if conflict not in catalog.offerings:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Binding {binding_id!r} conflicts with {conflict!r}, which is not an offered binding.",
                    details={"binding_id": binding_id, "conflict": conflict},
                )


def validate_all_configs(catalog: PolicyCatalog) -> None:
    for offering in catalog.offerings.values():
        check_config_schema(catalog, offering.descriptor, offering.binding_id, offering.config)


def check_config_schema(
    catalog: PolicyCatalog,
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


def offering_unknown(catalog: PolicyCatalog, binding_id: str) -> HarnessError:
    return HarnessError(
        "CAPABILITY_UNAVAILABLE",
        f"Binding {binding_id!r} is not offered by this session.",
        details={"binding_id": binding_id, "session_id": catalog.session_id},
    )


def check_lifecycle(
    catalog: PolicyCatalog, offering: BindingOffering, *, initial_configuration: bool
) -> None:
    if initial_configuration:
        return
    if offering.policy.change_mode == "new_session":
        raise HarnessError(
            "NEW_SESSION_REQUIRED",
            f"Binding {offering.binding_id!r} can only be reconfigured in a new session.",
            details={"binding_id": offering.binding_id, "change_mode": "new_session"},
        )


def validate_enable(
    catalog: PolicyCatalog,
    change: EnableChange,
    work: dict[str, tuple[bool, dict[str, Any]]],
    initial: bool,
) -> bool:
    offering = catalog.offerings.get(change.binding_id)
    if offering is None:
        raise offering_unknown(catalog, change.binding_id)
    current_enabled, config = work[change.binding_id]
    if change.enabled == current_enabled:
        return False  # idempotent no-op
    check_lifecycle(catalog, offering, initial_configuration=initial)
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


def validate_configure(
    catalog: PolicyCatalog,
    change: ConfigureChange,
    work: dict[str, tuple[bool, dict[str, Any]]],
    initial: bool,
) -> bool:
    offering = catalog.offerings.get(change.binding_id)
    if offering is None:
        raise offering_unknown(catalog, change.binding_id)
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
            check_lifecycle(catalog, offering, initial_configuration=False)
        _set_path(mutated, path, value)
        changed = True
    if not changed:
        return False
    work[change.binding_id] = (enabled, mutated)
    return True


def validate_add(
    catalog: PolicyCatalog,
    change: AddChange,
    work: dict[str, tuple[bool, dict[str, Any]]],
    added: dict[str, PluginDescriptor],
    *,
    initial_configuration: bool = False,
) -> bool:
    if change.binding_id in catalog.offerings or change.binding_id in added:
        raise HarnessError(
            "INVALID_CONFIGURATION",
            f"Binding {change.binding_id!r} already exists in this session.",
            details={"binding_id": change.binding_id},
        )
    if not catalog.registry.allow_local_binding_creation:
        raise HarnessError(
            "POLICY_DENIED",
            "This host does not advertise local binding creation; enable offered bindings instead.",
            details={"binding_id": change.binding_id, "plugin_id": change.plugin_id},
        )
    descriptor = catalog.registry.registered(change.plugin_id)
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
        catalog.registry.check_compatibility(descriptor)
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
    check_config_schema(catalog, descriptor, change.binding_id, change.config)
    added[change.binding_id] = descriptor
    work[change.binding_id] = (change.enabled, dict(change.config))
    return True


def validate_graph_states(
    catalog: PolicyCatalog,
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
                and catalog.definition.resource_requirement(requirement) is None
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
        offering = catalog.offerings.get(binding_id)
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
    validate_no_cycles(catalog, enabled_ids, added)


def validate_no_cycles(
    catalog: PolicyCatalog,
    enabled_ids: set[str],
    added: Mapping[str, PluginDescriptor],
) -> None:
    pending: dict[str, set[str]] = {}
    for binding_id in enabled_ids:
        offering = catalog.offerings.get(binding_id)
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
