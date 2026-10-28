"""Binding offering state, configuration values, and transaction outcomes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from acp import schema
from pydantic_core import to_jsonable_python

from hrns.protocol import (
    BindingChange,
    BindingState,
    CapabilityPolicy,
    ConfigureChange,
    EnableChange,
    HrnsError,
    canonical_hash,
)

from ..client_io import describe_missing, missing_client_capability
from ..registry import PluginDescriptor


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
    client_capabilities: schema.ClientCapabilities | None = None
    """What the connected ACP client advertised on ``initialize``, for ``requires_client``."""

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
        return (
            self.policy.available
            and self.descriptor.compatibility.runtime_compatible
            and self.missing_client is None
        )

    @property
    def missing_client(self) -> str | None:
        """The path into ``clientCapabilities`` this binding needs and the client lacks."""
        return missing_client_capability(self.client_capabilities, self.descriptor.requires_client)

    @property
    def availability_reason(self) -> str | None:
        if not self.policy.available:
            return "host policy marks this binding unavailable in this deployment"
        if not self.descriptor.compatibility.runtime_compatible:
            return "upstream compatibility constraints do not hold on this host"
        missing = self.missing_client
        if missing is not None:
            advertised = describe_missing(self.descriptor.requires_client)
            return f"the connected ACP client does not advertise {advertised}"
        return None

    def state(self) -> BindingState:
        return BindingState(enabled=self.enabled, config=jsonable_config(self.config))


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
        raise HrnsError(
            "INVALID_CONFIGURATION",
            "Configuration values must be finite, JSON-serializable values.",
            details={"reason": type(exc).__name__},
        ) from exc


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
