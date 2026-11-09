"""Agent registrations and process-owned conversation execution."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Generic
from uuid import uuid4

import acp
from acp import schema
from acp.interfaces import Client
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.output import OutputDataT
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.usage import UsageLimits

from .content import PromptContentBlock
from .models import ModelResolver
from .permission import PermissionPolicy
from .presentation import ToolCallPresenter
from .runtime import RunObserver, SessionRuntime
from .session import AttachmentConfigFunc, SessionConfigFunc
from .store import InMemorySessionStore, SessionStore

if TYPE_CHECKING:
    from fastapi import FastAPI

    from ..fastapi import Authenticator
    from .adapter import PydanticAIACPAgent


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
    origin: Client | None


@dataclass(frozen=True)
class SessionView:
    history: Callable[[], list[ModelMessage]]
    status: Callable[[], dict[str, object]]
    observer: RunObserver | None


class Session(Generic[AgentDepsT, OutputDataT]):
    def __init__(
        self,
        registration: RegisteredAgent[AgentDepsT, OutputDataT],
        runtime: SessionRuntime[AgentDepsT, OutputDataT],
    ) -> None:
        self._registration = registration
        self._runtime = runtime
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
        self, prompt: str | Sequence[PromptContentBlock], origin: Client | None = None
    ) -> TurnHandle[OutputDataT]:
        self._registration._server._require_running()
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
        self._registration._server._require_running()
        if self._closed:
            raise RuntimeError("session is closed")
        self._paused = False
        self._start_worker()

    async def clear_queue(self) -> None:
        while self._queue:
            self._queue.popleft().handle._future.set_result(TurnResult("cancelled"))

    async def close(self) -> None:
        async with self._registration._lock:
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
        self._registration._sessions.pop(self.session_id, None)
        self._registration._views.pop(self.session_id, None)


@dataclass
class _RegistrationEntry:
    adapter: Callable[[], acp.Agent]
    close: Callable[[], Awaitable[None]]
    views: dict[str, SessionView]


class RegisteredAgent(Generic[AgentDepsT, OutputDataT]):
    def __init__(
        self,
        server: AcpServer,
        agent_id: str,
        factory: Callable[[], AbstractAgent[AgentDepsT, OutputDataT]],
        *,
        deps: AgentDepsT,
        session_store: SessionStore,
        model_resolver: ModelResolver | None,
        session_config: SessionConfigFunc[AgentDepsT] | None,
        attachment_config: AttachmentConfigFunc[AgentDepsT] | None,
        permission_policy: PermissionPolicy | None,
        usage_limits: UsageLimits | None,
        tool_presenter: ToolCallPresenter | None,
        mcp_capabilities: schema.McpCapabilities | None,
    ) -> None:
        self._server = server
        self.agent_id = agent_id
        self._factory = factory
        self._deps = deps
        self.store = session_store
        self.resolver = model_resolver
        self._session_config = session_config
        self._attachment_config = attachment_config
        self._permission_policy = permission_policy
        self._usage_limits = usage_limits
        self._tool_presenter = tool_presenter
        self.mcp_capabilities = mcp_capabilities or schema.McpCapabilities()
        self._sessions: dict[str, Session[AgentDepsT, OutputDataT]] = {}
        self._views: dict[str, SessionView] = {}
        self._lock = asyncio.Lock()

    def _make_runtime(self) -> SessionRuntime[AgentDepsT, OutputDataT]:
        return SessionRuntime(
            self._factory(),
            deps=self._deps,
            agent_id=self.agent_id,
            session_store=self.store,
            model_resolver=self.resolver,
            session_config=self._session_config,
            attachment_config=self._attachment_config,
            permission_policy=self._permission_policy,
            usage_limits=self._usage_limits,
            tool_presenter=self._tool_presenter,
        )

    def _remember(
        self, runtime: SessionRuntime[AgentDepsT, OutputDataT]
    ) -> Session[AgentDepsT, OutputDataT]:
        self._server._require_running()
        session = Session(self, runtime)
        runtime.observer = self._server._make_observer(session.session_id, session.history())
        session._publish()
        self._sessions[session.session_id] = session
        self._views[session.session_id] = SessionView(
            session.history, session.status, runtime.observer
        )
        return session

    async def new_session(self, *, cwd: str) -> Session[AgentDepsT, OutputDataT]:
        self._server._require_running()
        runtime = self._make_runtime()
        await runtime.create(str(Path(cwd).resolve()))
        return self._remember(runtime)

    async def get_session(self, session_id: str) -> Session[AgentDepsT, OutputDataT]:
        self._server._require_running()
        async with self._lock:
            self._server._require_running()
            if session_id in self._sessions:
                return self._sessions[session_id]
            stored = await self.store.load(session_id)
            if stored is None:
                raise KeyError(session_id)
            runtime = self._make_runtime()
            await runtime.restore(session_id, stored)
            return self._remember(runtime)

    def connection(self) -> PydanticAIACPAgent[AgentDepsT, OutputDataT]:
        from .adapter import PydanticAIACPAgent

        return PydanticAIACPAgent(self)

    async def close(self) -> None:
        await asyncio.gather(*(session.close() for session in tuple(self._sessions.values())))


class AcpServer:
    """Own registered agents and live sessions in one process and event loop."""

    def __init__(self) -> None:
        self._registrations: dict[str, _RegistrationEntry] = {}
        self._stores: list[SessionStore] = []
        self._resolvers: list[ModelResolver] = []
        self._running = False
        self._started = False

    def add_agent(
        self,
        agent_id: str,
        *,
        factory: Callable[[], AbstractAgent[AgentDepsT, OutputDataT]],
        deps: AgentDepsT = None,
        session_store: SessionStore | None = None,
        model_resolver: ModelResolver | None = None,
        session_config: SessionConfigFunc[AgentDepsT] | None = None,
        attachment_config: AttachmentConfigFunc[AgentDepsT] | None = None,
        permission_policy: PermissionPolicy | None = None,
        usage_limits: UsageLimits | None = None,
        tool_presenter: ToolCallPresenter | None = None,
        mcp_capabilities: schema.McpCapabilities | None = None,
    ) -> RegisteredAgent[AgentDepsT, OutputDataT]:
        if self._started:
            raise RuntimeError("register agents before starting the server")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", agent_id):
            raise ValueError("agent_id must be a safe route segment")
        if agent_id in self._registrations:
            raise ValueError(f"agent already registered: {agent_id}")
        store = session_store if session_store is not None else InMemorySessionStore()
        if any(store is prior for prior in self._stores):
            raise ValueError("give each agent a separate store or explicit partition instance")
        if model_resolver is not None and any(model_resolver is prior for prior in self._resolvers):
            raise ValueError("give each agent its own model resolver")
        if mcp_capabilities is not None and attachment_config is None:
            raise ValueError("client MCP requires attachment_config")
        registration = RegisteredAgent(
            self,
            agent_id,
            factory,
            deps=deps,
            session_store=store,
            model_resolver=model_resolver,
            session_config=session_config,
            attachment_config=attachment_config,
            permission_policy=permission_policy,
            usage_limits=usage_limits,
            tool_presenter=tool_presenter,
            mcp_capabilities=mcp_capabilities,
        )
        self._registrations[agent_id] = _RegistrationEntry(
            registration.connection, registration.close, registration._views
        )
        self._stores.append(store)
        if model_resolver is not None:
            self._resolvers.append(model_resolver)
        return registration

    def _require_running(self) -> None:
        if not self._running:
            raise RuntimeError("use 'async with server' before creating or running sessions")

    def _make_observer(self, session_id: str, history: list[ModelMessage]) -> RunObserver | None:
        return None

    async def __aenter__(self) -> AcpServer:
        if self._started:
            raise RuntimeError("server lifecycle can only be entered once")
        self._started = self._running = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._running = False
        await asyncio.gather(*(entry.close() for entry in self._registrations.values()))

    def mount_acp(
        self, app: FastAPI, *, prefix: str = "/acp", authenticate: Authenticator | None = None
    ) -> None:
        from ..fastapi import mount_acp_routes

        mount_acp_routes(self, app, prefix=prefix, authenticate=authenticate)
