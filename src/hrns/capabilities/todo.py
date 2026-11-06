"""Agent-owned todos, independent of client protocols."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import CapabilityEvent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import FunctionToolset


class TodoItem(BaseModel):
    """One entry in an ordered todo snapshot."""

    model_config = ConfigDict(frozen=True)

    content: str = Field(min_length=1)
    priority: Literal["high", "medium", "low"] = "medium"
    status: Literal["pending", "in_progress", "completed"] = "pending"


@dataclass
class TodoStore:
    """Reusable in-memory state; use a separate store for each conversation."""

    items: tuple[TodoItem, ...] = ()


@dataclass(kw_only=True)
class TodoUpdated(CapabilityEvent, namespace="hrns.todo", name="updated"):
    """The complete list after a successful write, including an empty clear."""

    items: tuple[TodoItem, ...]


@dataclass
class _SessionBinding:
    store: TodoStore
    run_id: str | None = None


_session_store: ContextVar[_SessionBinding | None] = ContextVar("hrns_todo_store", default=None)


@contextmanager
def _bind_todo_store(store: TodoStore) -> Iterator[None]:
    """Bind an ACP session's state without mutating the shared agent."""
    token = _session_store.set(_SessionBinding(store))
    try:
        yield
    finally:
        _session_store.reset(token)


@dataclass
class Todo(AbstractCapability[AgentDepsT]):
    """Planning tools with typed events and no model-request reminder injection.

    Standalone runs get fresh state unless ``store`` is supplied. The hrns ACP
    adapter binds its session store so todos survive prompts and session loading.
    """

    store: TodoStore | None = None
    id: str | None = "hrns.todo"
    _resolved_store: TodoStore = field(default_factory=TodoStore, init=False, repr=False)

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> Todo[AgentDepsT]:
        clone = replace(self)
        binding = _session_store.get()
        if binding is not None and binding.run_id in (None, ctx.run_id):
            binding.run_id = ctx.run_id
            clone._resolved_store = binding.store
        else:
            clone._resolved_store = self.store or TodoStore()
        return clone

    def get_instructions(self) -> str:
        return (
            "For multi-step work, use write_todos to maintain an ordered todo list. "
            "Send the complete list on each write, preserving completed steps. "
            "Mark the current step in_progress and finished steps completed. "
            "Use read_todos to inspect the current list. Write an empty list to clear it. "
            "Skip todos for trivial tasks."
        )

    def get_toolset(self) -> FunctionToolset[AgentDepsT]:
        tools = FunctionToolset[AgentDepsT](id="hrns.todo")

        @tools.tool_plain
        async def read_todos() -> tuple[TodoItem, ...]:
            """Read the current ordered todo list."""
            return self._resolved_store.items

        @tools.tool
        async def write_todos(
            ctx: RunContext[AgentDepsT], items: list[TodoItem]
        ) -> tuple[TodoItem, ...]:
            """Replace the complete todo list. An empty list clears it."""
            snapshot = tuple(items)
            self._resolved_store.items = snapshot
            await ctx.emit(TodoUpdated(items=snapshot))
            return snapshot

        return tools
