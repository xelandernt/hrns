"""Agent registrations and process-owned conversation execution."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Generic
from uuid import uuid4

import acp
from acp import schema
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.output import OutputDataT
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.usage import UsageLimits

from .._types import PrincipalT
from ..loro.document import SessionDocument
from ..loro.registration import LoroRegistration
from ..loro.store import (
    DeletableSessionStore,
    DocumentCheckpoint,
    ListableSessionStore,
    SessionAccess,
    SessionSummary,
)
from ..session import Session, SessionHooks, SessionView
from .models import ModelResolver
from .permission import PermissionPolicy
from .presentation import ToolCallPresenter
from .runtime import RunObserver, SessionRuntime
from .session import AttachmentConfigFunc, SessionConfigFunc, SessionContext
from .store import InMemoryDocumentStore, SessionStore

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from ..fastapi import Authenticator
    from .adapter import PydanticAIACPAgent


@dataclass
class _RegistrationEntry(Generic[PrincipalT]):
    adapter: Callable[[PrincipalT | None], acp.Agent]
    close: Callable[[], Awaitable[None]]
    views: dict[str, SessionView]
    store: SessionStore[PrincipalT]


class RegisteredAgent(Generic[PrincipalT, AgentDepsT, OutputDataT]):
    def __init__(
        self,
        server: AcpServer[PrincipalT],
        agent_id: str,
        factory: Callable[
            [SessionContext],
            AbstractAgent[AgentDepsT, OutputDataT]
            | Awaitable[AbstractAgent[AgentDepsT, OutputDataT]],
        ],
        *,
        deps: AgentDepsT,
        session_store: SessionStore[PrincipalT],
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

    async def _make_runtime(
        self, context: SessionContext, document: SessionDocument
    ) -> SessionRuntime[AgentDepsT, OutputDataT]:
        result = self._factory(context)
        agent = await result if isawaitable(result) else result
        return SessionRuntime(
            agent,
            deps=self._deps,
            agent_id=self.agent_id,
            session_store=self.store,
            document=document,
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
        hooks = SessionHooks(
            require_running=self._server._require_running,
            lock=self._lock,
            forget=self._forget,
        )
        session = Session(runtime, hooks)
        runtime.observer = self._server._make_observer(session.session_id, session.history())
        session._publish()
        self._sessions[session.session_id] = session
        self._views[session.session_id] = SessionView(
            session.history, session.status, runtime.observer
        )
        session.start_if_ready()
        return session

    def _forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._views.pop(session_id, None)

    async def new_session(
        self, *, cwd: str, principal: PrincipalT | None = None
    ) -> Session[AgentDepsT, OutputDataT]:
        self._server._require_running()
        context = SessionContext(
            agent_id=self.agent_id, session_id=uuid4().hex, cwd=str(Path(cwd).resolve())
        )
        document = SessionDocument.create(
            agent_id=self.agent_id, session_id=context.session_id, cwd=context.cwd
        )
        await self.store.create(
            context.session_id,
            checkpoint=DocumentCheckpoint(
                session_id=context.session_id,
                agent_id=self.agent_id,
                cwd=context.cwd,
                snapshot=document.snapshot(),
                pending=document.pending_updates(),
                schema_version=document.schema_version,
                updated_at=datetime.now(UTC),
            ),
            principal=principal,
        )
        runtime = await self._make_runtime(context, document)
        await runtime.create(context.session_id, context.cwd)
        return self._remember(runtime)

    async def get_session(
        self,
        session_id: str,
        *,
        principal: PrincipalT | None = None,
        operation: SessionAccess = "session/load",
    ) -> Session[AgentDepsT, OutputDataT]:
        self._server._require_running()
        async with self._lock:
            self._server._require_running()
            stored = await self.store.load(session_id, principal=principal, operation=operation)
            if stored is None:
                raise KeyError(session_id)
            if session_id in self._sessions:
                return self._sessions[session_id]
            document = SessionDocument.restore(stored.snapshot, stored.pending)
            runtime = await self._make_runtime(
                SessionContext(agent_id=self.agent_id, session_id=session_id, cwd=stored.cwd),
                document,
            )
            await runtime.restore(session_id, stored)
            return self._remember(runtime)

    async def cancel_session(self, session_id: str) -> None:
        """Cancel the active turn and pause queued prompts; raise KeyError if unknown."""
        session = await self.get_session(session_id, operation="session/cancel")
        await session.cancel()

    async def close_session(self, session_id: str) -> None:
        """Close the session, preserving stored history; raise KeyError if unknown."""
        session = await self.get_session(session_id, operation="session/close")
        await session.close()

    async def list_sessions(
        self,
        *,
        principal: PrincipalT | None = None,
        cwd: str | None = None,
        cursor: str | None = None,
    ) -> tuple[Sequence[SessionSummary], str | None]:
        store = self.store
        if not isinstance(store, ListableSessionStore):
            raise NotImplementedError("this registration's store cannot list sessions")
        return await store.list_sessions(principal=principal, cwd=cwd, cursor=cursor)

    async def delete_session(self, session_id: str, *, principal: PrincipalT | None = None) -> None:
        """Delete a live or stored session; raise KeyError if unknown."""
        self._server._require_running()
        async with self._lock:
            stored = await self.store.load(
                session_id, principal=principal, operation="session/delete"
            )
            if stored is None:
                raise KeyError(session_id)
            store = self.store
            if not isinstance(store, DeletableSessionStore):
                raise NotImplementedError("this registration's store cannot delete sessions")
            session = self._sessions.get(session_id)
            if session is not None:
                await session._close()
            await store.delete(session_id, principal=principal)

    def connection(
        self, principal: PrincipalT | None = None
    ) -> PydanticAIACPAgent[PrincipalT, AgentDepsT, OutputDataT]:
        from .adapter import PydanticAIACPAgent

        return PydanticAIACPAgent(self, principal=principal)

    async def close(self) -> None:
        await asyncio.gather(*(session.close() for session in tuple(self._sessions.values())))


class AcpServer(Generic[PrincipalT]):
    """Own registered agents and live sessions in one process and event loop."""

    def __init__(self) -> None:
        self._registrations: dict[str, _RegistrationEntry[PrincipalT]] = {}
        self._loro: dict[str, LoroRegistration[PrincipalT]] = {}
        self._stores: list[SessionStore[PrincipalT]] = []
        self._resolvers: list[ModelResolver] = []
        self._running = False
        self._started = False

    def add_agent(
        self,
        agent_id: str,
        *,
        factory: Callable[
            [SessionContext],
            AbstractAgent[AgentDepsT, OutputDataT]
            | Awaitable[AbstractAgent[AgentDepsT, OutputDataT]],
        ],
        deps: AgentDepsT = None,
        session_store: SessionStore[PrincipalT] | None = None,
        model_resolver: ModelResolver | None = None,
        session_config: SessionConfigFunc[AgentDepsT] | None = None,
        attachment_config: AttachmentConfigFunc[AgentDepsT] | None = None,
        permission_policy: PermissionPolicy | None = None,
        usage_limits: UsageLimits | None = None,
        tool_presenter: ToolCallPresenter | None = None,
        mcp_capabilities: schema.McpCapabilities | None = None,
    ) -> RegisteredAgent[PrincipalT, AgentDepsT, OutputDataT]:
        if self._started:
            raise RuntimeError("register agents before starting the server")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", agent_id):
            raise ValueError("agent_id must be a safe route segment")
        if agent_id in self._registrations:
            raise ValueError(f"agent already registered: {agent_id}")
        store = session_store if session_store is not None else InMemoryDocumentStore[PrincipalT]()
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
            registration.connection, registration.close, registration._views, registration.store
        )
        list_sessions = (
            (
                lambda principal, cwd, cursor: registration.list_sessions(
                    principal=principal, cwd=cwd, cursor=cursor
                )
            )
            if isinstance(store, ListableSessionStore)
            else None
        )
        delete_session = (
            (
                lambda session_id, principal: registration.delete_session(
                    session_id, principal=principal
                )
            )
            if isinstance(store, DeletableSessionStore)
            else None
        )
        loro: LoroRegistration[PrincipalT] = LoroRegistration(
            create=lambda cwd, principal: registration.new_session(cwd=cwd, principal=principal),
            get=lambda session_id, principal, operation: registration.get_session(
                session_id, principal=principal, operation=operation
            ),
            list=list_sessions,
            delete=delete_session,
        )
        self._loro[agent_id] = loro
        self._stores.append(store)
        if model_resolver is not None:
            self._resolvers.append(model_resolver)
        return registration

    def _require_running(self) -> None:
        if not self._running:
            raise RuntimeError("use 'async with server' before creating or running sessions")

    def _make_observer(self, session_id: str, history: list[ModelMessage]) -> RunObserver | None:
        return None

    async def __aenter__(self) -> AcpServer[PrincipalT]:
        if self._started:
            raise RuntimeError("server lifecycle can only be entered once")
        self._started = self._running = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._running = False
        results = await asyncio.gather(
            *(entry.close() for entry in self._registrations.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.exception("failed to close an agent registration", exc_info=result)

    def mount_loro(
        self,
        app: FastAPI,
        *,
        prefix: str = "/loro",
        authenticate: Authenticator[PrincipalT] | None = None,
    ) -> None:
        from ..loro.routes import mount_loro_routes

        mount_loro_routes(self, app, prefix=prefix, authenticate=authenticate)

    def mount_acp(
        self,
        app: FastAPI,
        *,
        prefix: str = "/acp",
        authenticate: Authenticator[PrincipalT] | None = None,
    ) -> None:
        from ..fastapi import mount_acp_routes

        mount_acp_routes(self, app, prefix=prefix, authenticate=authenticate)
