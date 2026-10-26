"""Bind the upstream planning capability to the host's plan channel."""

from __future__ import annotations

from pydantic_ai.capabilities import Capability
from pydantic_ai.tools import AgentDepsT
from pydantic_ai_harness.planning import InMemoryPlanStore, Planning, PlanStore

from ...registry import BindingContext
from ...resources import PLAN
from .config import TodoConfig

__all__ = ["Todo", "build_todo"]

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


class Todo(Capability[AgentDepsT]):
    """Plan tools and static guidance, with no model-request reminder hook."""

    def __init__(self, *, store: PlanStore | None = None, enable_subtasks: bool = False) -> None:
        planning = Planning[AgentDepsT](
            store=store if store is not None else InMemoryPlanStore(),
            tools=["write_plan"],
            descriptions={"write_plan": _WRITE_PLAN_DESCRIPTION},
            enable_subtasks=enable_subtasks,
        )
        toolset = planning.get_toolset()
        assert toolset is not None
        super().__init__(id="planning", instructions=_GUIDANCE, toolsets=[toolset])


def build_todo(config: TodoConfig, binding: BindingContext) -> Todo[object]:
    """Bind planning to the host's session-scoped ``plan_events`` resource.

    The channel is optional by design: a definition may enable planning without one (a
    headless run), and the list then works for the run without notifying a client.
    """
    resources = binding.resources
    channel = resources.get(PLAN) if resources is not None else None
    store = channel.store if channel is not None else InMemoryPlanStore()
    return Todo(
        store=store,
        enable_subtasks=config.enable_subtasks,
    )
