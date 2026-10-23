"""Register native agent definitions on an existing FastAPI application."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket

from harness.runtime import (
    MODEL,
    AgentDefinition,
    PluginRegistry,
    ResourceBindings,
    ResourceSet,
    SessionStore,
)
from harness.runtime.acp import canonical_root
from harness.runtime.host import HarnessACPAgent, HostBundle, HostPolicy
from harness.runtime.local import thinking_support

from .catalog import CursorCodec
from .errors import ApiError
from .models import AuthenticationDependency, Principal, RegisteredAgent


async def _local_principal() -> Principal:
    return Principal("local")


class AgentServer:
    """Own a shared store and serve registered agents inside FastAPI."""

    def __init__(
        self,
        *,
        store: SessionStore,
        authenticate: AuthenticationDependency | None = None,
        registry: PluginRegistry | None = None,
    ) -> None:
        if not isinstance(store, SessionStore):
            raise TypeError("store must be a SessionStore/PostgresSessionStore instance")
        self.store = store
        self.authenticate: AuthenticationDependency = (
            authenticate if authenticate is not None else _local_principal
        )
        if registry is None:
            registry = PluginRegistry(allow_local_binding_creation=False)
            registry.discover()
        self.registry = registry
        self._agents: dict[str, RegisteredAgent] = {}
        self._cursor_codec = CursorCodec()
        self._mounted = False
        self._running = False
        self._connections: dict[WebSocket, asyncio.Task[Any]] = {}
        self._principal_connections: dict[str, int] = {}

    def add_agent(
        self,
        definition: AgentDefinition,
        *,
        resources: ResourceSet | ResourceBindings | None = None,
        models: Sequence[str] = (),
        allowed_cwds: Sequence[str | Path] | None = None,
        default_cwd: str | Path | None = None,
    ) -> None:
        """Register a definition before mounting; IDs must be unique URL segments."""
        if self._mounted:
            raise RuntimeError("Register agents before calling mount()")
        if not isinstance(definition, AgentDefinition):
            raise TypeError("definition must be an AgentDefinition")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}", definition.agent_id):
            raise ValueError("agent_id must be a URL segment using letters, digits, ._-")
        if definition.agent_id in self._agents:
            raise ValueError(f"Agent {definition.agent_id!r} is already registered")
        if isinstance(allowed_cwds, (str, Path)):
            raise TypeError("allowed_cwds must be a sequence of paths")
        roots = tuple(
            dict.fromkeys(
                canonical_root(str(p))
                for p in (allowed_cwds if allowed_cwds is not None else [Path.cwd()])
            )
        )
        if not roots:
            raise ValueError("At least one allowed workspace root is required")
        cwd = canonical_root(str(default_cwd)) if default_cwd is not None else roots[0]
        if cwd not in roots:
            raise ValueError("default_cwd must be listed in allowed_cwds")
        if isinstance(models, str) or any(
            not isinstance(model, str) or not model.strip() for model in models
        ):
            raise ValueError("models must be a sequence of non-empty model IDs")
        bound = resources if isinstance(resources, ResourceSet) else ResourceSet(resources)
        selected = bound.get(MODEL) or definition.model
        offered = tuple(
            dict.fromkeys([*([selected] if isinstance(selected, str) else []), *models])
        )
        self._agents[definition.agent_id] = RegisteredAgent(
            definition=definition.snapshot(),
            allowed_cwds=roots,
            default_cwd=cwd,
            models=offered,
            resources=bound.values,
        )

    def mount(self, app: FastAPI, *, prefix: str = "") -> None:
        """Add catalog/ACP routes and storage lifespan to an existing app."""
        from .api import build_router

        if self._mounted:
            raise RuntimeError("An AgentServer can only be mounted once")
        if not self._agents:
            raise ValueError("Register at least one agent before mounting")
        app.include_router(build_router(self, prefix=prefix), prefix=prefix)
        self._mounted = True

    def available_agents(self, principal: Principal) -> list[RegisteredAgent]:
        return [agent for agent in self._agents.values() if principal.may_use(agent.id)]

    def get_agent(self, agent_id: str, principal: Principal) -> RegisteredAgent:
        agent = self._agents.get(agent_id)
        if agent is None or not principal.may_use(agent_id):
            raise ApiError("NOT_FOUND", "no such agent is available to this principal")
        return agent

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        if self._running:
            raise RuntimeError("AgentServer is already running")
        self._running = True
        try:
            await self.store.initialize()
            yield
        finally:
            try:
                for socket in list(self._connections):
                    try:
                        await socket.close(code=1001, reason="SERVER_SHUTDOWN")
                    except Exception:
                        pass
                tasks = list(self._connections.values())
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=10)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await self.store.close()
                self._running = False

    @asynccontextmanager
    async def connection(
        self, socket: WebSocket, agent: RegisteredAgent, principal: Principal
    ) -> AsyncIterator[HarnessACPAgent]:
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        count = self._principal_connections.get(principal.id, 0)
        if len(self._connections) >= 32 or count >= 4:
            raise ApiError("SERVER_BUSY", "Too many active agent connections")
        task = asyncio.current_task()
        assert task is not None
        bundle = HostBundle(
            definition=agent.definition,
            registry=self.registry,
            store=self.store,
            close_store=False,
        )
        host = HarnessACPAgent(
            bundle,
            HostPolicy(
                principal=principal.id,
                deployment_id=agent.session_namespace,
                hosted=True,
                allowed_cwds=agent.allowed_cwds,
                default_cwd=agent.default_cwd,
                allow_local_binding_creation=False,
                resources=agent.resources,
                models=agent.models,
                default_model=agent.models[0] if agent.models else None,
                thinking_models=thinking_support(agent.models),
            ),
        )
        self._connections[socket] = task
        self._principal_connections[principal.id] = count + 1
        try:
            yield host
        finally:
            try:
                await host.shutdown()
            finally:
                self._connections.pop(socket, None)
                remaining = self._principal_connections[principal.id] - 1
                if remaining:
                    self._principal_connections[principal.id] = remaining
                else:
                    self._principal_connections.pop(principal.id, None)
