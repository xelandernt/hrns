"""Named resources: how host-specific values meet portable definitions.

A definition *requires* named resources (a model, a workspace, memory, a session store, an
external service). Deployment resource bindings supply host-specific values; the session
resolves requirements against a :class:`ResourceSet` before any turn runs. Missing required
workspaces produce ``WORKSPACE_UNAVAILABLE``; other missing resources produce
``RESOURCE_UNAVAILABLE`` — a requested sandbox is never silently replaced with host
execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.protocol import HarnessError

__all__ = [
    "ResourceKind",
    "ResourceRequirement",
    "ResourceSet",
    "resolve_workspace_value",
    "resource_label",
]

_MISSING = object()

ResourceKind = Literal["model", "workspace", "memory", "store", "service", "custom"]


class ResourceRequirement(BaseModel):
    """One named resource a definition requires."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    kind: ResourceKind = "custom"
    description: str = ""
    required: bool = True


def resource_label(requirement: ResourceRequirement) -> str:
    """Human prefix for the requirement's kind, empty for ``custom``."""
    return f"{requirement.kind} " if requirement.kind and requirement.kind != "custom" else ""


class ResourceSet:
    """Resolved resource bindings for a deployment or session.

    Values are arbitrary host objects: a Pydantic AI model selection, a
    ``WorkspaceBackend``/``WorkspaceRef``, an upstream memory capability, a database
    factory, a credentials wrapper, and so on. The runtime never inspects plugin-specific
    values; it only passes them to definition/plugin factories.
    """

    def __init__(self, values: Mapping[str, Any] | None = None, **named: Any) -> None:
        merged: dict[str, Any] = dict(values or {})
        merged.update(named)
        self._values: dict[str, Any] = merged

    @property
    def values(self) -> dict[str, Any]:
        return dict(self._values)

    def __contains__(self, name: object) -> bool:
        return name in self._values

    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def __repr__(self) -> str:
        return f"ResourceSet(names={sorted(self._values)})"

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)

    def resolve(self, requirement: ResourceRequirement) -> Any:
        """Resolve one requirement, honouring conventional host aliases.

        Hosts commonly bind a single ``workspace``/``model`` value for the session even when
        the definition names requirements more specifically (``workspace.main``,
        ``model.default``). Alias resolution keeps those portable definitions usable without
        the host having to guess every declared name.
        """
        if requirement.name in self._values:
            return self._values[requirement.name]
        if requirement.kind == "workspace" and "workspace" in self._values:
            return self._values["workspace"]
        if requirement.kind == "model":
            model = self.get_model()
            if model is not None:
                return model
        return _MISSING

    def has(self, name: str) -> bool:
        return name in self._values

    def try_resolve(self, requirement: ResourceRequirement) -> tuple[bool, Any]:
        """``(found, value)`` resolution result for one requirement."""
        value = self.resolve(requirement)
        if value is _MISSING:
            return False, None
        return True, value

    def get_workspace(self, name: str = "workspace") -> Any:
        """Return a workspace binding, raising ``WORKSPACE_UNAVAILABLE`` when absent.

        A workspace value is anything ``Agent.run(workspace=...)`` accepts: a
        ``WorkspaceBackend``, a ``Workspace``-like object, a ``WorkspaceRef`` to continue
        in, or ``'new'``.
        """
        if name not in self._values:
            raise HarnessError(
                "WORKSPACE_UNAVAILABLE",
                f"The session requires workspace resource {name!r}, which this host does not bind.",
                details={"resource": name, "kind": "workspace"},
            )
        return self._values[name]

    def get_model(self) -> Any:
        """Return the model resource, or ``None`` when none was bound."""
        for name in ("model.default", "model.primary", "model"):
            if name in self._values:
                return self._values[name]
        return None

    def merge(self, other: Mapping[str, Any] | ResourceSet) -> ResourceSet:
        """Return a new set whose values start from this one and add ``other`` (no mutation)."""
        extra = other._values if isinstance(other, ResourceSet) else dict(other)
        return ResourceSet({**self._values, **extra})

    def missing(self, requirements: list[ResourceRequirement]) -> list[ResourceRequirement]:
        """Requirements whose names are not bound (directly or through a host alias)."""
        return [
            requirement
            for requirement in requirements
            if requirement.required and self.resolve(requirement) is _MISSING
        ]

    def validate_requirements(
        self,
        requirements: list[ResourceRequirement],
        *,
        context: str = "session",
    ) -> None:
        """Raise the kind-specific availability error for the first missing required resource."""
        for requirement in self.missing(requirements):
            code = "WORKSPACE_UNAVAILABLE" if requirement.kind == "workspace" else "RESOURCE_UNAVAILABLE"
            raise HarnessError(
                code,
                f"{context} requires {resource_label(requirement)}"
                f"resource {requirement.name!r}, which this deployment does not bind.",
                details={
                    "resource": requirement.name,
                    "kind": requirement.kind,
                    "description": requirement.description,
                },
            )


def resolve_workspace_value(value: Any) -> Any:
    """Coerce a bound workspace resource into something ``Agent.run(workspace=...)`` accepts.

    Deployment bindings may supply a plain filesystem path; the runtime maps it to a native
    ``LocalWorkspaceBackend`` rather than failing inside Pydantic AI. Rich bindings
    (sandbox backends, ``WorkspaceRef``, ``'new'``) pass through unchanged.
    """
    from pathlib import Path

    if isinstance(value, str | Path):
        from pydantic_ai.workspaces import LocalWorkspaceBackend

        return LocalWorkspaceBackend(value)
    return value
