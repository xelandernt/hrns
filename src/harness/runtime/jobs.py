"""Session-owned work that intentionally outlives a single run.

A capability does not gain background execution implicitly. Work that survives a turn
must be registered here with its binding and active configuration revision. Disabling a
binding cancels and drains its jobs; if cancellation cannot be confirmed, the disable is
rejected with ``CAPABILITY_BUSY`` rather than claiming an effect it did not have.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["OwnedJob", "SessionJobManager"]


@dataclass(eq=False)
class OwnedJob:
    job_id: str
    binding_id: str
    revision: int
    description: str = ""
    task: asyncio.Task[Any] | None = None
    cancel_callback: Callable[[], Awaitable[None] | None] | None = None
    external: bool = False
    completed: bool = False

    def is_active(self) -> bool:
        if self.completed:
            return False
        if self.task is not None:
            return not self.task.done()
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "binding_id": self.binding_id,
            "revision": self.revision,
            "description": self.description,
            "external": self.external,
            "active": self.is_active(),
        }


async def _await_any(awaitable: Awaitable[Any]) -> Any:
    """Run any awaitable as a coroutine so it can be owned by an ``asyncio.Task``."""
    return await awaitable


class SessionJobManager:
    """Tracks explicitly registered session jobs, isolated by binding identity."""

    def __init__(self) -> None:
        self._jobs: dict[str, OwnedJob] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"job-{self._counter}"

    def register(
        self,
        binding_id: str,
        revision: int,
        awaitable: Awaitable[Any],
        *,
        description: str = "",
        name: str | None = None,
        context: Any = None,
    ) -> OwnedJob:
        """Start an awaitable owned by ``binding_id``; exceptions are not logged as unhandled.

        ``context`` lets the session detach explicit background work from its run-scoped
        task tracking: jobs must survive the turn that registered them.
        """
        job = OwnedJob(
            job_id=self._next_id(),
            binding_id=binding_id,
            revision=revision,
            description=description,
        )
        if context is None:
            task = asyncio.create_task(
                _await_any(awaitable), name=name or f"harness:{binding_id}:{job.job_id}"
            )
        else:
            task = asyncio.create_task(
                _await_any(awaitable),
                name=name or f"harness:{binding_id}:{job.job_id}",
                context=context,
            )
        job.task = task
        self._jobs[job.job_id] = job

        def finished(done: asyncio.Task[Any]) -> None:
            job.completed = True
            with contextlib.suppress(BaseException):
                done.exception()
            self._jobs.pop(job.job_id, None)

        task.add_done_callback(finished)
        return job

    def register_external(
        self,
        binding_id: str,
        revision: int,
        *,
        cancel: Callable[[], Awaitable[None] | None] | None = None,
        description: str = "",
    ) -> OwnedJob:
        """Track work whose lifetime belongs to an external system.

        There is no task to observe. A successful cancellation callback acknowledges
        that the remote work has stopped and clears the job; without a callback (or if
        it fails), the job remains busy until :meth:`complete`. A toggle cannot undo
        effects the provider already applied.
        """
        job = OwnedJob(
            job_id=self._next_id(),
            binding_id=binding_id,
            revision=revision,
            description=description,
            cancel_callback=cancel,
            external=True,
        )
        self._jobs[job.job_id] = job
        return job

    def complete(self, job_or_id: OwnedJob | str) -> None:
        job_id = job_or_id.job_id if isinstance(job_or_id, OwnedJob) else job_or_id
        job = self._jobs.pop(job_id, None)
        if job is not None:
            job.completed = True

    def has_active(self, binding_id: str) -> bool:
        return any(job.binding_id == binding_id and job.is_active() for job in self._jobs.values())

    def busy_bindings(self) -> set[str]:
        return {job.binding_id for job in self._jobs.values() if job.is_active()}

    def tasks(self) -> set[asyncio.Task[Any]]:
        """Live task objects owned explicitly by this job manager."""
        return {
            job.task
            for job in self._jobs.values()
            if job.task is not None and job.is_active()
        }

    def snapshot(self) -> list[dict[str, Any]]:
        return [job.to_dict() for job in self._jobs.values() if job.is_active()]

    async def cancel_binding(self, binding_id: str, *, timeout: float = 5.0) -> bool:
        """Cancel and drain one binding's work. Returns whether everything is settled."""
        jobs = [job for job in self._jobs.values() if job.binding_id == binding_id and job.is_active()]
        if not jobs:
            return True
        tasks: list[asyncio.Task[Any]] = []
        for job in jobs:
            if job.task is not None:
                job.task.cancel()
                tasks.append(job.task)
            elif job.cancel_callback is not None:
                try:
                    result = job.cancel_callback()
                    if result is not None and hasattr(result, "__await__"):
                        await result
                except BaseException:
                    # Unconfirmed cancellation stays busy until complete_external.
                    continue
                job.completed = True
                self._jobs.pop(job.job_id, None)
            # An external job without a successful cancellation callback remains
            # tracked: already submitted provider effects cannot be retracted.
        if tasks:
            await asyncio.wait(tasks, timeout=max(0.0, timeout))
        return not any(job.binding_id == binding_id and job.is_active() for job in self._jobs.values())

    async def close(self, *, timeout: float = 10.0) -> bool:
        """Cancel every job. Returns ``False`` if any task or external job remains."""
        settled = True
        for binding_id in sorted(self.busy_bindings()):
            settled = await self.cancel_binding(binding_id, timeout=timeout) and settled
        return settled
