"""Bind the upstream planning capability to the host's plan channel."""

from __future__ import annotations

from pydantic_ai_harness.planning import InMemoryPlanStore, Planning

from ...registry import BindingContext
from ...resources import PLAN
from .config import TodoConfig

__all__ = ["Todo", "build_todo"]

#: The public name of the native capability. The bundled factory selects its tool
#: surface and guidance and supplies the session store.
Todo = Planning

_GUIDANCE = """\
For multi-step work, use `write_plan` to keep a short checklist of outcomes. Pass the
complete list when updating it, with at most one step `in_progress`. Update it at meaningful
milestones and combine progress updates with independent work in the same response when
possible. Do not spend separate turns just updating progress. Mark a step `completed` only
after tool results confirm success.
"""

_WRITE_PLAN_DESCRIPTION = (
    "Create or replace the complete plan with a short checklist. Include unchanged steps "
    "and keep at most one step in_progress. Update at meaningful milestones; mark steps "
    "completed only after tool results confirm success."
)


def build_todo(config: TodoConfig, binding: BindingContext) -> Planning[object]:
    """Bind planning to the host's session-scoped ``plan_events`` resource.

    The channel is optional by design: a definition may enable planning without one (a
    headless run), and the list then works for the run without notifying a client.
    """
    resources = binding.resources
    channel = resources.get(PLAN) if resources is not None else None
    store = channel.store if channel is not None else InMemoryPlanStore()
    return Planning(
        store=store,
        tools=["write_plan"],
        guidance=_GUIDANCE,
        descriptions={"write_plan": _WRITE_PLAN_DESCRIPTION},
        enable_subtasks=config.enable_subtasks,
        inject=config.inject,
        cache_ttl=config.cache_ttl,
    )
