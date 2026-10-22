"""Session-scoped plan state and ACP plan notifications for the todo capability.

The upstream ``Planning`` capability owns the model-facing tools and the reminder. This
module only keeps one store per ACP session and mirrors every mutation to the connected
client as a standard ``session/update`` plan notification.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import acp
from acp import schema
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem, PlanStore, TaskStatus

__all__ = [
    "PLAN_TABLE",
    "NotifyingPlanStore",
    "PlanChannel",
    "forget_plan",
    "plan_entries",
    "plan_update",
]

PlanPublisher = Callable[[str, list[PlanItem]], Awaitable[None]]

#: The SQLite table the durable plan store uses, shared with :func:`forget_plan`.
PLAN_TABLE = "plan_items"

_STATUS: dict[TaskStatus, schema.PlanEntryStatus] = {
    TaskStatus.pending: "pending",
    TaskStatus.in_progress: "in_progress",
    TaskStatus.completed: "completed",
    # ACP only carries three states. A cancelled step is finished and should not
    # inflate the remaining count; a blocked step is still outstanding.
    TaskStatus.cancelled: "completed",
    TaskStatus.blocked: "pending",
}


def plan_entries(items: list[PlanItem]) -> list[schema.PlanEntry]:
    """Serialize plan steps as standard ACP plan entries."""
    entries: list[schema.PlanEntry] = []
    for item in items:
        content = item.content
        if item.status is TaskStatus.cancelled:
            content = f"{content} (cancelled)"
        elif item.status is TaskStatus.blocked:
            content = f"{content} (blocked)"
        entries.append(
            schema.PlanEntry(
                content=content,
                priority="medium",
                status=_STATUS[item.status],
            )
        )
    return entries


class NotifyingPlanStore:
    """A PlanStore that publishes a complete snapshot after every mutation."""

    def __init__(
        self, inner: PlanStore, notify: Callable[[list[PlanItem]], Awaitable[None]]
    ) -> None:
        self._inner = inner
        self._notify = notify

    async def get_items(self) -> list[PlanItem]:
        return await self._inner.get_items()

    async def set_items(self, items: list[PlanItem]) -> None:
        await self._inner.set_items(items)
        await self._publish()

    async def get_item(self, item_id: str) -> PlanItem | None:
        return await self._inner.get_item(item_id)

    async def add_item(self, item: PlanItem) -> PlanItem:
        item = await self._inner.add_item(item)
        await self._publish()
        return item

    async def update_item(
        self,
        item_id: str,
        *,
        content: str | None = None,
        status: TaskStatus | None = None,
        active_form: str | None = None,
        parent_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> PlanItem | None:
        item = await self._inner.update_item(
            item_id,
            content=content,
            status=status,
            active_form=active_form,
            parent_id=parent_id,
            depends_on=depends_on,
        )
        await self._publish()
        return item

    async def remove_item(self, item_id: str) -> bool:
        removed = await self._inner.remove_item(item_id)
        await self._publish()
        return removed

    async def _publish(self) -> None:
        # A client that cannot receive the update must not turn a successful plan
        # tool call into a failed run.
        try:
            await self._notify(await self._inner.get_items())
        except Exception:
            return


class PlanChannel:
    """The per-session ``plan_events`` resource for the todo capability."""

    def __init__(
        self,
        session_id: str,
        publish: PlanPublisher,
        store: PlanStore | None = None,
    ) -> None:
        self.session_id = session_id
        self._publish_sink = publish
        self.store = NotifyingPlanStore(store or InMemoryPlanStore(), self._publish)

    async def _publish(self, items: list[PlanItem]) -> None:
        await self._publish_sink(self.session_id, items)

    async def publish_current(self) -> None:
        """Re-send a non-empty plan after the session is loaded again."""
        items = await self.store.get_items()
        if items:
            await self._publish(items)


def forget_plan(database: str | Path, session_id: str) -> None:
    """Drop a deleted session's rows from the SQLite plan table, when there is one."""
    try:
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(f"DELETE FROM {PLAN_TABLE} WHERE session = ?", (session_id,))
    except sqlite3.Error:
        return


def plan_update(items: list[PlanItem]) -> schema.AgentPlanUpdate:
    """The standard ACP update for the current plan.

    An empty list is the complete plan, so it clears the client's view without relying on
    the newer ``plan_removed`` capability.
    """
    return acp.update_plan(plan_entries(items))
