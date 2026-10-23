"""Typed resource slots: how a portable definition meets host-owned objects.

A definition *requires* resources; a deployment *binds* values to them. The binding side is
typed: a :class:`ResourceSlot` is the only key the Python API accepts, so a misspelled
resource is an ``AttributeError`` at import time instead of a ``None`` in the middle of a tool
call. Strings survive only in data that crosses a wire — manifests, ``--resource`` flags, and
the ACP catalog — where they are unavoidable.

Each slot owns three things a plain string key cannot: the value type it produces, the
narrowing function that turns an untyped binding into that type, and the error code raised when
it is missing. ``ResourceSet`` never widens a read to ``object`` and never casts: the value
comes back through the slot's own ``coerce``, so a positional read is typed at the call site.

Three rules keep the two sides honest:

* a definition that enables a plugin declaring ``needs`` must declare the matching slot, and
  composition fails before a turn starts when it does not;
* a bound value is checked when it is bound, so a wrong type fails at ``session/new`` rather
  than inside a tool call;
* a requested value is never replaced by a fallback: ``WORKSPACE_UNAVAILABLE`` and
  ``RESOURCE_UNAVAILABLE`` are deliberate failures, not a quiet local execution mode.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field
from pydantic_ai.models import Model
from pydantic_ai.workspaces import Workspace, WorkspaceBackend, WorkspaceRef
from pydantic_ai_harness.ask_user import Answerer
from typing_extensions import TypeIs

from harness.protocol import HarnessError

from .todo import PlanChannel

__all__ = [
    "ANSWERS",
    "MODEL",
    "PLAN",
    "SLOTS",
    "WORKSPACE",
    "ModelInput",
    "ResolvedWorkspace",
    "ResourceBindings",
    "ResourceKind",
    "ResourceRequirement",
    "ResourceSet",
    "ResourceSlot",
    "SlotRegistry",
    "WorkspaceInput",
    "resolve_workspace_value",
    "resource_label",
]

_MISSING = object()

ResourceKind = Literal["model", "workspace", "memory", "store", "service", "custom"]

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)

#: What ``Agent.run(workspace=...)`` accepts, plus a local path the runtime coerces first.
WorkspaceInput = str | os.PathLike[str] | Workspace | WorkspaceBackend | WorkspaceRef

#: A workspace after :func:`resolve_workspace_value` has run.
ResolvedWorkspace = Workspace | WorkspaceBackend | WorkspaceRef

#: What a model resource may hold: a provider string or a native model instance.
ModelInput = Model | str


# --- narrowing -----------------------------------------------------------
#
# A slot's ``coerce`` is the one place an untyped binding becomes a typed value. ``isinstance``
# covers the concrete types and runtime-checkable protocols; ``TypeIs`` covers a
# non-runtime-checkable protocol such as ``Answerer`` without resorting to a cast.


def _identity(value: object) -> object:
    """Accept any binding: the shape a deployment-defined custom slot has."""
    return value


def _is_path(value: object) -> TypeIs[str | os.PathLike[str]]:
    return isinstance(value, os.PathLike | str)


def _is_answerer(value: object) -> TypeIs[Answerer]:
    return callable(value)


def _coerce_workspace(value: object) -> WorkspaceInput | None:
    if _is_path(value):
        return value
    if isinstance(value, Workspace | WorkspaceBackend | WorkspaceRef):
        return value
    return None


def _coerce_model(value: object) -> ModelInput | None:
    return value if isinstance(value, Model | str) else None


def _coerce_answerer(value: object) -> Answerer | None:
    return value if _is_answerer(value) else None


def _coerce_plan_channel(value: object) -> PlanChannel | None:
    return value if isinstance(value, PlanChannel) else None


class ResourceBindings(Protocol):
    """Any mapping of slot-or-name to value: what a deployment supplies.

    Declared through ``items()`` on purpose. A ``Mapping``'s key parameter is invariant, so
    ``Mapping[ResourceSlot[object] | str, object]`` would reject both ``{MODEL: model}`` (slot
    keys) and ``{"model.default": model}`` (string keys); an iterable of pairs accepts either,
    because tuples and unions are covariant.
    """

    def items(self) -> Iterable[tuple[ResourceSlot[object] | str, object]]: ...


# --- slots ---------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceSlot(Generic[T_co]):
    """One typed port: a capability consumes it, a deployment fills it.

    Args:
        name: The serialized name. A requirement with this name, or one of ``aliases``,
            addresses this slot; manifests and host flags spell it as text.
        kind: The resource kind used in client-facing descriptions and error details.
        description: Human text for errors and catalogs.
        aliases: Further serialized names that also satisfy this slot, so a host may bind a
            conventional spelling (``model.default``) while the slot stays canonical.
        missing_code: Error code raised when the slot is required but unbound.
        coerce: The narrowing function applied when the value is bound and when it is read.
            ``None`` from it means "this value does not belong here", so a wrong type is
            rejected at composition rather than surfacing deep inside a tool call.
    """

    name: str
    kind: ResourceKind
    coerce: Callable[[object], T_co | None]
    description: str = ""
    aliases: tuple[str, ...] = ()
    missing_code: str = "RESOURCE_UNAVAILABLE"

    def named(self, name: str) -> bool:
        """Whether a serialized name addresses this slot."""
        return name == self.name or name in self.aliases

    def validate_value(self, value: object) -> None:
        """Reject a value this slot cannot hold."""
        if self.coerce(value) is None:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Resource {self.name!r} expects a {self.kind} value, got {type(value).__name__}.",
                details={
                    "resource": self.name,
                    "kind": self.kind,
                    "bound": type(value).__name__,
                },
            )

    def requirement(
        self,
        *,
        required: bool = True,
        name: str | None = None,
        description: str | None = None,
    ) -> ResourceRequirement:
        """A definition requirement addressing this slot."""
        return ResourceRequirement(
            name=name or self.name,
            kind=self.kind,
            description=description if description is not None else self.description,
            required=required,
        )


WORKSPACE: ResourceSlot[WorkspaceInput] = ResourceSlot(
    name="workspace",
    kind="workspace",
    description="Directory or backend the workspace-dependent tools act on.",
    missing_code="WORKSPACE_UNAVAILABLE",
    coerce=_coerce_workspace,
)
MODEL: ResourceSlot[ModelInput] = ResourceSlot(
    name="model",
    kind="model",
    description="Agent model: a provider string or a native model instance.",
    aliases=("model.default", "model.primary"),
    coerce=_coerce_model,
)
ANSWERS: ResourceSlot[Answerer] = ResourceSlot(
    name="user_answers",
    kind="service",
    description="Answerer that carries agent questions to the connected client.",
    coerce=_coerce_answerer,
)
PLAN: ResourceSlot[PlanChannel] = ResourceSlot(
    name="plan_events",
    kind="service",
    description="Session-scoped plan channel that streams todos to the connected client.",
    coerce=_coerce_plan_channel,
)

_KIND_SLOTS: dict[str, ResourceSlot[object]] = {
    WORKSPACE.kind: WORKSPACE,
    MODEL.kind: MODEL,
}


# --- requirement ---------------------------------------------------------


class ResourceRequirement(BaseModel):
    """One named resource a definition requires.

    A declaration, never a value: it may be serialized into a manifest, sent over a wire, and
    evaluated against a different deployment. Use :meth:`of` to build one from a slot, and
    :attr:`slot` to read back the typed handle it addresses.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    kind: ResourceKind = "custom"
    description: str = ""
    required: bool = True

    @classmethod
    def of(
        cls,
        slot: ResourceSlot[object],
        *,
        required: bool = True,
        name: str | None = None,
        description: str | None = None,
    ) -> ResourceRequirement:
        """The requirement addressing ``slot``, optionally under another serialized name."""
        return slot.requirement(required=required, name=name, description=description)

    @property
    def slot(self) -> ResourceSlot[object]:
        """The typed slot this requirement addresses."""
        return SLOTS.of(self)


def resource_label(requirement: ResourceRequirement) -> str:
    """Human prefix for the requirement's kind, empty for ``custom``."""
    return f"{requirement.kind} " if requirement.kind and requirement.kind != "custom" else ""


# --- registry ------------------------------------------------------------


class SlotRegistry:
    """The known slots, addressable by their serialized names.

    An unknown name is not an error: a deployment may bind a resource that only one plugin
    understands, and a manifest may declare it. Such a name gets an ad-hoc ``custom`` slot,
    cached so that resolving the same name twice yields the same object.
    """

    def __init__(self, slots: Iterable[ResourceSlot[object]] = ()) -> None:
        self._slots: dict[str, ResourceSlot[object]] = {}
        self._custom: dict[tuple[str, str], ResourceSlot[object]] = {}
        for slot in slots:
            self.register(slot)

    def __repr__(self) -> str:
        return f"SlotRegistry(names={sorted(self._slots)})"

    def register(self, slot: ResourceSlot[object]) -> ResourceSlot[object]:
        """Register a slot; one serialized name may belong to only one slot."""
        for name in (slot.name, *slot.aliases):
            existing = self.lookup(name)
            if existing is not None and existing is not slot:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Resource name {name!r} is already registered by slot {existing.name!r}.",
                    details={"resource": name, "slot": existing.name},
                )
        self._slots[slot.name] = slot
        return slot

    def lookup(self, name: str) -> ResourceSlot[object] | None:
        """The registered slot that owns ``name``, or ``None`` for a custom resource."""
        slot = self._slots.get(name)
        if slot is not None:
            return slot
        for candidate in self._slots.values():
            if name in candidate.aliases:
                return candidate
        return None

    def registered(self) -> tuple[ResourceSlot[object], ...]:
        return tuple(self._slots.values())

    def of(self, requirement: ResourceRequirement) -> ResourceSlot[object]:
        """The slot a serialized requirement addresses.

        The declared name wins, so a deployment may rename a resource. Without a match, a
        requirement of a well-known kind falls back to that kind's slot — that is what lets a
        definition declare ``repo`` of kind ``workspace`` and still receive the bound
        workspace. Anything else is a custom slot.
        """
        slot = self.lookup(requirement.name)
        if slot is not None:
            return slot
        kind_slot = _KIND_SLOTS.get(requirement.kind)
        if kind_slot is not None:
            return kind_slot
        return self._custom_slot(requirement)

    def _custom_slot(self, requirement: ResourceRequirement) -> ResourceSlot[object]:
        key = (requirement.name, requirement.kind)
        slot = self._custom.get(key)
        if slot is None:
            slot = ResourceSlot(
                name=requirement.name,
                kind=requirement.kind,
                coerce=_identity,
                description=requirement.description,
            )
            self._custom[key] = slot
        return slot


SLOTS = SlotRegistry((WORKSPACE, MODEL, ANSWERS, PLAN))


# --- values --------------------------------------------------------------


class ResourceSet:
    """The values one deployment or session binds to typed slots.

    Bind with slots — ``ResourceSet({WORKSPACE: backend, MODEL: model})``. A plain string key
    is accepted for values that arrive as text (``--resource name=value`` flags, settings
    files); it is resolved to the slot that owns that name, so a later typed read finds it.
    """

    def __init__(
        self,
        values: ResourceBindings | ResourceSet | None = None,
    ) -> None:
        self._values: dict[str, object] = {}
        if values is None:
            return
        if isinstance(values, ResourceSet):
            self._values.update(values._values)
            return
        for key, value in values.items():
            self.bind(key, value)

    def __repr__(self) -> str:
        return f"ResourceSet(names={sorted(self._values)})"

    def __contains__(self, slot: object) -> bool:
        return isinstance(slot, ResourceSlot) and self.get(slot) is not None

    # --- binding -----------------------------------------------------

    def bind(self, slot: ResourceSlot[object] | str, value: object) -> None:
        """Bind ``value`` to a slot, validating it against the slot's own contract.

        Binding ``None`` removes the value instead: a settings mapping may spell "not bound"
        explicitly, and an absent slot is exactly what a later ``require`` reports.
        """
        target = self._slot_for(slot)
        key = self._key(slot, target)
        if value is None:
            self._values.pop(key, None)
            return
        if target is not None:
            target.validate_value(value)
        self._values[key] = value

    def bind_if_absent(self, slot: ResourceSlot[object] | str, value: object) -> None:
        """Bind only when the slot is still empty, so a deployment binding always wins."""
        target = self._slot_for(slot)
        if self._key(slot, target) not in self._values:
            self.bind(slot, value)

    # --- reading -----------------------------------------------------

    def get(self, slot: ResourceSlot[T]) -> T | None:
        """The value bound to ``slot``, narrowed to the slot's own type, or ``None``."""
        for name in (slot.name, *slot.aliases):
            value = self._values.get(name, _MISSING)
            if value is not _MISSING:
                return slot.coerce(value)
        return None

    def require(self, slot: ResourceSlot[T], *, context: str = "The session") -> T:
        """The value bound to ``slot``, raising the slot's own error when it is absent."""
        value = self.get(slot)
        if value is not None:
            return value
        raise HarnessError(
            slot.missing_code,
            f"{context} requires the {slot.kind} resource {slot.name!r}, "
            "which this deployment does not bind.",
            details={
                "resource": slot.name,
                "kind": slot.kind,
                "description": slot.description,
            },
        )

    def has(self, slot: ResourceSlot[object]) -> bool:
        """Whether a value is bound to ``slot``."""
        return self.get(slot) is not None

    @property
    def values(self) -> dict[str, object]:
        """A copy of the raw bindings, keyed by serialized name."""
        return dict(self._values)

    # --- whole-set operations ----------------------------------------

    def merge(self, other: ResourceBindings | ResourceSet) -> ResourceSet:
        """A new set starting from this one, with ``other`` bound on top."""
        merged = ResourceSet(self)
        if isinstance(other, ResourceSet):
            merged._values.update(other._values)
        else:
            for key, value in other.items():
                merged.bind(key, value)
        return merged

    def missing(self, requirements: Iterable[ResourceRequirement]) -> list[ResourceRequirement]:
        """Required requirements whose slot has no value bound."""
        return [
            requirement
            for requirement in requirements
            if requirement.required and self.get(requirement.slot) is None
        ]

    def validate_requirements(
        self,
        requirements: list[ResourceRequirement],
        *,
        context: str = "session",
    ) -> None:
        """Raise the slot-specific error for an unsupported or missing requirement."""
        for requirement in requirements:
            slot = requirement.slot
            if requirement.kind not in ("custom", slot.kind):
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Resource {requirement.name!r} is declared as {requirement.kind!r} but the "
                    f"slot {slot.name!r} it addresses is {slot.kind!r}.",
                    details={
                        "resource": requirement.name,
                        "declared_kind": requirement.kind,
                        "slot_kind": slot.kind,
                    },
                )
        for requirement in self.missing(requirements):
            slot = requirement.slot
            raise HarnessError(
                slot.missing_code,
                f"{context} requires the {slot.kind} resource {requirement.name!r}, "
                "which this deployment does not bind.",
                details={
                    "resource": requirement.name,
                    "kind": requirement.kind,
                    "description": requirement.description or slot.description,
                },
            )

    # --- internals ---------------------------------------------------

    @staticmethod
    def _slot_for(key: ResourceSlot[object] | str) -> ResourceSlot[object] | None:
        return key if isinstance(key, ResourceSlot) else SLOTS.lookup(key)

    @staticmethod
    def _key(key: ResourceSlot[object] | str, slot: ResourceSlot[object] | None) -> str:
        return slot.name if slot is not None else str(key)


def resolve_workspace_value(value: WorkspaceInput) -> ResolvedWorkspace:
    """Coerce a bound workspace value into something ``Agent.run(workspace=...)`` accepts.

    Deployment bindings may supply a plain filesystem path; the runtime maps it to a native
    ``LocalWorkspaceBackend`` rather than failing inside Pydantic AI. Rich bindings
    (sandbox backends, ``WorkspaceRef``) pass through unchanged.
    """
    if _is_path(value):
        from pydantic_ai.workspaces import LocalWorkspaceBackend

        return LocalWorkspaceBackend(Path(value))
    return value
