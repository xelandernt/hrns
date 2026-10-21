"""A tiny synchronous event bus for typed client events."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

from .events import ConnectionEvent

Subscriber = Callable[[Any], Awaitable[None] | None]


class EventBus:
    """Publishes client events to in-process subscribers.

    Handlers are called synchronously on the loop that publishes. Coroutine
    handlers are scheduled as tasks so a slow consumer never blocks protocol
    dispatch. Disabled handlers are pruned.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, handler: Subscriber) -> Callable[[], None]:
        """Register a handler and return a function that removes it."""
        if not callable(handler):
            raise TypeError("Event handlers must be callable")
        self._subscribers.append(handler)
        if isinstance(handler, weakref.ref):  # pragma: no cover - defensive
            raise TypeError("Pass a bound method or closure, not a weakref")

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(handler)
            except ValueError:  # pragma: no cover - already removed
                pass

        return unsubscribe

    def unsubscribe_all(self) -> None:
        self._subscribers.clear()

    def publish(self, event: Any) -> None:
        for handler in list(self._subscribers):
            try:
                result = handler(event)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Event handler failed")
                continue
            if asyncio.iscoroutine(result):
                task = asyncio.create_task(result)
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for handler tasks published so far to finish (tests only)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def connection(
        self, state: str, detail: str = "", location: dict[str, str] | None = None
    ) -> ConnectionEvent:
        event = ConnectionEvent(state=state, detail=detail, location=location or {})
        self.publish(event)
        return event
