"""Session-owned decisions; a lost connection never supplies an answer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TypeVar

from acp.interfaces import Client

ResultT = TypeVar("ResultT")


class Interactions:
    def __init__(self) -> None:
        self.client: Client | None = None
        self.generation = 0
        self.waiting = False
        self._changed = asyncio.Event()

    def _signal(self) -> None:
        self.generation += 1
        self._changed.set()
        self._changed = asyncio.Event()

    def attach(self, client: Client) -> None:
        self.client = client
        self._signal()

    def detach(self) -> None:
        self.client = None
        self._signal()

    async def request(self, operation: Callable[[Client], Awaitable[ResultT]]) -> ResultT:
        while True:
            changed = self._changed
            client = self.client
            if client is None:
                await changed.wait()
                continue
            generation = self.generation
            task = asyncio.ensure_future(operation(client))
            disconnected = asyncio.create_task(changed.wait())
            try:
                await asyncio.wait((task, disconnected), return_when=asyncio.FIRST_COMPLETED)
                if generation != self.generation:
                    continue
                try:
                    return task.result()
                except (ConnectionError, EOFError, asyncio.CancelledError):
                    self.detach()
            finally:
                task.cancel()
                disconnected.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
                with suppress(asyncio.CancelledError):
                    await disconnected
