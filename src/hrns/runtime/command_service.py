"""Command request caching, locking, execution, and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from hrns.protocol import (
    CommandCancelRequest,
    CommandCancelResponse,
    CommandCatalogChanged,
    CommandInvokeRequest,
    CommandInvokeResponse,
    HrnsError,
    canonical_hash,
)

if TYPE_CHECKING:
    from .composition import Composition
    from .host import HrnsACPAgent

#: At most this many request-id results are retained per session.
COMMAND_CACHE_PER_SESSION = 100


class CommandService:
    """Owns user-only command dispatch for a host process."""

    def __init__(self, *, agent: HrnsACPAgent) -> None:
        self.agent = agent
        self.tasks: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
        self.active: set[str] = set()
        self.results: dict[str, OrderedDict[str, tuple[str, dict[str, Any]]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def forget(self, session_id: str) -> None:
        self.results.pop(session_id, None)
        self.locks.pop(session_id, None)
        self.active.discard(session_id)

    async def cancel_session(self, session_id: str) -> None:
        cancelled: list[asyncio.Task[dict[str, Any]]] = []
        for key, task in list(self.tasks.items()):
            if key[0] == session_id:
                task.cancel()
                cancelled.append(task)
                self.tasks.pop(key, None)
        for task in cancelled:
            with contextlib.suppress(BaseException):
                await task

    def cancel(self, request: CommandCancelRequest) -> dict[str, Any]:
        task = self.tasks.get((request.session_id, request.request_id))
        if task is None or task.done():
            return CommandCancelResponse(finished=task is not None).to_wire()
        task.cancel()
        return CommandCancelResponse(cancelled=True).to_wire()

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())

    @staticmethod
    def _hash(request: CommandInvokeRequest) -> str:
        return canonical_hash(request.model_dump(mode="json", by_alias=True, exclude_none=True))

    async def invoke(self, request: CommandInvokeRequest) -> dict[str, Any]:
        """Run one user-only command under the session's existing turn lock.

        Duplicate request ids with an identical payload share one execution; reusing an id
        for different arguments is rejected. A session that is running a turn or another
        plugin command rejects the new invocation instead of building another work queue.
        """
        agent = self.agent
        # A command needs the live composition, not a stored row: a session that is composed but
        # not yet used still runs commands, and they stay in memory with it.
        if agent.sessions.composition(request.session_id) is None:
            raise HrnsError("SESSION_CLOSED", "Load the session before invoking a command.")
        digest = self._hash(request)
        cached = self.results.get(request.session_id, {}).get(request.request_id)
        if cached is not None:
            if cached[0] != digest:
                raise HrnsError(
                    "INVALID_CONFIGURATION",
                    "A request id cannot be reused with different command arguments.",
                    details={"requestId": request.request_id},
                )
            return cached[1]
        key = (request.session_id, request.request_id)
        async with self._lock(request.session_id):
            cached = self.results.get(request.session_id, {}).get(request.request_id)
            if cached is not None:
                if cached[0] != digest:
                    raise HrnsError(
                        "INVALID_CONFIGURATION",
                        "A request id cannot be reused with different command arguments.",
                        details={"requestId": request.request_id},
                    )
                return cached[1]
            existing = self.tasks.get(key)
            if existing is None:
                if request.session_id in self.active:
                    raise HrnsError(
                        "COMMAND_BUSY",
                        "The session is already running another plugin command.",
                        details={"sessionId": request.session_id},
                    )
                if agent._turn_lock(request.session_id).locked():
                    raise HrnsError(
                        "COMMAND_BUSY",
                        "The session is already running a turn.",
                        details={"sessionId": request.session_id},
                    )
                task = asyncio.create_task(
                    self._execute(request, digest),
                    name=f"hrns-command-{key[1]}",
                )
                self.tasks[key] = task
                self.active.add(request.session_id)

                def _finished(_: Any, *, key: tuple[str, str] = key) -> None:
                    self.tasks.pop(key, None)
                    if not any(item[0] == key[0] for item in self.tasks):
                        self.active.discard(key[0])

                task.add_done_callback(_finished)
            else:
                task = existing
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise HrnsError(
                    "COMMAND_CANCELLED",
                    "The command was cancelled before it completed.",
                    details={"sessionId": request.session_id, "requestId": request.request_id},
                ) from None
            raise

    async def _execute(self, request: CommandInvokeRequest, digest: str) -> dict[str, Any]:
        agent = self.agent
        session_id = request.session_id
        composition = agent.sessions.require_composition(session_id)
        if request.expected_revision != composition.snapshot.selected_revision:
            raise HrnsError(
                "REVISION_CONFLICT",
                "The configuration changed since the client's expected revision.",
                details={
                    "expectedRevision": request.expected_revision,
                    "selectedRevision": composition.snapshot.selected_revision,
                },
            )
        binding = composition.commands.resolve(request.binding_id, request.command_name)
        unsupported = set(binding.command.required_operations) - set(
            composition.commands.supported_operations
        )
        if unsupported:
            raise HrnsError(
                "UNSUPPORTED_OPERATION",
                "This command needs a conversation operation this host does not support.",
                details={"operations": sorted(unsupported)},
            )
        lock = agent._turn_lock(session_id)
        if lock.locked():
            raise HrnsError(
                "COMMAND_BUSY",
                "The session is already running a turn or another command.",
                details={"sessionId": session_id},
            )
        async with lock:
            # Configuration updates take the same lock, so the binding/revision cannot
            # change between the checks above and the handler call.
            refreshed = agent.sessions.require_composition(session_id)
            if refreshed.snapshot.selected_revision != request.expected_revision:
                raise HrnsError(
                    "REVISION_CONFLICT",
                    "The configuration changed before the command started.",
                    details={"expectedRevision": request.expected_revision},
                )
            result = await composition.commands.invoke(
                binding,
                request,
                history_reader=lambda: agent.session_history(session_id),
            )
        response = CommandInvokeResponse(
            request_id=request.request_id,
            binding_id=request.binding_id,
            command_name=request.command_name,
            result=result,
        ).to_wire()
        cache = self.results.setdefault(session_id, OrderedDict())
        cache[request.request_id] = (digest, response)
        while len(cache) > COMMAND_CACHE_PER_SESSION:
            cache.popitem(last=False)
        return response

    async def send_changed(self, session_id: str, composition: Composition) -> None:
        with contextlib.suppress(Exception):
            await self.agent.fanout.session_notification(
                session_id,
                "hrns/commands/changed",
                CommandCatalogChanged(
                    session_id=session_id,
                    selected_revision=composition.snapshot.selected_revision,
                    commands=composition.commands.descriptors(),
                    supported_operations=sorted(composition.commands.supported_operations),
                ).to_wire(),
            )
