"""Transport-independent conversation handles and server-owned scheduling.

A live session owns its native execution. ACP and Python are interfaces to it;
neither owns the executor. This module deliberately knows nothing about an
outbound ACP client, so a Python-created session can run before any attachment
exists and the same session can attach or replay later.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic
from uuid import uuid4

from acp import schema
from pydantic_ai.messages import ModelMessage
from pydantic_ai.output import OutputDataT
from pydantic_ai.tools import AgentDepsT

from .acp.content import PromptContentBlock
from .acp.runtime import RunObserver, SessionRuntime


@dataclass(frozen=True)
class TurnResult(Generic[OutputDataT]):
    stop_reason: schema.StopReason
    output: OutputDataT | None = None
    usage: schema.Usage | None = None


@dataclass(frozen=True)
class TurnHandle(Generic[OutputDataT]):
    turn_id: str
    prompt: tuple[PromptContentBlock, ...]
    _future: asyncio.Future[TurnResult[OutputDataT]]

    async def result(self) -> TurnResult[OutputDataT]:
        """Wait without transferring ownership of execution to the waiter."""
        return await asyncio.shield(self._future)


@dataclass(frozen=True)
class _QueuedTurn(Generic[OutputDataT]):
    handle: TurnHandle[OutputDataT]
    origin: object | None


@dataclass(frozen=True, slots=True)
class SessionView:
    history: Callable[[], list[ModelMessage]]
    status: Callable[[], dict[str, object]]
    observer: RunObserver | None


@dataclass(frozen=True, slots=True)
class SessionHooks:
    """The registration-owned operations a session needs, without importing it."""

    require_running: Callable[[], None]
    lock: asyncio.Lock
    forget: Callable[[str], None]


class Session(Generic[AgentDepsT, OutputDataT]):
    """One live conversation, executed independently of any transport request."""

    def __init__(
        self,
        runtime: SessionRuntime[AgentDepsT, OutputDataT],
        hooks: SessionHooks,
    ) -> None:
        self._runtime = runtime
        self._hooks = hooks
        self.session_id = runtime.state.session_id
        self.cwd = runtime.state.cwd
        self._queue: deque[_QueuedTurn[OutputDataT]] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._active: TurnHandle[OutputDataT] | None = None
        self._execution: asyncio.Task[schema.PromptResponse] | None = None
        self._paused = False
        self._closed = False
        self._revision = 0
        self._last_outcome: str | None = None
        self._last_usage: dict[str, object] | None = None
        runtime.on_state = self._publish

    def status(self) -> dict[str, object]:
        return {
            "sessionId": self.session_id,
            "revision": self._revision,
            "status": ("waiting" if self._runtime.interactions.waiting else "running")
            if self._active is not None
            else "idle",
            "activeTurnId": self._active.turn_id if self._active else None,
            "lastOutcome": self._last_outcome,
            "lastUsage": self._last_usage,
        }

    def _publish(self) -> None:
        self._revision += 1
        self._runtime.publish_state(self.status())

    def history(self) -> list[ModelMessage]:
        return list(self._runtime.state.history)

    async def submit(self, prompt: str | Sequence[PromptContentBlock]) -> TurnHandle[OutputDataT]:
        return await self._accept(prompt)

    async def _accept(
        self, prompt: str | Sequence[PromptContentBlock], origin: object | None = None
    ) -> TurnHandle[OutputDataT]:
        self._hooks.require_running()
        if self._closed:
            raise RuntimeError("session is closed")
        blocks = (
            (schema.TextContentBlock(text=prompt),) if isinstance(prompt, str) else tuple(prompt)
        )
        handle = TurnHandle[OutputDataT](
            uuid4().hex, blocks, asyncio.get_running_loop().create_future()
        )
        # Consume unobserved failures without changing what result() raises.
        handle._future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self._queue.append(_QueuedTurn(handle, origin))
        self._start_worker()
        return handle

    def _start_worker(self) -> None:
        if not self._paused and self._queue and self._worker is None:
            self._worker = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while self._queue and not self._paused and not self._closed:
                queued = self._queue.popleft()
                handle = queued.handle
                self._active = handle
                self._runtime.last_output = None
                self._runtime.prompt_origin = queued.origin
                self._publish()
                task = asyncio.create_task(self._runtime.execute(list(handle.prompt)))
                self._execution = task
                try:
                    response = await task
                    self._last_outcome = response.stop_reason
                    self._last_usage = (
                        response.usage.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if response.usage
                        else None
                    )
                    if response.stop_reason == "cancelled":
                        self._paused = True
                    handle._future.set_result(
                        TurnResult(response.stop_reason, self._runtime.last_output, response.usage)
                    )
                except asyncio.CancelledError:
                    self._last_outcome = "cancelled"
                    self._last_usage = None
                    self._paused = True
                    handle._future.set_result(TurnResult("cancelled"))
                except Exception as exc:
                    self._last_outcome = "error"
                    self._last_usage = None
                    self._paused = True
                    handle._future.set_exception(exc)
                finally:
                    self._active = None
                    self._execution = None
                    self._runtime.prompt_origin = None
                    self._publish()
        finally:
            self._worker = None

    async def cancel(self) -> None:
        self._paused = True
        if self._execution is not None:
            self._execution.cancel()
            if self._worker is not None:
                await asyncio.shield(self._worker)

    async def pending_prompts(self) -> tuple[TurnHandle[OutputDataT], ...]:
        return tuple(queued.handle for queued in self._queue)

    async def resume_queue(self) -> None:
        self._hooks.require_running()
        if self._closed:
            raise RuntimeError("session is closed")
        self._paused = False
        self._start_worker()

    async def clear_queue(self) -> None:
        while self._queue:
            self._queue.popleft().handle._future.set_result(TurnResult("cancelled"))

    async def close(self) -> None:
        async with self._hooks.lock:
            await self._close()

    async def _close(self) -> None:
        self._closed = True
        await self.cancel()
        await self.clear_queue()
        async with self._runtime._persist_lock:
            pass
        delivery = self._runtime._delivery
        self._runtime.detach()
        if delivery is not None:
            await asyncio.gather(delivery, return_exceptions=True)
        if self._worker is not None:
            await asyncio.shield(self._worker)
        if self._runtime.observer is not None:
            self._runtime.observer.close()
        self._hooks.forget(self.session_id)
