"""The session's resources as typed run dependencies.

Pydantic AI's own dependency injection is the framework's mechanism for handing services to
tools, instructions and capabilities: ``Agent(deps_type=...)`` makes ``RunContext.deps``
typed, and ``AbstractCapability[Deps]`` says a capability needs those dependencies. A
session's bound resources are exactly such a bundle, so the runtime exposes them as ``deps``
instead of inventing a second access path beside the framework.

Two sides, one bundle:

* the *binding* side stays validated and serializable — a definition declares
  ``ResourceRequirement``s, a deployment binds values, and ``ResourceSet`` checks them at
  session start;
* the *consumption* side is this dataclass, so a tool, instruction function, factory or
  capability writes ``ctx.deps.workspace`` and the type checker sees the concrete type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic_ai_harness.ask_user import Answerer

from .resources import (
    ANSWERS,
    MODEL,
    PLAN,
    WORKSPACE,
    ModelInput,
    ResolvedWorkspace,
    ResourceSet,
    resolve_workspace_value,
)
from .todo import PlanChannel

__all__ = ["HrnsDeps"]


@dataclass(frozen=True, slots=True)
class HrnsDeps:
    """Typed view of one session's resources, available as ``ctx.deps`` in every run.

    A deployment that needs more than these fields passes its own deps object to
    ``compose_definition(deps=...)``; this bundle is the default when it passes none.
    ``custom`` keeps any resource a manifest declared or a plugin registered under its own
    name, so extension code can read ``ctx.deps.custom["github_token"]`` — the value is
    ``object`` because only the plugin that declared it knows the type, which keeps the
    narrowing at that plugin's boundary instead of leaking ``Any`` through the bundle.
    """

    workspace: ResolvedWorkspace | None = None
    """The run's workspace: a backend or ``WorkspaceRef``, or ``None`` when undeclared."""
    model: ModelInput | None = None
    """The selected model resource: a provider string or a native model instance."""
    answers: Answerer | None = None
    """The answerer that carries questions to the connected client."""
    plan: PlanChannel | None = None
    """The session's plan channel, when the host provides one."""
    custom: Mapping[str, object] = field(default_factory=dict)
    """Every other bound resource, keyed by its serialized name."""

    @classmethod
    def from_resources(
        cls,
        resources: ResourceSet,
        *,
        workspace: ResolvedWorkspace | None = None,
    ) -> HrnsDeps:
        """Project validated bindings onto the typed bundle.

        ``workspace`` accepts the already-resolved run workspace (a path coerced to a
        backend), so ``ctx.deps.workspace`` is the same object the run uses rather than the
        raw deployment value.
        """
        raw = resources.get(WORKSPACE)
        resolved = (
            workspace
            if workspace is not None
            else (resolve_workspace_value(raw) if raw is not None else None)
        )
        known = {WORKSPACE.name, MODEL.name, ANSWERS.name, PLAN.name, *MODEL.aliases}
        return cls(
            workspace=resolved,
            model=resources.get(MODEL),
            answers=resources.get(ANSWERS),
            plan=resources.get(PLAN),
            custom={name: value for name, value in resources.values.items() if name not in known},
        )
