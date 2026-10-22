"""Todo wiring: the upstream planning tools on a session-scoped plan store."""

from __future__ import annotations

from typing import Any

from pydantic_ai_harness.planning import InMemoryPlanStore, Planning

from .config import TodoConfig

__all__ = ["Todo", "build_todo"]

#: The public name of the native capability. ``Planning`` already owns the tools and
#: the cache-safe reminder; the bundled plugin only supplies the session store.
Todo = Planning


def build_todo(config: TodoConfig, binding: Any) -> Planning[Any]:
    """Bind planning to the host's session-scoped ``plan_events`` resource."""
    resources = getattr(binding, "resources", None)
    channel = resources.get("plan_events") if resources is not None else None
    store = getattr(channel, "store", None)
    if store is None:
        # A definition can use this capability without a host channel (a headless run,
        # for example). The list still works for the run, but no client is notified.
        store = InMemoryPlanStore()
    return Planning(
        store=store,
        enable_subtasks=config.enable_subtasks,
        inject=config.inject,
        cache_ttl=config.cache_ttl,
    )
