"""Bind the upstream planning capability to the host's plan channel."""

from __future__ import annotations

from pydantic_ai_harness.planning import InMemoryPlanStore, Planning

from ...registry import BindingContext
from ...resources import PLAN
from .config import TodoConfig

__all__ = ["Todo", "build_todo"]

#: The public name of the native capability. ``Planning`` already owns the tools and
#: the cache-safe reminder; the bundled plugin only supplies the session store.
Todo = Planning


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
        enable_subtasks=config.enable_subtasks,
        inject=config.inject,
        cache_ttl=config.cache_ttl,
    )
