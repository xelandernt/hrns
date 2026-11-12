"""Transport-independent conversation handles and server-owned scheduling.

A live session owns its native execution. ACP and Python are interfaces to it;
neither owns the executor. This module deliberately knows nothing about an
outbound ACP client, so a Python-created session can run before any attachment
exists and the same session can attach or replay later.

One handle belongs to one contribution, not to a whole "turn": an execution that
captures several contributions completes all of their handles. A contribution
that is never consumed resolves cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic

import anyio
from acp import schema
from pydantic_ai.messages import ModelMessage
from pydantic_ai.output import OutputDataT
from pydantic_ai.tools import AgentDepsT

from .acp.content import PromptContentBlock, skill_references
from .acp.runtime import RunObserver, SessionRuntime
from .loro.models import Author, Contribution, InteractionRecord
from .loro.sync import SessionSync, SyncTransport


@dataclass(frozen=True)
class TurnResult(Generic[OutputDataT]):
    stop_reason: schema.StopReason
    output: OutputDataT | None = None
    usage: schema.Usage | None = None


@dataclass(frozen=True)
class TurnHandle(Generic[OutputDataT]):
    """One contribution's acceptance/consumption receipt."""

    turn_id: str
    prompt: tuple[PromptContentBlock, ...]
    _future: asyncio.Future[TurnResult[OutputDataT]]

    async def result(self) -> TurnResult[OutputDataT]:
        """Wait without transferring ownership of execution to the waiter."""
        return await asyncio.shield(self._future)


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
        self._accepted: dict[str, TurnHandle[OutputDataT]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._active: TurnHandle[OutputDataT] | None = None
        self._execution: asyncio.Task[schema.PromptResponse] | None = None
        self._captured: set[str] = set()
        self._origins: dict[str, object] = {}
        self._active_contribution_id: str | None = None
        self._sync: SessionSync | None = None
        self._paused = runtime.restored_interrupted
        self._closed = False
        self._revision = 0
        self._last_outcome: str | None = None
        self._last_usage: dict[str, object] | None = None
        runtime.on_state = self._publish
        runtime.on_capture = self._note_capture

    def status(self) -> dict[str, object]:
        if self._paused:
            lifecycle = "paused"
        elif self._execution is not None:
            lifecycle = "waiting" if self._runtime.interactions.waiting else "running"
        else:
            lifecycle = "idle"
        active = self._active_contribution_id or (self._active.turn_id if self._active else None)
        return {
            "sessionId": self.session_id,
            "revision": self._revision,
            "status": lifecycle,
            "activeTurnId": active,
            "lastOutcome": self._last_outcome,
            "lastUsage": self._last_usage,
        }

    def _publish(self) -> None:
        self._revision += 1
        self._runtime.publish_state(self.status())

    def history(self) -> list[ModelMessage]:
        return list(self._runtime.state.history)

    def snapshot(self) -> bytes:
        """The current document image, for an HTTP bootstrap or inspection."""
        return self._runtime.document.snapshot()

    def document_changed(self) -> None:
        """React to an imported document change without blocking the sync loop."""
        self._runtime.document.stamp_ready()
        self._runtime.schedule_interaction_ingest()
        self._start_worker()
        self._publish()

    async def sync(self, transport: SyncTransport) -> None:
        """Serve one connected replica until it disconnects."""
        if self._sync is None:
            self._sync = SessionSync(
                self._runtime.document,
                persist=self._runtime._persist_strict,
                on_change=self.document_changed,
            )
        await self._sync.connect(transport)

    async def submit(
        self,
        prompt: str | Sequence[PromptContentBlock],
        *,
        author: Author | None = None,
    ) -> TurnHandle[OutputDataT]:
        """Accept a ready contribution and return its receipt."""
        return await self._accept(prompt, author=author)

    async def _accept(
        self,
        prompt: str | Sequence[PromptContentBlock],
        origin: object | None = None,
        *,
        author: Author | None = None,
    ) -> TurnHandle[OutputDataT]:
        self._hooks.require_running()
        if self._closed:
            raise RuntimeError("session is closed")
        blocks = (
            (schema.TextContentBlock(text=prompt),) if isinstance(prompt, str) else tuple(prompt)
        )
        skills = skill_references(blocks)
        if skills:
            await self._runtime._resolve_skills(blocks)
        text = "\n".join(
            block.text for block in blocks if isinstance(block, schema.TextContentBlock)
        )
        if author is None:
            author = (
                Author(id="acp", label="ACP client", kind="acp")
                if origin is not None
                else Author(id="python", label="Python", kind="python")
            )
        contribution = await self._runtime.coordinator.accept(
            author=author,
            text=text,
            blocks=[
                block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in blocks
            ],
            skills=skills,
            origin="acp" if origin is not None else "python",
        )
        handle = TurnHandle[OutputDataT](
            contribution.id,
            blocks,
            asyncio.get_running_loop().create_future(),
        )
        # Consume unobserved failures without changing what result() raises.
        handle._future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        if origin is not None:
            self._origins[contribution.id] = origin
        self._accepted[contribution.id] = handle
        self._start_worker()
        return handle

    def _note_capture(self, contributions: tuple[Contribution, ...]) -> None:
        for contribution in contributions:
            self._captured.add(contribution.id)
            if self._active_contribution_id is None:
                self._active_contribution_id = contribution.id
            if self._active is None:
                self._active = self._accepted.get(contribution.id)
            origin = self._origins.get(contribution.id)
            if origin is not None and self._runtime.prompt_origin is None:
                self._runtime.prompt_origin = origin
        self._publish()

    def _start_worker(self) -> None:
        if self._runtime.document.poisoned:
            return
        if (
            not self._paused
            and not self._closed
            and self._worker is None
            and self._runtime.coordinator.eligible()
        ):
            self._worker = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while not self._paused and not self._closed:
                if not self._runtime.coordinator.eligible():
                    break
                self._captured.clear()
                self._active = None
                self._active_contribution_id = None
                self._runtime.prompt_origin = None
                self._publish()
                task = asyncio.create_task(self._runtime.execute())
                self._execution = task
                try:
                    response = await task
                    self._last_outcome = response.stop_reason
                    self._last_usage = (
                        response.usage.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if response.usage
                        else None
                    )
                    self._resolve_captured(
                        TurnResult(response.stop_reason, self._runtime.last_output, response.usage)
                    )
                    if response.stop_reason == "cancelled":
                        self._paused = True
                except asyncio.CancelledError:
                    self._last_outcome = "cancelled"
                    self._last_usage = None
                    self._fail_captured(None)
                    self._paused = True
                except Exception as exc:
                    self._last_outcome = "error"
                    self._last_usage = None
                    self._fail_captured(exc)
                    self._paused = True
                    with anyio.CancelScope(shield=True):
                        await self._runtime.report_error(exc)
                finally:
                    self._active = None
                    self._active_contribution_id = None
                    self._execution = None
                    self._runtime.prompt_origin = None
                    self._publish()
        finally:
            self._worker = None
            if not self._paused and not self._closed and self._runtime.coordinator.eligible():
                self._start_worker()

    def _resolve_captured(self, result: TurnResult[OutputDataT]) -> None:
        for contribution_id in self._captured:
            self._origins.pop(contribution_id, None)
            handle = self._accepted.pop(contribution_id, None)
            if handle is not None and not handle._future.done():
                handle._future.set_result(result)
        self._captured.clear()

    def _fail_captured(self, error: Exception | None) -> None:
        for contribution_id in self._captured:
            self._origins.pop(contribution_id, None)
            handle = self._accepted.pop(contribution_id, None)
            if handle is None or handle._future.done():
                continue
            if error is None:
                handle._future.set_result(TurnResult("cancelled"))
            else:
                handle._future.set_exception(error)
        self._captured.clear()

    async def answer_interaction(
        self,
        interaction_id: str,
        response: Mapping[str, object],
        *,
        actor: str = "loro",
    ) -> bool:
        """Accept the first valid answer from any interface."""
        return await self._runtime.answer_interaction(interaction_id, response, actor=actor)

    async def pending_interactions(self) -> tuple[InteractionRecord, ...]:
        return tuple(self._runtime._interaction_service.pending())

    async def cancel(self) -> None:
        self._paused = True
        if self._execution is not None:
            self._execution.cancel()
            if self._worker is not None:
                await asyncio.shield(self._worker)
        await self._runtime.cancel_interactions("execution cancelled")
        # A cancelled ACP prompt will not be echoed to a future attachment.
        for contribution_id in list(self._origins):
            contribution = self._runtime.document.contribution(contribution_id)
            if contribution is None or contribution.state != "captured":
                self._origins.pop(contribution_id, None)
        await self._runtime.coordinator.interrupt_orphans()

    def start_if_ready(self) -> None:
        """Start an idle session whose document already contains ready input."""
        self._start_worker()
        self._publish()

    async def resume(self) -> None:
        """Explicit recovery from a paused or interrupted session."""
        await self.resume_queue()

    async def clear_inputs(self) -> tuple[str, ...]:
        """Cancel ready input and report the consumed contribution IDs."""
        ready = [contribution.id for contribution in self._runtime.coordinator.pending()]
        await self.clear_queue()
        return tuple(ready)

    async def pending_prompts(self) -> tuple[TurnHandle[OutputDataT], ...]:
        return tuple(
            handle
            for contribution_id, handle in self._accepted.items()
            if (
                (contribution := self._runtime.document.contribution(contribution_id)) is not None
                and contribution.state == "ready"
            )
        )

    async def resume_queue(self) -> None:
        self._hooks.require_running()
        if self._closed:
            raise RuntimeError("session is closed")
        if self._execution is not None and not self._paused:
            return
        self._paused = False
        await self._runtime.coordinator.interrupt_orphans()
        self._runtime.resume_document()
        self._start_worker()
        self._publish()

    async def clear_queue(self) -> tuple[str, ...]:
        cancelled = await self._runtime.coordinator.cancel_ready()
        for contribution_id in cancelled:
            self._origins.pop(contribution_id, None)
            handle = self._accepted.pop(contribution_id, None)
            if handle is not None and not handle._future.done():
                handle._future.set_result(TurnResult("cancelled"))
        return tuple(cancelled)

    async def close(self) -> None:
        async with self._hooks.lock:
            await self._close()

    async def _close(self) -> None:
        self._closed = True
        await self.cancel()
        await self.clear_queue()
        ingest = self._runtime._ingest_task
        if ingest is not None and not ingest.done():
            ingest.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ingest
        if self._sync is not None:
            await self._sync.close()
            self._sync = None
        await self._runtime.flush()
        delivery = self._runtime._delivery
        self._runtime.detach()
        if delivery is not None:
            await asyncio.gather(delivery, return_exceptions=True)
        if self._worker is not None:
            await asyncio.shield(self._worker)
        if self._runtime.observer is not None:
            self._runtime.observer.close()
        self._hooks.forget(self.session_id)
