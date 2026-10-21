"""Plugin registry: descriptors, lazy discovery, and capability instantiation.

A *plugin* is a native Pydantic AI capability plus optional management metadata: a stable
publisher-qualified ``plugin_id``, an installed ``version``, a validated configuration
model, a host-local factory, requirements/conflicts, lifecycle modes, presentation ids, and
compatibility constraints. Authors do not inherit a second behavioral base class; a plugin
factory creates an ``AbstractCapability`` (or the ``Capability`` convenience class).

Discovery is lazy: local import references and installed ``harness.capabilities`` entry
points are read as metadata first, and a plugin's module is imported only when one of its
bindings is actually composed. Duplicate plugin IDs are configuration errors, never
resolved by import order.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.capabilities import AbstractCapability, AgentCapability
from pydantic_core import to_jsonable_python

from harness.protocol import CapabilityPolicy, HarnessError, PluginInfo

from .resources import ResourceSet

__all__ = [
    "BindingContext",
    "CompatibilityConstraints",
    "ENTRY_POINT_GROUP",
    "PluginDescriptor",
    "PluginRegistry",
]

ENTRY_POINT_GROUP = "harness.capabilities"

_BUNDLED_PLUGINS: dict[str, PluginDescriptor] | None = None
_BUNDLED_ERROR: str | None = None


def bundled_catalog_plugins() -> dict[str, PluginDescriptor]:
    """Bundled official adapters, imported lazily and cached process-wide.

    The catalog is metadata plus lazy factory references; importing it does not import any
    optional integration. Entries marked unavailable in the pinned upstream release are
    skipped (they have no factory), matching ``catalog.register_catalog``.
    """
    global _BUNDLED_PLUGINS, _BUNDLED_ERROR
    if _BUNDLED_PLUGINS is not None:
        return _BUNDLED_PLUGINS
    plugins: dict[str, PluginDescriptor] = {}
    try:
        from .catalog import CatalogTier, iter_plugins

        accepted = {CatalogTier.VERIFIED, CatalogTier.INSTALL_REQUIRED}
        for catalog_entry in iter_plugins():
            if catalog_entry.tier not in accepted:
                continue
            try:
                plugins[catalog_entry.plugin_id] = catalog_entry.descriptor()
            except HarnessError:  # pragma: no cover - unavailable factory
                continue
        _BUNDLED_ERROR = None
    except Exception as exc:  # pragma: no cover - broken/missing catalog must not break core
        _BUNDLED_ERROR = f"{type(exc).__name__}: {exc}"
        plugins = {}
    _BUNDLED_PLUGINS = plugins
    return plugins


CapabilityLifecycleMode = Literal["next_turn", "new_session"]
CapabilityStateMode = Literal["run", "session", "external"]


class CompatibilityConstraints(BaseModel):
    """Runtime/upstream version range and platform constraints for a plugin."""

    model_config = {"extra": "forbid"}

    runtime_compatible: bool = True
    """Whether the plugin is usable as a native capability on this runtime (always true for
    descriptors backed by installed factories here)."""
    min_python: str | None = None
    requires_extras: list[str] = Field(default_factory=list)
    """Extra names (e.g. ``['openai']``) that must be importable when the binding composes."""
    providers: list[str] = Field(default_factory=list)
    required_apps: list[str] = Field(default_factory=list)
    constraint: str | None = None
    """Free-form note rendered in catalogs, e.g. 'Apple-silicon macOS only'."""


class PluginDescriptor(BaseModel):
    """Management metadata for one capability plugin.

    ``factory`` creates the *native capability* from a validated configuration and a
    :class:`BindingContext`. It may also be a string import reference ``module:attr`` which
    is resolved on first instantiation, keeping module imports lazy.
    """

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    plugin_id: str = Field(min_length=1, max_length=200)
    version: str = Field(default="0.0.0", min_length=1, max_length=100)
    description: str = ""
    config_model: type[BaseModel] | None = None
    """Optional Pydantic model producing the JSON schema for editable, non-secret fields."""
    factory: Callable[..., Any] | str | None = None
    """Factory callable (or ``module:attr`` import reference evaluated lazily)."""
    instructions: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    """``binding_id`` (or resource) names required when this plugin is enabled."""
    conflicts: list[str] = Field(default_factory=list)
    """``binding_id`` names that may not be enabled side by side with this plugin."""
    change_mode: CapabilityLifecycleMode = "new_session"
    state_mode: CapabilityStateMode = "run"
    presentation: list[str] = Field(default_factory=list)
    """Presentation identifiers for host presenters / client-side renderers."""
    default_policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    compatibility: CompatibilityConstraints = Field(default_factory=CompatibilityConstraints)
    local_hint: dict[str, Any] = Field(default_factory=dict)
    """Non-executable discovery metadata from entry points (name, dist info)."""

    def resolved_factory(self) -> Callable[..., Any]:
        """Return the factory callable, importing it lazily from ``module:attr`` if needed."""
        target = self.factory
        if target is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Plugin {self.plugin_id!r} has no capability factory; it cannot be composed.",
                details={"plugin_id": self.plugin_id},
            )
        if isinstance(target, str):
            module_name, _, attribute = target.partition(":")
            if not module_name:
                raise HarnessError(
                    "CAPABILITY_UNAVAILABLE",
                    f"Plugin {self.plugin_id!r} import reference {target!r} is not 'module:attr'.",
                    details={"plugin_id": self.plugin_id, "import_ref": target},
                )
            try:
                module = importlib.import_module(module_name)
                factory = getattr(module, attribute)
            except Exception as exc:
                raise HarnessError(
                    "CAPABILITY_UNAVAILABLE",
                    f"Failed to import capability factory for plugin {self.plugin_id!r}: {exc}",
                    details={"plugin_id": self.plugin_id, "import_ref": target},
                ) from exc
            if not callable(factory):
                raise HarnessError(
                    "CAPABILITY_UNAVAILABLE",
                    f"Plugin {self.plugin_id!r} import reference {target!r} is not callable.",
                    details={"plugin_id": self.plugin_id},
                )
            return factory
        return target

    def config_schema(self) -> dict[str, Any]:
        """JSON schema for this plugin's editable, non-secret configuration fields."""
        if self.config_model is None:
            return {}
        try:
            return self.config_model.model_json_schema(mode="validation")
        except Exception:  # pragma: no cover - schema generation is pure
            return {}

    def config_defaults(self) -> dict[str, Any]:
        """Initial values from the config model's field defaults."""
        if self.config_model is None:
            return {}
        model = self.config_model
        try:
            instance = model()
        except ValidationError:  # pragma: no cover - required fields have no defaults
            return {}
        dump = instance.model_dump(mode="json", exclude_none=False)
        return {key: value for key, value in dump.items()}

    def to_info(self) -> PluginInfo:
        """Wire-view descriptor for the extension's ``plugins list``."""
        return PluginInfo(
            plugin_id=self.plugin_id,
            version=self.version,
            description=self.description,
            config_schema=self.config_schema(),
            change_mode=self.change_mode,
            state_mode=self.state_mode,
            runtime_compatible=self.compatibility.runtime_compatible,
            managed=True,
            presentation=self.presentation[0] if self.presentation else "generic",
            requirements=list(self.requirements),
            constraint=self.compatibility.constraint,
        )


class BindingContext(BaseModel):
    """Context passed to plugin factories when a binding is composed for a turn."""

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    binding_id: str
    plugin_id: str
    session_id: str
    principal: str
    deployment_id: str
    binding_config: dict[str, Any]
    """Raw binding configuration (merged over plugin defaults)."""
    resources: ResourceSet | None = None
    """Resolved resources for the deployment, before session additions."""
    active_configuration: Any = None
    """The turn's immutable :class:`~harness.protocol.ConfigurationSnapshot`."""
    agent_context: Any = None
    """The turn's :class:`~harness.runtime.definitions.AgentFactoryContext` (duck-typed)."""


def _validate_required_extras(descriptor: PluginDescriptor) -> None:
    missing: list[str] = []
    for extra in descriptor.compatibility.requires_extras:
        try:
            importlib.import_module(extra)
        except ImportError:
            missing.append(extra)
    if missing:
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Plugin {descriptor.plugin_id!r} requires missing optional dependencies: {', '.join(missing)}.",
            details={"plugin_id": descriptor.plugin_id, "missing_extras": missing},
        )


def _check_min_python(descriptor: PluginDescriptor) -> None:
    min_python = descriptor.compatibility.min_python
    if not min_python:
        return
    current = ".".join(str(part) for part in sys.version_info[:2])
    parts = [int(part) for part in min_python.split(".")[:2] if part.isdigit()]
    if parts and current_tuple(sys.version_info[:2]) < tuple(parts):
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Plugin {descriptor.plugin_id!r} needs Python {min_python}+; running {current}.",
            details={"plugin_id": descriptor.plugin_id, "required_python": min_python},
        )


def current_tuple(parts: Sequence[int]) -> tuple[int, ...]:
    return tuple(parts)


class _PendingImportPlugin:
    """Metadata for an import-reference plugin discovered without importing its module."""

    __slots__ = ("descriptor",)

    def __init__(self, descriptor: PluginDescriptor) -> None:
        self.descriptor = descriptor


class PluginRegistry:
    """Registered plugin catalog for one runtime host.

    ``PluginRegistry()`` starts empty; :meth:`register` and :meth:`register_import` add
    descriptors, and :meth:`discover` scans the installed ``harness.capabilities`` entry
    point group lazily (metadata only; imports are deferred until composition). Host
    configuration decides whether new bindings may be created by clients from the
    registered catalog: see :attr:`allow_local_binding_creation`.
    """

    def __init__(
        self,
        *,
        allow_local_binding_creation: bool = False,
        use_bundled_catalog: bool = True,
    ) -> None:
        self._plugins: dict[str, PluginDescriptor] = {}
        self._entry_point_source: str | None = None
        self.allow_local_binding_creation = allow_local_binding_creation
        self.use_bundled_catalog = use_bundled_catalog
        self._discovery_errors: dict[str, str] = {}

    # --- registration -------------------------------------------------

    def register(self, descriptor: PluginDescriptor) -> PluginDescriptor:
        """Register a descriptor. Duplicate plugin IDs are configuration errors."""
        existing = self._plugins.get(descriptor.plugin_id)
        if existing is not None and existing != descriptor:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Plugin id {descriptor.plugin_id!r} is already registered; duplicate ids are configuration "
                "errors, never resolved by import order.",
                details={"plugin_id": descriptor.plugin_id},
            )
        self._plugins[descriptor.plugin_id] = descriptor
        return descriptor

    def register_import(
        self,
        plugin_id: str,
        import_ref: str,
        *,
        version: str = "0.0.0",
        description: str = "",
        config_model: type[BaseModel] | None = None,
        requirements: list[str] | None = None,
        conflicts: list[str] | None = None,
        change_mode: CapabilityLifecycleMode = "new_session",
        state_mode: CapabilityStateMode = "run",
        presentation: list[str] | None = None,
        default_policy: CapabilityPolicy | None = None,
        compatibility: CompatibilityConstraints | None = None,
    ) -> PluginDescriptor:
        """Register a plugin from a ``module:attr`` import reference without importing it."""
        descriptor = PluginDescriptor(
            plugin_id=plugin_id,
            version=version,
            description=description,
            config_model=config_model,
            factory=import_ref,
            requirements=list(requirements or []),
            conflicts=list(conflicts or []),
            change_mode=change_mode,
            state_mode=state_mode,
            presentation=list(presentation or []),
            default_policy=default_policy or CapabilityPolicy(),
            compatibility=compatibility or CompatibilityConstraints(),
        )
        return self.register(descriptor)

    def register_capability(
        self,
        plugin_id: str,
        factory: Callable[..., Any],
        *,
        version: str = "0.0.0",
        **kwargs: Any,
    ) -> PluginDescriptor:
        """Register a plugin whose factory callable is already in hand."""
        return self.register(
            PluginDescriptor(plugin_id=plugin_id, version=version, factory=factory, **kwargs)
        )

    # --- discovery ----------------------------------------------------

    def discover(self, *, entry_point_group: str = ENTRY_POINT_GROUP) -> int:
        """Scan installed entry points lazily and register any new plugins.

        Only entry-point *metadata* is read here: the descriptor's factory is the import
        reference ``entry_point.module:entry_point.attr``, so plugin modules load on first
        composition. Returns the number of newly registered plugins. Import failures are
        recorded per plugin id and surfaced with ``registration_error``; they are never
        resolved by import order.
        """
        newly = 0
        try:
            eps = importlib.metadata.entry_points()
            selected = list(eps.select(group=entry_point_group))
        except Exception:  # pragma: no cover - metadata access is robust in CPython
            selected = []
        for entry_point in selected:
            plugin_id = entry_point.name
            if plugin_id in self._plugins or plugin_id in self._bundled():
                continue
            try:
                self._plugins[plugin_id] = self._descriptor_from_entry_point(entry_point)
            except HarnessError as exc:
                self._discovery_errors[plugin_id] = exc.message
                continue
            newly += 1
        if newly:
            self._entry_point_source = entry_point_group
        return newly

    def _descriptor_from_entry_point(self, entry_point: Any) -> PluginDescriptor:
        return PluginDescriptor(
            plugin_id=entry_point.name,
            factory=f"{entry_point.module}:{entry_point.attr}",
            version=_entry_point_version(entry_point),
            description=_entry_point_summary(entry_point),
            local_hint={"source": "entry_point", "group": entry_point.group},
        )

    def registration_error(self, plugin_id: str) -> str | None:
        """Discovery failure message for a plugin id, when one was recorded."""
        return self._discovery_errors.get(plugin_id)

    # --- lookup -------------------------------------------------------

    def _bundled(self) -> dict[str, PluginDescriptor]:
        return bundled_catalog_plugins() if self.use_bundled_catalog else {}

    def lookup(self, plugin_id: str | None) -> PluginDescriptor | None:
        """Return a descriptor, resolving bundled catalog entries as a fallback.

        Explicit registrations (including installation-specific factories) take precedence
        over the bundled catalog for the same plugin id.
        """
        if plugin_id is None:
            return None
        descriptor = self._plugins.get(plugin_id)
        if descriptor is not None:
            return descriptor
        return self._bundled().get(plugin_id)

    def registered(self, plugin_id: str | None) -> PluginDescriptor | None:
        """Return an explicitly registered descriptor, never a bundled lookup fallback.

        Local binding creation is offered from the host's registered catalog only; a
        bundled descriptor that merely backs an already-selected definition binding is not
        automatically exposed to clients as an addable plugin.
        """
        if plugin_id is None:
            return None
        return self._plugins.get(plugin_id)

    def require(self, plugin_id: str) -> PluginDescriptor:
        """Return the resolved descriptor or raise ``CAPABILITY_UNAVAILABLE``."""
        descriptor = self.lookup(plugin_id)
        if descriptor is None:
            error = self._discovery_errors.get(plugin_id)
            if error is None and _BUNDLED_ERROR and self.use_bundled_catalog:
                error = f"Bundled capability catalog unavailable ({_BUNDLED_ERROR})."
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                error or f"Plugin {plugin_id!r} is not registered on this host.",
                details={"plugin_id": plugin_id},
            )
        return descriptor

    def catalog(self) -> list[PluginInfo]:
        """Wire descriptors for explicitly registered plugins.

        The bundled catalog is a lookup fallback for bindings a deployment already selects;
        it is not offered for local binding creation unless the host registered it.
        """
        return [descriptor.to_info() for descriptor in self.ordered()]

    def ordered(self) -> list[PluginDescriptor]:
        """Explicitly registered descriptors in stable registration order."""
        return list(self._plugins.values())

    def reload(self) -> int:
        """Re-run discovery between sessions after installation (never mid-session)."""
        return self.discover()

    # --- composition --------------------------------------------------

    def check_compatibility(self, descriptor: PluginDescriptor) -> None:
        """Raise ``CAPABILITY_UNAVAILABLE`` when host prerequisites are not installed.

        Descriptor registration is metadata-only; this check runs when a binding is about
        to become part of a session (add transaction) or composed for a turn.
        """
        _check_min_python(descriptor)
        _validate_required_extras(descriptor)

    def instantiate(
        self,
        descriptor: PluginDescriptor,
        config: Mapping[str, Any],
        context: BindingContext,
    ) -> AgentCapability:
        """Validate ``config`` and call the plugin factory, returning the native capability."""
        self.check_compatibility(descriptor)
        validated_config: Any
        if descriptor.config_model is not None:
            try:
                validated_config = descriptor.config_model.model_validate(dict(config))
            except ValidationError as exc:
                errors = _simple_errors(exc)
                rendered = "; ".join(
                    f"{'.'.join(error['loc'])}: {error['msg']}" for error in errors
                )
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Invalid configuration for binding {context.binding_id!r} "
                    f"(plugin {descriptor.plugin_id!r}): {rendered}",
                    details={
                        "binding_id": context.binding_id,
                        "plugin_id": descriptor.plugin_id,
                        "errors": errors,
                    },
                ) from exc
        else:
            unknown = dict(config)
            validated_config = CapabilityShim(unknown)
        factory = descriptor.resolved_factory()
        try:
            capability = factory(validated_config, context)
        except HarnessError:
            raise
        except Exception as exc:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Plugin {descriptor.plugin_id!r} failed to create capability for binding "
                f"{context.binding_id!r}: {exc}",
                details={"plugin_id": descriptor.plugin_id, "binding_id": context.binding_id},
            ) from exc
        if not _is_agent_capability(capability):
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Plugin {descriptor.plugin_id!r} factory did not return a native Pydantic AI capability.",
                details={"plugin_id": descriptor.plugin_id, "returned": type(capability).__name__},
            )
        return capability


class CapabilityShim(dict[str, Any]):
    """Configuration snapshot for plugins without a ``config_model``."""


def _is_agent_capability(value: Any) -> bool:
    if isinstance(value, AbstractCapability):
        return True
    return callable(value) and not isinstance(value, type)


def _entry_point_version(entry_point: Any) -> str:
    try:
        dist = entry_point.dist
    except Exception:  # pragma: no cover - defensive
        return "0.0.0"
    try:
        return dist.version if dist is not None else "0.0.0"
    except Exception:  # pragma: no cover - defensive
        return "0.0.0"


def _entry_point_summary(entry_point: Any) -> str:
    try:
        if entry_point.dist is not None:
            summary = (entry_point.dist.metadata or {}).get("Summary")
        else:
            summary = None
    except Exception:  # pragma: no cover - defensive
        summary = None
    return summary or ""


def jsonable(value: Any) -> Any:
    """JSON-safe projection used for persisted and wire values."""
    return to_jsonable_python(value)


def _simple_errors(exc: BaseException, *, limit: int = 5) -> list[dict[str, Any]]:
    """JSON-safe projection of pydantic validation errors."""
    raw = getattr(exc, "errors", None)
    if not callable(raw):
        return [{"loc": [], "msg": str(exc), "type": type(exc).__name__}]
    try:
        entries = raw()[:limit]
    except Exception:  # pragma: no cover - defensive
        return [{"loc": [], "msg": str(exc), "type": type(exc).__name__}]
    return [
        {
            "loc": [str(part) for part in entry.get("loc", ())],
            "msg": str(entry.get("msg", "")),
            "type": str(entry.get("type", "")),
        }
        for entry in entries
    ]
