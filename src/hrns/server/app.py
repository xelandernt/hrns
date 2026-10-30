"""Register native agent definitions on an existing FastAPI application."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket

from hrns.runtime import (
    MODEL,
    AgentDefinition,
    BoundDefinition,
    DefaultSessionPolicy,
    DeploymentRecord,
    HrnsError,
    NoopLifecycleHooks,
    PluginRegistry,
    RequestContext,
    ResourceBindings,
    ResourceSet,
    SessionAccess,
    SessionLifecycleHooks,
    SessionPolicy,
    SessionStore,
)
from hrns.runtime.acp import canonical_root
from hrns.runtime.host import HrnsACPAgent
from hrns.runtime.host_support import HostBundle, HostPolicy, RevisionSpec
from hrns.runtime.local import thinking_support
from hrns.runtime.request_context import current_connection_id, current_request

from .catalog import CursorCodec
from .connections import ConnectionInfo, ConnectionRegistry
from .deployments import DeploymentConflict, DeploymentRegistry, DeploymentRevision
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
        session_policy: SessionPolicy | None = None,
        lifecycle: SessionLifecycleHooks | None = None,
        lease_ttl_ms: int = 120_000,
    ) -> None:
        if lease_ttl_ms < 1_000:
            raise ValueError("lease_ttl_ms must be at least 1000")
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
        self.session_policy: SessionPolicy = session_policy or DefaultSessionPolicy()
        self.lifecycle = lifecycle
        self.lease_ttl_ms = lease_ttl_ms
        self.deployments = DeploymentRegistry()
        self.connections = ConnectionRegistry()
        self._instance_id = uuid.uuid4().hex
        self._hosts: dict[str, HrnsACPAgent] = {}
        self._actors: dict[str, HrnsACPAgent] = {}
        self._native_deployments: set[tuple[str, str]] = set()
        self._pending_deployments: dict[tuple[str, str], DeploymentRecord] = {}
        self._persist_tasks: set[asyncio.Task[None]] = set()
        self._cursor_codec = CursorCodec()
        self._mounted = False
        self._running = False

    def add_agent(
        self,
        definition: BoundDefinition | AgentDefinition,
        *,
        resources: ResourceSet | ResourceBindings | None = None,
        models: Sequence[str] = (),
        allowed_cwds: Sequence[str | Path] | None = None,
        default_cwd: str | Path | None = None,
        accepts_client_capabilities: bool = False,
    ) -> RegisteredAgent:
        """Register an immutable deployment revision.

        A bare ``AgentDefinition`` is resolved against this server's registry and bound to
        ``resources`` here, so a capability whose requirements are missing fails at
        registration rather than on the first ``session/new``.

        Registration may happen before or after :meth:`mount`. A new ``definition_version``
        for an existing agent id becomes the revision new sessions resolve, while older
        revisions stay retained for sessions pinned to them.

        ``accepts_client_capabilities`` permits offered client-origin plugins; it is the
        default gate advertised during ACP initialization and can be overridden per session
        with :meth:`set_session_capabilities`.
        """
        if isinstance(definition, BoundDefinition):
            if resources is not None:
                raise TypeError("resources are already bound by the BoundDefinition")
            bound = definition
        elif isinstance(definition, AgentDefinition):
            bound = definition.bind(resources, registry=self.registry)
        else:
            raise TypeError("definition must be an AgentDefinition or BoundDefinition")
        agent_definition = bound.definition
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}", agent_definition.agent_id):
            raise ValueError("agent_id must be a URL segment using letters, digits, ._-")
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
        selected = bound.resources.get(MODEL) or agent_definition.model
        offered = tuple(
            dict.fromkeys([*([selected] if isinstance(selected, str) else []), *models])
        )
        version = agent_definition.definition_version or "1"
        if agent_definition.definition_version != version:
            # An immutable revision needs a stable name; an unversioned definition is its
            # first revision, not an error.
            agent_definition = agent_definition.model_copy(update={"definition_version": version})
        existing = self.deployments.get(agent_definition.agent_id, version)
        key = (agent_definition.agent_id, version)
        if existing is not None and key in self._native_deployments:
            raise DeploymentConflict(
                f"Agent {agent_definition.agent_id!r} version {version!r} is already registered"
            )
        revision = self.deployments.register(
            RegisteredAgent(
                definition=agent_definition.snapshot(),
                allowed_cwds=roots,
                default_cwd=cwd,
                models=offered,
                resources=bound.resources.values,
                accepts_client_capabilities=accepts_client_capabilities,
            ),
            replace=existing is not None,
        )
        self._native_deployments.add(key)
        record = DeploymentRecord(
            agent_id=revision.agent_id,
            version=revision.version,
            name=revision.agent.definition.name,
            description=revision.agent.definition.description,
            visible=revision.visible,
            retained=revision.retained,
            registered_at=revision.registered_at,
        )
        self._pending_deployments[key] = record
        if self._running:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Registration from a sync callback while the server runs: the next
                # async boundary flushes the pending metadata.
                loop = None
            if loop is not None:
                task = loop.create_task(self._write_deployment(record))
                self._persist_tasks.add(task)
                task.add_done_callback(self._persist_tasks.discard)
        self._refresh_actor_visibility(agent_definition.agent_id)
        self._refresh_actor_definition(agent_definition.agent_id)
        return revision.agent

    async def flush_deployments(self) -> None:
        """Persist revision metadata registered outside the event loop."""
        for record in list(self._pending_deployments.values()):
            await self._write_deployment(record)

    async def _write_deployment(self, record: DeploymentRecord) -> None:
        # A revision may have been hidden between registration and this flush; persist
        # the registry's current state, not the state captured at add_agent().
        current = self.deployments.get(record.agent_id, record.version)
        if current is not None:
            record = DeploymentRecord(
                agent_id=current.agent_id,
                version=current.version,
                name=current.agent.definition.name,
                description=current.agent.definition.description,
                visible=current.visible,
                retained=current.retained,
                registered_at=current.registered_at,
            )
        await self.store.upsert_deployment(record)
        self._pending_deployments.pop((record.agent_id, record.version), None)

    async def _restore_deployments(self) -> None:
        """Load durable revision metadata; native objects still come from the application."""
        for record in await self.store.list_deployments():
            key = (record.agent_id, record.version)
            native = self.deployments.get(record.agent_id, record.version)
            if key in self._native_deployments and native is not None:
                # The application re-registered this revision before lifespan start.
                # Re-apply the durable visibility/retention instead of resurrecting it.
                self.deployments.register(
                    native.agent,
                    visible=record.visible,
                    replace=True,
                    retained=record.retained or native.retained,
                    registered_at=record.registered_at,
                )
                continue
            placeholder = RegisteredAgent(
                definition=AgentDefinition(
                    agent_id=record.agent_id,
                    name=record.name,
                    description=record.description,
                    definition_version=record.version,
                ),
                allowed_cwds=(),
                default_cwd="",
            )
            existing = self.deployments.get(record.agent_id, record.version)
            self.deployments.register(
                placeholder,
                visible=record.visible,
                replace=existing is not None,
                retained=record.retained,
                registered_at=record.registered_at,
            )

    async def unregister_agent(self, agent_id: str) -> int:
        """Hide every revision of an agent, retaining it for pinned sessions."""
        count = self.deployments.unregister(agent_id)
        for revision in self.deployments.revisions(agent_id):
            await self.store.set_deployment_visible(agent_id, revision.version, revision.visible)
        self._refresh_actor_visibility(agent_id)
        self._refresh_actor_definition(agent_id)
        return count

    async def set_agent_visible(self, agent_id: str, version: str, visible: bool) -> None:
        """Hide or show one revision and persist the decision."""
        revision = self.deployments.set_visible(agent_id, version, visible)
        await self.store.set_deployment_visible(agent_id, version, revision.visible)
        self._refresh_actor_visibility(agent_id)
        self._refresh_actor_definition(agent_id)

    def _refresh_actor_visibility(self, agent_id: str) -> None:
        visible = self._native_revision(agent_id) is not None
        for host in self._actors.values():
            if host.definition.agent_id == agent_id:
                host.allow_new_sessions = visible

    def _refresh_actor_definition(self, agent_id: str) -> None:
        """Point live actors at the newest native visible revision for *new* sessions.

        Metadata-only placeholders never become an actor's policy. Already-open
        sessions keep their pinned revision through the record's
        ``definition_version`` and the host's revision resolver.
        """
        revision = self._native_revision(agent_id)
        if revision is None:
            return
        for host in self._actors.values():
            if host.definition.agent_id != agent_id:
                continue
            agent = revision.agent
            host.definition = agent.definition
            host.bundle.definition = agent.definition
            host.sessions.definition = agent.definition
            host.policy = dataclasses.replace(
                host.policy,
                models=agent.models,
                default_model=agent.models[0] if agent.models else None,
                thinking_models=thinking_support(agent.models),
                resources=agent.resources,
                allowed_cwds=agent.allowed_cwds,
                default_cwd=agent.default_cwd,
                accepts_client_capabilities=agent.accepts_client_capabilities,
            )
            host.sessions.policy = host.policy
            host._default_model = host.policy.default_model
            host.allow_new_sessions = revision.visible
            host.config.policy = host.policy
            host._models = agent.models
            host._name = agent.definition.name or agent.id
            host._version = agent.definition.version or ""

    def retained_revisions(self, agent_id: str) -> tuple[DeploymentRevision, ...]:
        """Every persisted revision of an agent, including hidden ones.

        After a restart only metadata is durable; the application re-registers the
        native definition for each revision it wants to keep loadable.
        """
        return self.deployments.revisions(agent_id)

    def mount(self, app: FastAPI, *, prefix: str = "") -> None:
        """Add catalog/ACP routes and storage lifespan to an existing app."""
        from .api import build_router

        if self._mounted:
            raise RuntimeError("An AgentServer can only be mounted once")
        app.include_router(build_router(self, prefix=prefix), prefix=prefix)
        self._mounted = True

    def _native_revision(
        self, agent_id: str, *, include_hidden: bool = False
    ) -> DeploymentRevision | None:
        """Newest revision of an agent that has a native object in this process."""
        for revision in reversed(self.deployments.revisions(agent_id)):
            if not include_hidden and not revision.visible:
                continue
            if (revision.agent_id, revision.version) in self._native_deployments:
                return revision
        return None

    def available_agents(self, principal: Principal) -> list[RegisteredAgent]:
        agents: list[RegisteredAgent] = []
        for agent_id in self.deployments.agent_ids():
            if not principal.may_use(agent_id):
                continue
            revision = self._native_revision(agent_id)
            if revision is not None:
                agents.append(revision.agent)
        return agents

    def get_agent(
        self, agent_id: str, principal: Principal, *, allow_hidden: bool = False
    ) -> RegisteredAgent:
        if not principal.may_use(agent_id):
            raise ApiError("NOT_FOUND", "no such agent is available to this principal")
        revision = self._native_revision(agent_id)
        if revision is None and allow_hidden:
            revision = self._native_revision(agent_id, include_hidden=True)
        if revision is None:
            message = (
                "this deployment revision is retained but not registered in this process"
                if self.deployments.revisions(agent_id)
                else "no such agent is available to this principal"
            )
            raise ApiError("NOT_FOUND", message, details={"agentId": agent_id})
        return revision.agent

    def get_revision(
        self, agent_id: str, principal: Principal, *, version: str | None = None
    ) -> DeploymentRevision:
        """Resolve a visible or retained revision for a principal."""
        if not principal.may_use(agent_id):
            raise ApiError("NOT_FOUND", "no such agent is available to this principal")
        revision = (
            self.deployments.get(agent_id, version)
            if version is not None
            else self._native_revision(agent_id, include_hidden=True)
        )
        if (
            revision is None
            or (revision.agent_id, revision.version) not in self._native_deployments
        ):
            raise ApiError(
                "NOT_FOUND",
                "the deployment revision for this session is not registered in this process",
                details={
                    "agentId": agent_id,
                    "version": version,
                    "retained": revision is not None,
                },
            )
        return revision

    async def set_session_access(
        self,
        principal: Principal,
        agent_id: str,
        session_id: str,
        *,
        access: SessionAccess,
        collaborators: frozenset[str] | None = None,
    ) -> bool:
        """Change a session's access mode; the caller must own the session.

        ``collaborators`` is only meaningful for ``shared`` and ``archived`` modes.
        Archived sessions stay loadable and replayable but reject every mutation.
        """
        agent = self.get_agent(agent_id, principal, allow_hidden=True)
        host = self._host_for_deployment(agent.session_namespace)
        temporary = await self._admin_lease(principal, agent.session_namespace, session_id)
        try:
            if host is not None:
                return await host.sessions.set_access(
                    session_id,
                    access=access,
                    collaborators=collaborators,
                    principal=principal.id,
                )
            return await self.store.set_access(
                principal.id,
                agent.session_namespace,
                session_id,
                access=access,
                collaborators=collaborators,
            )
        finally:
            if temporary is not None:
                await self.store.release_lease(
                    principal.id, agent.session_namespace, session_id, temporary
                )

    async def set_session_capabilities(
        self, principal: Principal, agent_id: str, session_id: str, accepts: bool | None
    ) -> bool:
        """Override the deployment's client-origin capability gate for one session."""
        agent = self.get_agent(agent_id, principal, allow_hidden=True)
        host = self._host_for_deployment(agent.session_namespace)
        temporary = await self._admin_lease(principal, agent.session_namespace, session_id)
        try:
            if host is not None:
                return await host.sessions.set_client_capabilities(
                    session_id, accepts, principal=principal.id
                )
            return await self.store.set_client_capabilities(
                principal.id, agent.session_namespace, session_id, accepts
            )
        finally:
            if temporary is not None:
                await self.store.release_lease(
                    principal.id, agent.session_namespace, session_id, temporary
                )

    def _host_for_deployment(self, deployment_id: str) -> HrnsACPAgent | None:
        """A live actor for this logical agent, regardless of which revision opened it."""
        return next(
            (host for host in self._hosts.values() if host.policy.deployment_id == deployment_id),
            None,
        )

    async def _admin_lease(
        self, principal: Principal, deployment_id: str, session_id: str
    ) -> str | None:
        """Guard an embedding-API write with the session lease.

        A live actor reuses its own lease. With no actor in this process a short-lived
        administrative lease is taken; a live other worker wins and the write is
        refused.
        """
        if await self.store.get(principal.id, deployment_id, session_id) is None:
            # Do not create a coordination row for a session that does not exist.
            return None
        host = next(
            (
                candidate
                for candidate in self._hosts.values()
                if candidate.policy.deployment_id == deployment_id
            ),
            None,
        )
        if host is not None:
            try:
                await host.acquire_session_lease(session_id)
            except HrnsError as exc:
                raise ApiError("CONFLICT", exc.message) from exc
            return None
        owner = f"{self._instance_id}:admin:{uuid.uuid4().hex}"
        lease = await self.store.acquire_lease(
            principal.id, deployment_id, session_id, owner, self.lease_ttl_ms
        )
        if lease is None:
            raise ApiError(
                "CONFLICT",
                "The session is owned by another live worker.",
                details={"sessionId": session_id},
            )
        return owner

    async def revoke_credential(self, credential_id: str) -> int:
        """Close every live connection using ``credential_id`` before returning."""
        return await self.connections.revoke_credential(credential_id)

    async def revoke_principal(self, principal: str) -> int:
        """Close every live connection of ``principal`` across all its credentials."""
        return await self.connections.revoke_principal(principal)

    def list_connections(
        self, *, principal: str | None = None, credential_id: str | None = None
    ) -> tuple[ConnectionInfo, ...]:
        """Enumerate live connections for attribution or revocation."""
        return self.connections.list_connections(principal=principal, credential_id=credential_id)

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncGenerator[None, None]:
        if self._running:
            raise RuntimeError("AgentServer is already running")
        self._running = True
        try:
            await self.store.initialize()
            await self._restore_deployments()
            await self.flush_deployments()
            yield
        finally:
            try:
                await self.connections.close_all()
                for namespace, host in list(self._actors.items()):
                    if self._actors.get(namespace) is host:
                        self._actors.pop(namespace, None)
                    await host.shutdown()
                self._hosts.clear()
                await self.flush_deployments()
                if self._persist_tasks:
                    await asyncio.gather(*tuple(self._persist_tasks), return_exceptions=True)
            finally:
                await self.store.close()
                self._running = False

    def _build_host(self, agent: RegisteredAgent, principal: Principal) -> HrnsACPAgent:
        version = agent.definition.definition_version or ""

        def resolve_revision(definition_version: str) -> RevisionSpec | None:
            key = (agent.id, definition_version)
            if key not in self._native_deployments:
                return None
            revision = self.deployments.get(agent.id, definition_version)
            if revision is None:
                return None
            pinned = revision.agent
            return RevisionSpec(
                definition=pinned.definition,
                allowed_cwds=pinned.allowed_cwds,
                default_cwd=pinned.default_cwd,
                models=pinned.models,
                thinking_models=thinking_support(pinned.models),
                resources=pinned.resources,
                accepts_client_capabilities=pinned.accepts_client_capabilities,
            )

        revision = self.deployments.get(agent.id, version)
        bundle = HostBundle(
            definition=agent.definition,
            registry=self.registry,
            store=self.store,
            close_store=False,
            revision_resolver=resolve_revision,
        )
        return HrnsACPAgent(
            bundle,
            HostPolicy(
                principal=principal.id,
                deployment_id=agent.session_namespace,
                hosted=True,
                accepts_client_capabilities=agent.accepts_client_capabilities,
                allowed_cwds=agent.allowed_cwds,
                default_cwd=agent.default_cwd,
                allow_local_binding_creation=False,
                resources=agent.resources,
                models=agent.models,
                default_model=agent.models[0] if agent.models else None,
                thinking_models=thinking_support(agent.models),
                session_policy=self.session_policy,
                allow_new_sessions=revision.visible if revision is not None else True,
                lifecycle=self.lifecycle or NoopLifecycleHooks(),
                worker_id=f"{self._instance_id}:{uuid.uuid4().hex}",
                lease_ttl_ms=self.lease_ttl_ms,
            ),
        )

    @asynccontextmanager
    async def connection(
        self, socket: WebSocket, agent: RegisteredAgent, principal: Principal
    ) -> AsyncGenerator[HrnsACPAgent, None]:
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        if self._pending_deployments:
            await self.flush_deployments()
        if len(self.connections.list_connections()) >= 32 or (
            self.connections.count_for_principal(principal.id) >= 4
        ):
            raise ApiError("SERVER_BUSY", "Too many active agent connections")
        task = asyncio.current_task()
        if task is None:
            raise ApiError("SERVER_BUSY", "No ASGI handler task for this connection")
        try:
            info = await self.connections.register(
                socket,
                task,
                principal=principal.id,
                credential_id=principal.credential_id,
                agent_id=agent.id,
            )
        except PermissionError as exc:
            raise ApiError("UNAUTHORIZED", str(exc)) from exc
        try:
            host = self._actors.get(agent.session_namespace)
            if host is None:
                host = self._build_host(agent, principal)
                self._actors[agent.session_namespace] = host
        except BaseException:
            # A constructor failure must not keep counting against the connection limit.
            self.connections.unregister(info.connection_id)
            raise
        request = RequestContext(
            principal=principal.id,
            credential_id=principal.credential_id,
            connection_id=info.connection_id,
            agent_id=agent.id,
        )
        request_token = current_request.set(request)
        connection_token = current_connection_id.set(info.connection_id)
        host.request_context = request
        self._hosts[info.connection_id] = host
        try:
            yield host
        finally:
            current_request.reset(request_token)
            current_connection_id.reset(connection_token)
            host.detach(info.connection_id)
            self._hosts.pop(info.connection_id, None)
            self.connections.unregister(info.connection_id)
            # Another connection may have registered this actor but not yet attached
            # its socket. Keep the actor alive until every handler leaves `_hosts`.
            still_attached = any(candidate is host for candidate in self._hosts.values())
            if not host.fanout.connections() and not still_attached:
                if self._actors.get(agent.session_namespace) is host:
                    self._actors.pop(agent.session_namespace, None)
                await host.shutdown()
