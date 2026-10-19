"""Typed helpers for the versioned ``_harness/`` project extension.

Negotiation follows the concept: the client advertises extension major
version 1 and its supported methods in ``initialize._meta.harness``; the
agent may advertise its own subset in ``initialize._meta.harness``. Only
mutually advertised methods are invoked. Extension message types come from
``harness_protocol``; parsing uses those models so malformed transactions
fail loudly instead of half-applying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness_protocol import (
    BindingDescriptor,
    ConfigurationSnapshot,
    ConfigurationUpdate,
    ExtensionAdvertisement,
    PluginInfo,
    changes_adapter,
)

from .constants import EXTENSION_METHODS, EXTENSION_VERSION

__all__ = [
    "CapabilityListResult",
    "HarnessExtension",
    "PluginListResult",
    "advertisement_meta",
    "parse_advertisement",
    "parse_capability_list",
    "parse_configuration_snapshot",
    "parse_plugin_list",
    "strip_prefix",
    "validate_changes",
]


def strip_prefix(method: str) -> str:
    """ACP ``ext_method`` takes names without the leading underscore."""
    return method[1:] if method.startswith("_") else method


def advertisement_meta(
    *,
    methods: tuple[str, ...] = EXTENSION_METHODS,
    advertise: bool = True,
) -> dict[str, Any]:
    """Build the ``_meta`` payload attached to ``initialize`` (and optionally sessions)."""
    if not advertise:
        return {}
    return {
        "harness": {
            "version": EXTENSION_VERSION,
            "methods": list(methods),
        }
    }


@dataclass(frozen=True, slots=True)
class HarnessExtension:
    """The negotiated extension surface of one connection."""

    enabled: bool = False
    version: int = 0
    methods: frozenset[str] = field(default_factory=frozenset)
    local_binding_creation: bool = False

    def supports(self, method: str) -> bool:
        if not self.enabled:
            return False
        return method in self.methods

    def supports_any(self, *methods: str) -> bool:
        return self.enabled and any(method in self.methods for method in methods)

    def describe(self) -> str:
        if not self.enabled:
            return "standard ACP only"
        extras = " · local binding creation" if self.local_binding_creation else ""
        return f"extension v{self.version}{extras}"


def _normalize_method(method: str) -> str:
    return method if method.startswith("_") else f"_{method}"


def parse_advertisement(meta: dict[str, Any] | None) -> HarnessExtension:
    """Parse ``initialize._meta.harness`` tolerantly.

    Agents may send the full advertisement, a version-only marker, or nothing.
    A version mismatch disables the extension (standard ACP still works).
    """
    if not meta:
        return HarnessExtension()
    payload = meta.get("harness")
    if not isinstance(payload, dict):
        return HarnessExtension()
    # Extension support is explicitly versioned. A methods list without a
    # major version is not a compatible advertisement, and a version-only
    # marker grants no methods: only mutually listed methods may be invoked.
    try:
        if "version" not in payload:
            return HarnessExtension()
        methods = payload.get("methods")
        if methods is None:
            methods = []
        if not isinstance(methods, (list, tuple)):
            return HarnessExtension()
        version = int(payload["version"])
        advertisement = ExtensionAdvertisement(
            version=EXTENSION_VERSION,
            methods=[str(method) for method in methods],
            local_binding_creation=bool(payload.get("localBindingCreation", False)),
        )
        if version != EXTENSION_VERSION:
            return HarnessExtension(version=version)
    except Exception:
        # A broken advertisement must not break the standard connection.
        return HarnessExtension()
    methods = frozenset(_normalize_method(method) for method in advertisement.methods)
    return HarnessExtension(
        enabled=advertisement.version == EXTENSION_VERSION,
        version=advertisement.version,
        methods=methods,
        local_binding_creation=advertisement.local_binding_creation,
    )


def _lenient_validate(model: Any, payload: Any) -> Any:
    """Validate a wire object, ignoring extra keys from a newer host.

    Wire models forbid unknown fields so typos fail loudly, but the client must
    keep talking to hosts that add compatible fields (for example ``model`` or
    ``requestId`` alongside a configuration snapshot). Known fields are passed
    through unchanged; unknown ones are ignored rather than guessed at.
    """
    if not isinstance(payload, dict):
        raise ValueError("expected an object")
    aliases = {name: (field_.alias or name) for name, field_ in model.model_fields.items()}
    allowed = set(aliases) | set(aliases.values())
    return model.model_validate({key: value for key, value in payload.items() if key in allowed})


@dataclass(frozen=True, slots=True)
class CapabilityListResult:
    bindings: tuple[BindingDescriptor, ...] = ()
    selected_revision: int = 0
    active_revision: int | None = None
    policy_revision: int = 1
    application_status: str | None = None
    model: dict[str, Any] | None = None
    next_cursor: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def by_id(self) -> dict[str, BindingDescriptor]:
        return {binding.binding_id: binding for binding in self.bindings}


def _list_items(payload: dict[str, Any], *keys: str) -> list[Any]:
    """Accept the shared ``items`` envelope and the historical per-method keys."""
    for key in keys:
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, list):
                raise ValueError(f"'{key}' must be a list")
            return value
    return []


def parse_capability_list(payload: dict[str, Any]) -> CapabilityListResult:
    """Parse ``_harness/capabilities/list``.

    The versioned shared contract returns ``{items, nextCursor?, revisions...}``;
    released hosts returned the same page under ``bindings``. Both are accepted.
    """
    if not isinstance(payload, dict):
        raise ValueError("capabilities/list result must be an object")
    raw_bindings = _list_items(payload, "items", "bindings")
    bindings = tuple(_lenient_validate(BindingDescriptor, item) for item in raw_bindings)
    selected = payload.get("selectedRevision", payload.get("selected_revision", 0))
    active = payload.get("activeRevision", payload.get("active_revision"))
    policy = payload.get("policyRevision", payload.get("policy_revision", 1))
    cursor = payload.get("nextCursor", payload.get("next_cursor"))
    model = payload.get("model")
    return CapabilityListResult(
        bindings=bindings,
        selected_revision=int(selected or 0),
        active_revision=None if active is None else int(active),
        policy_revision=int(policy or 1),
        application_status=payload.get("applicationStatus") or payload.get("application_status"),
        model=model if isinstance(model, dict) else None,
        next_cursor=cursor,
        raw=payload,
    )


@dataclass(frozen=True, slots=True)
class PluginListResult:
    plugins: tuple[PluginInfo, ...] = ()
    next_cursor: str | None = None


def parse_plugin_list(payload: dict[str, Any]) -> PluginListResult:
    if not isinstance(payload, dict):
        raise ValueError("plugins/list result must be an object")
    raw_plugins = _list_items(payload, "items", "plugins")
    return PluginListResult(
        plugins=tuple(_lenient_validate(PluginInfo, item) for item in raw_plugins),
        next_cursor=payload.get("nextCursor", payload.get("next_cursor")),
    )


def parse_configuration_snapshot(payload: dict[str, Any]) -> ConfigurationSnapshot:
    """Parse get/update/changed responses, ignoring compatible host extras."""
    if isinstance(payload, dict) and "snapshot" in payload and isinstance(payload["snapshot"], dict):
        payload = payload["snapshot"]
    if not isinstance(payload, dict):
        raise ValueError("configuration snapshot must be an object")
    return _lenient_validate(ConfigurationSnapshot, payload)


def validate_changes(
    changes: list[dict[str, Any]] | dict[str, Any] | None,
) -> list[Any]:
    """Validate the discriminated binding changes before anything is sent."""
    if changes is None:
        raise ValueError("configuration update needs at least one change")
    return changes_adapter.validate_python(changes if isinstance(changes, list) else [changes])


def build_configuration_update(
    session_id: str,
    request_id: str,
    expected_revision: int,
    changes: list[dict[str, Any]] | dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate changes locally, then render the camelCase request payload."""
    validated = validate_changes(changes)
    update = ConfigurationUpdate(
        session_id=session_id,
        request_id=request_id,
        expected_revision=expected_revision,
        changes=validated,
    )
    return update.to_wire()
