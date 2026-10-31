"""Register native agent definitions on an existing FastAPI application."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import re
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from functools import wraps
from pathlib import Path
from typing import Concatenate, ParamSpec, TypeVar

from acp import schema
from fastapi import FastAPI, WebSocket
from pydantic import ValidationError

from hrns.protocol import BindingChange, canonical_hash
from hrns.runtime import (
    MODEL,
    AgentDefinition,
    BoundDefinition,
    DefaultSessionPolicy,
    DeploymentRecord,
    HrnsError,
    NoopLifecycleHooks,
    OperationKind,
    PluginRegistry,
    PolicyAction,
    PromptState,
    QueuedPrompt,
    RequestContext,
    ResourceBindings,
    ResourceSet,
    ResourceSlot,
    SessionAccess,
    SessionContext,
    SessionLease,
    SessionLifecycleHooks,
    SessionPolicy,
    SessionStore,
    decide_policy,
)
from hrns.runtime.acp import canonical_root
from hrns.runtime.acp_store import (
    SessionRecord,
    TurnReceipt,
    TurnRequest,
    now_ms,
)
from hrns.runtime.creation import (
    InitialSessionPolicy,
    SessionCreateRequest,
    SessionCreationOperation,
    SessionCreationResult,
    SessionCreationState,
    SessionNewInput,
)
from hrns.runtime.creation_service import creation_lease, recover_creation
from hrns.runtime.host import HrnsACPAgent
from hrns.runtime.host_support import HostBundle, HostPolicy, RevisionSpec
from hrns.runtime.local import thinking_support
from hrns.runtime.request_context import current_connection_id, current_request
from hrns.runtime.resources import SLOTS

from .catalog import CursorCodec
from .connections import ConnectionInfo, ConnectionRegistry
from .deployments import DeploymentConflict, DeploymentRegistry, DeploymentRevision
from .errors import ApiError
from .models import (
    AuthenticationDependency,
    InitialSessionPolicyFactory,
    Principal,
    RegisteredAgent,
    SessionLifecycleResult,
)
from .resources import SessionResourceResolver


async def _local_principal() -> Principal:
    return Principal("local")


_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")


@dataclasses.dataclass(frozen=True, slots=True)
class SessionCapabilitySettings:
    """Durable override, effective pinned deployment setting, and CAS revision."""

    override: bool | None
    effective: bool
    revision: int


def _admitted(
    method: Callable[Concatenate[AgentServer, _Params], Awaitable[_Result]],
) -> Callable[Concatenate[AgentServer, _Params], Awaitable[_Result]]:
    """Keep embedding calls visible to shutdown from admission through finalization."""

    @wraps(method)
    async def wrapped(self: AgentServer, *args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        task = self._admit()
        try:
            return await method(self, *args, **kwargs)
        finally:
            self._leave(task)

    return wrapped


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
        initial_session_policy: InitialSessionPolicy | InitialSessionPolicyFactory | None = None,
        resource_resolver: SessionResourceResolver | None = None,
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
        self.initial_session_policy = initial_session_policy or InitialSessionPolicy()
        self.resource_resolver = resource_resolver
        self.lease_ttl_ms = lease_ttl_ms
        self.deployments = DeploymentRegistry()
        self.connections = ConnectionRegistry()
        self._instance_id = uuid.uuid4().hex
        self._hosts: dict[str, HrnsACPAgent] = {}
        self._actors: dict[str, HrnsACPAgent] = {}
        self._headless_actors: set[str] = set()
        self._actor_locks: dict[str, asyncio.Lock] = {}
        self._retirements: dict[str, asyncio.Task[None]] = {}
        self._native_deployments: set[tuple[str, str]] = set()
        self._pending_deployments: dict[tuple[str, str], DeploymentRecord] = {}
        self._persist_tasks: set[asyncio.Task[None]] = set()
        self._creation_tasks: set[asyncio.Task[SessionCreationResult]] = set()
        self._admitted_tasks: dict[asyncio.Task[object], int] = {}
        self._connection_tasks: set[asyncio.Task[object]] = set()
        self._cursor_codec = CursorCodec()
        self._mounted = False
        self._running = False
        self._starting = False
        self._stopping = False

    def _admit(self) -> asyncio.Task[object]:
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        task = asyncio.current_task()
        if task is None:
            raise ApiError("SERVER_BUSY", "No task for this server operation")
        self._admitted_tasks[task] = self._admitted_tasks.get(task, 0) + 1
        return task

    def _leave(self, task: asyncio.Task[object]) -> None:
        count = self._admitted_tasks[task] - 1
        if count:
            self._admitted_tasks[task] = count
        else:
            del self._admitted_tasks[task]

    @asynccontextmanager
    async def _actor_scope(self, namespace: str) -> AsyncGenerator[None, None]:
        """Serialize actor selection without waiting for retirement under the actor lock."""
        lock = self._actor_locks.setdefault(namespace, asyncio.Lock())
        while True:
            await lock.acquire()
            retirement = self._retirements.get(namespace)
            if retirement is None:
                break
            lock.release()
            await asyncio.shield(retirement)
            if self._retirements.get(namespace) is retirement:
                self._retirements.pop(namespace, None)
        try:
            yield
        finally:
            lock.release()

    def _retire_actor(self, namespace: str, host: HrnsACPAgent) -> asyncio.Task[None]:
        """Called under the actor lock; the returned task must be awaited outside it."""
        if self._actors.get(namespace) is host:
            self._actors.pop(namespace, None)
        retirement = asyncio.create_task(host.shutdown())
        self._retirements[namespace] = retirement
        return retirement

    def add_agent(
        self,
        definition: BoundDefinition | AgentDefinition,
        *,
        resources: ResourceSet | ResourceBindings | None = None,
        session_resource_slots: Sequence[ResourceSlot[object]] = (),
        models: Sequence[str] = (),
        allowed_cwds: Sequence[str | Path] | None = None,
        default_cwd: str | Path | None = None,
        accepts_client_capabilities: bool = False,
    ) -> RegisteredAgent:
        """Register an immutable deployment revision.

        Required slots must be bound at registration unless explicitly listed in
        ``session_resource_slots`` for the server's resource resolver. Every required
        slot is validated again during session composition.

        Registration may happen before or after :meth:`mount`. A new ``definition_version``
        for an existing agent id becomes the revision new sessions resolve, while older
        revisions stay retained for sessions pinned to them.

        ``accepts_client_capabilities`` permits offered client-origin plugins; it is the
        default gate advertised during ACP initialization and can be overridden per session
        with :meth:`set_session_capabilities`.
        """
        if session_resource_slots and self.resource_resolver is None:
            raise ValueError("session_resource_slots requires a resource_resolver")
        if isinstance(definition, BoundDefinition):
            if resources is not None:
                raise TypeError("resources are already bound by the BoundDefinition")
            agent_definition = definition.definition
            bound_resources = definition.resources
        elif isinstance(definition, AgentDefinition):
            agent_definition = definition.resolve(self.registry)
            bound_resources = ResourceSet(resources)
        else:
            raise TypeError("definition must be an AgentDefinition or BoundDefinition")
        for binding in agent_definition.bindings:
            descriptor = binding.plugin
            if descriptor is None:
                raise HrnsError("INVALID_CONFIGURATION", "An agent binding is unresolved.")
            if descriptor.config_model is not None and binding.config:
                try:
                    descriptor.config_model.model_validate(binding.config)
                except ValidationError as exc:
                    raise HrnsError(
                        "INVALID_CONFIGURATION",
                        f"Configuration for binding {binding.binding_id!r} is invalid.",
                    ) from exc
        for slot in session_resource_slots:
            if not isinstance(slot, ResourceSlot):
                raise TypeError("session_resource_slots must contain ResourceSlot objects")
            if not agent_definition.declares(slot):
                raise ValueError(f"Session resource slot {slot.name!r} is not declared")
            SLOTS.register(slot)
        deferred = {slot.name for slot in session_resource_slots}
        requirements = [
            requirement.model_copy(update={"required": False})
            if requirement.slot.name in deferred
            else requirement
            for requirement in agent_definition.resources
        ]
        # Deferred bindings may still be supplied statically and must be type-checked.
        for slot in session_resource_slots:
            for name in (slot.name, *slot.aliases):
                if name in bound_resources.values:
                    slot.validate_value(bound_resources.values[name])
        bound_resources.validate_requirements(
            requirements, context=f"Definition {agent_definition.agent_id!r}"
        )
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
        selected = bound_resources.get(MODEL) or agent_definition.model
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
                resources=bound_resources.values,
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

    @_admitted
    async def flush_deployments(self) -> None:
        """Persist revision metadata registered outside the event loop."""
        await self._flush_deployments()

    async def _flush_deployments(self) -> None:
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

    @_admitted
    async def unregister_agent(self, agent_id: str) -> int:
        """Hide every revision of an agent, retaining it for pinned sessions."""
        count = self.deployments.unregister(agent_id)
        for revision in self.deployments.revisions(agent_id):
            await self.store.set_deployment_visible(agent_id, revision.version, revision.visible)
        self._refresh_actor_visibility(agent_id)
        self._refresh_actor_definition(agent_id)
        return count

    @_admitted
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

    @_admitted
    async def create_session(
        self,
        principal: Principal,
        agent_id: str,
        *,
        definition_version: str,
        mutation_id: str,
        policy: InitialSessionPolicy | None = None,
        cwd: str | None = None,
        initial_configuration: Sequence[BindingChange] = (),
        effort: str | None = None,
        client_capabilities: schema.ClientCapabilities | None = None,
    ) -> SessionCreationResult:
        """Create a fully durable, authenticated session pinned to an exact revision.

        ``principal`` must come from the embedding application's authentication.
        ``policy`` is trusted server input, not REST/ACP client configuration. A
        mutation ID identifies one product intent across credentials and deployments.
        Unknown responses can be retried with identical input; conflicting reuse fails.
        """
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        if not principal.may_use(agent_id) or (
            principal.scopes is not None and "session:create" not in principal.scopes
        ):
            raise ApiError("NOT_FOUND", "No deployment is available to this principal.")
        if not isinstance(mutation_id, str) or not mutation_id.strip() or len(mutation_id) > 200:
            raise ApiError(
                "INVALID_REQUEST",
                "mutation_id must be a non-empty string of at most 200 characters.",
            )
        if not isinstance(definition_version, str) or not definition_version.strip():
            raise ApiError("INVALID_REQUEST", "definition_version must be a non-empty string.")
        task = asyncio.create_task(
            self._create_session(
                principal,
                agent_id,
                definition_version=definition_version,
                mutation_id=mutation_id,
                policy=(policy or InitialSessionPolicy()).model_copy(deep=True),
                cwd=cwd,
                initial_configuration=[
                    change.model_copy(deep=True) for change in initial_configuration
                ],
                effort=effort,
                client_capabilities=client_capabilities.model_copy(deep=True)
                if client_capabilities
                else None,
            )
        )
        self._creation_tasks.add(task)

        def completed(done: asyncio.Task[SessionCreationResult]) -> None:
            self._creation_tasks.discard(done)
            # Retrieve exceptions even when the transport lost the response.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        try:
            return await asyncio.shield(task)
        except HrnsError as exc:
            raise self._embedding_creation_error(exc) from exc
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                "SESSION_DURABILITY_FAILED",
                "The session creation outcome could not be confirmed; retry with the same mutation ID.",
                status_code=503,
            ) from exc

    async def _create_session(
        self,
        principal: Principal,
        agent_id: str,
        *,
        definition_version: str,
        mutation_id: str,
        policy: InitialSessionPolicy,
        cwd: str | None,
        initial_configuration: Sequence[BindingChange],
        effort: str | None,
        client_capabilities: schema.ClientCapabilities | None,
    ) -> SessionCreationResult:
        existing = await self.store.get_creation(principal.id, mutation_id)
        revision = self.deployments.get(agent_id, definition_version)
        # Completed retries remain usable after hiding their original revision. New
        # intents require an exact visible native revision, with a uniform absence error.
        if existing is None and (
            revision is None
            or not revision.visible
            or (agent_id, definition_version) not in self._native_deployments
        ):
            raise ApiError("NOT_FOUND", "No deployment is available to this principal.")
        default_cwd = (
            existing.request.cwd
            if existing is not None and cwd is None
            else revision.agent.default_cwd
            if revision is not None
            else ""
        )
        try:
            request = SessionCreateRequest(
                agent_id=agent_id,
                definition_version=definition_version,
                cwd=canonical_root(cwd if cwd is not None else default_cwd),
                policy=policy,
                initial_configuration=list(initial_configuration),
                effort=effort,
                client_capabilities=client_capabilities
                if policy.accepts_client_capabilities
                else None,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise ApiError("INVALID_REQUEST", "Invalid session creation input.") from exc
        payload = request.model_dump(mode="json")
        payload["policy"]["collaborators"] = sorted(policy.collaborators)
        timestamp = now_ms()
        attribution = current_request.get()
        candidate = SessionCreationOperation(
            operation_id=uuid.uuid4().hex,
            session_id=uuid.uuid4().hex,
            mutation_id=mutation_id,
            principal=principal.id,
            credential_id=principal.credential_id,
            connection_id=attribution.connection_id if attribution is not None else None,
            scopes=principal.scopes,
            deployment_id=revision.agent.session_namespace
            if revision is not None
            else existing.deployment_id
            if existing is not None
            else "",
            request=request,
            request_hash=canonical_hash(payload),
            created_at=timestamp,
            updated_at=timestamp,
        )
        await self._authorize_creation(existing or candidate, principal)
        operation = await self.store.reserve_creation(candidate)
        if operation.operation_id != (existing or candidate).operation_id:
            await self._authorize_creation(operation, principal)
        if operation.result is not None and operation.state is SessionCreationState.SUCCEEDED:
            return operation.result
        if operation.state is SessionCreationState.FAILED:
            raise HrnsError(
                operation.error_code or "SESSION_CREATION_FAILED",
                operation.error_message or "Session creation failed.",
            )
        if revision is None or (agent_id, definition_version) not in self._native_deployments:
            raise ApiError("NOT_FOUND", "No deployment is available to this principal.")
        namespace = revision.agent.session_namespace
        async with self._actor_scope(namespace):
            # Re-read after waiting for this process's actor; another attempt may have
            # committed. Cross-process claims are arbitrated by the database fence.
            current = await self.store.get_creation(principal.id, mutation_id)
            if current is not None and current.result is not None:
                return current.result
            if current is not None and current.state is SessionCreationState.FAILED:
                raise HrnsError(
                    current.error_code or "SESSION_CREATION_FAILED",
                    current.error_message or "Session creation failed.",
                )
            claimed = await self.store.claim_creation(
                principal.id,
                mutation_id,
                f"{self._instance_id}:create:{uuid.uuid4().hex}",
                self.lease_ttl_ms,
            )
            if claimed is None:
                current = await self.store.get_creation(principal.id, mutation_id)
                if current is not None and current.result is not None:
                    return current.result
                raise HrnsError(
                    "SESSION_CREATION_IN_PROGRESS",
                    "The session creation intent is pending; retry with the same mutation ID.",
                )
            async with creation_lease(self.store, claimed, self.lease_ttl_ms) as lease:
                if claimed.state is not SessionCreationState.RESERVED:
                    await recover_creation(lease, self.lifecycle or NoopLifecycleHooks())
                    raise HrnsError(
                        "SESSION_CREATION_IN_PROGRESS",
                        "The previous attempt was reconciled; retry with the same mutation ID.",
                    )
                host = self._actors.get(namespace)
                temporary = host is None
                if host is None:
                    host = self._build_host(revision.agent, principal)
                context = RequestContext(
                    principal=principal.id,
                    credential_id=principal.credential_id,
                    connection_id=claimed.connection_id,
                    request_id=claimed.operation_id,
                    agent_id=agent_id,
                    scopes=principal.scopes,
                )
                token = current_request.set(context)
                try:
                    result = await host.create_authoritative(lease)
                    if temporary:
                        self._actors[namespace] = host
                    return result
                except BaseException as exc:
                    if (
                        lease.operation.state is SessionCreationState.RESERVED
                        and lease.operation.expires_at
                    ):
                        if isinstance(exc, asyncio.CancelledError):
                            await lease.transition(SessionCreationState.RESERVED, release=True)
                        else:
                            await lease.transition(
                                SessionCreationState.FAILED,
                                error_code=exc.code
                                if isinstance(exc, HrnsError)
                                else "SESSION_COMPOSITION_FAILED",
                                error_message=exc.message
                                if isinstance(exc, HrnsError)
                                else "Session creation failed.",
                                release=True,
                            )
                    if temporary:
                        self._retire_actor(namespace, host)
                    if isinstance(exc, (HrnsError, ApiError, asyncio.CancelledError)):
                        raise
                    raise HrnsError(
                        "SESSION_COMPOSITION_FAILED", "Session composition failed."
                    ) from exc
                finally:
                    current_request.reset(token)

    @_admitted
    async def get_session_creation(
        self, principal: Principal, mutation_id: str
    ) -> SessionCreationOperation:
        """Read durable intent status without constructing a session or actor."""
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        operation = await self.store.get_creation(principal.id, mutation_id)
        if (
            operation is None
            or not principal.may_use(operation.request.agent_id)
            or (principal.scopes is not None and "session:create" not in principal.scopes)
        ):
            raise ApiError(
                "NOT_FOUND", "No session creation operation is available to this principal."
            )
        try:
            await self._authorize_creation(operation, principal)
        except HrnsError as exc:
            raise self._embedding_creation_error(exc) from exc
        return operation

    async def _authorize_creation(
        self, operation: SessionCreationOperation, principal: Principal
    ) -> None:
        policy = operation.request.policy
        request_context = current_request.get()
        context = SessionContext(
            session_id=operation.session_id,
            principal=principal.id,
            deployment_id=operation.deployment_id,
            agent_id=operation.request.agent_id,
            owner=operation.principal,
            access=policy.access,
            collaborators=policy.collaborators,
            credential_id=principal.credential_id,
            connection_id=request_context.connection_id if request_context is not None else None,
            request_id=operation.operation_id,
            metadata={
                **policy.metadata,
                "clientCapabilities": "enabled"
                if policy.accepts_client_capabilities
                else "disabled",
            },
        )
        try:
            decision = await decide_policy(self.session_policy, context, PolicyAction.CREATE)
        except Exception as exc:
            raise HrnsError(
                "POLICY_EVALUATION_FAILED", "The session creation policy could not be evaluated."
            ) from exc
        if not decision.allowed:
            raise HrnsError(
                "SESSION_CREATION_DENIED", "No deployment is available to this principal."
            )

    @_admitted
    async def reconcile_session_creations(self) -> tuple[SessionCreationOperation, ...]:
        """Recover expired creation attempts without publishing provisional sessions.

        Live claims in other workers are left alone. Call again after their expiry;
        cleanup hook failures remain pending and fail closed.
        """
        return await self._reconcile_session_creations()

    async def _reconcile_session_creations(self) -> tuple[SessionCreationOperation, ...]:
        results: list[SessionCreationOperation] = []
        for operation in await self.store.list_pending_creations():
            claimed = await self.store.claim_creation(
                operation.principal,
                operation.mutation_id,
                f"{self._instance_id}:reconcile:{uuid.uuid4().hex}",
                self.lease_ttl_ms,
            )
            if claimed is None:
                continue
            try:
                async with creation_lease(self.store, claimed, self.lease_ttl_ms) as lease:
                    await recover_creation(lease, self.lifecycle or NoopLifecycleHooks())
                    results.append(lease.operation)
            except HrnsError as exc:
                raise self._embedding_creation_error(exc) from exc
        return tuple(results)

    @staticmethod
    def _embedding_creation_error(error: HrnsError) -> ApiError:
        if error.code in {
            "SESSION_NOT_FOUND",
            "DEPLOYMENT_UNAVAILABLE",
            "FORBIDDEN",
            "SESSION_CREATION_DENIED",
        }:
            return ApiError("NOT_FOUND", "No deployment is available to this principal.")
        statuses = {
            "SESSION_CREATION_IDEMPOTENCY_CONFLICT": 409,
            "SESSION_CREATION_IN_PROGRESS": 409,
            "SESSION_CREATION_CONFLICT": 409,
            "POLICY_DENIED": 403,
            "SESSION_READ_ONLY": 403,
            "INVALID_CONFIGURATION": 400,
            "CAPABILITY_UNAVAILABLE": 400,
            "LIFECYCLE_FAILED": 500,
            "SESSION_COMPOSITION_FAILED": 500,
            "SESSION_DURABILITY_FAILED": 503,
        }
        return ApiError(error.code, error.message, status_code=statuses.get(error.code, 500))

    @_admitted
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
        async with self._actor_scope(agent.session_namespace):
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
                    await self._release_admin_lease(temporary)

    @_admitted
    async def get_session_capabilities(
        self, principal: Principal, agent_id: str, session_id: str
    ) -> SessionCapabilitySettings:
        """Read owner-scoped durable capability settings under the current LOAD policy."""
        agent = self.get_agent(agent_id, principal, allow_hidden=True)
        request = current_request.get()
        missing = ApiError("NOT_FOUND", "No session is available to this principal.")
        if request is not None and (
            request.principal != principal.id or request.credential_id != principal.credential_id
        ):
            raise missing
        record = await self.store.get_visible(agent.session_namespace, session_id, principal.id)
        if record is None or record.effective_owner != principal.id:
            raise missing
        context = self._capability_policy_context(principal, agent, record)
        if not (await decide_policy(self.session_policy, context, PolicyAction.LOAD)).allowed:
            raise missing
        return self._capability_settings(principal, agent_id, record)

    def _capability_settings(
        self, principal: Principal, agent_id: str, record: SessionRecord
    ) -> SessionCapabilitySettings:
        pinned = self.get_revision(agent_id, principal, version=record.definition_version)
        override = record.accepts_client_capabilities
        return SessionCapabilitySettings(
            override=override,
            effective=pinned.agent.accepts_client_capabilities if override is None else override,
            revision=record.capability_revision,
        )

    @_admitted
    async def set_session_capabilities(
        self,
        principal: Principal,
        agent_id: str,
        session_id: str,
        accepts: bool | None,
        *,
        expected_revision: int,
    ) -> SessionCapabilitySettings:
        """Atomically change the capability override at the expected revision."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ApiError("INVALID_REQUEST", "expected_revision must be a non-negative integer.")
        if accepts is not None and not isinstance(accepts, bool):
            raise ApiError("INVALID_REQUEST", "accepts must be a boolean or None.")
        agent = self.get_agent(agent_id, principal, allow_hidden=True)
        request = current_request.get()
        if request is not None and (
            request.principal != principal.id or request.credential_id != principal.credential_id
        ):
            raise ApiError("NOT_FOUND", "No session is available to this principal.")
        if (principal.scopes is not None and "queue:mutate" not in principal.scopes) or (
            request is not None
            and request.scopes is not None
            and "queue:mutate" not in request.scopes
        ):
            raise ApiError(
                "INSUFFICIENT_SCOPE",
                "The credential does not grant queue mutation scope.",
                details={"requiredScope": "queue:mutate"},
                status_code=403,
            )
        async with self._actor_scope(agent.session_namespace):
            host = self._host_for_deployment(agent.session_namespace)
            await self._authorize_capability_change(principal, agent, session_id, host)
            temporary = await self._admin_lease(principal, agent.session_namespace, session_id)
            try:
                await self._authorize_capability_change(principal, agent, session_id, host)
                if host is not None:
                    record = await host.sessions.set_client_capabilities(
                        session_id,
                        accepts,
                        principal=principal.id,
                        expected_revision=expected_revision,
                    )
                else:
                    if temporary is None:
                        raise ApiError("CONFLICT", "No administrative session lease is available.")
                    record = await self.store.set_client_capabilities(
                        principal.id,
                        agent.session_namespace,
                        session_id,
                        accepts,
                        expected_revision=expected_revision,
                        owner_id=temporary.owner_id,
                        fence=temporary.fence,
                    )
                return self._capability_settings(principal, agent_id, record)
            except HrnsError as exc:
                if exc.code == "SESSION_NOT_FOUND":
                    raise ApiError(
                        "NOT_FOUND", "No session is available to this principal."
                    ) from exc
                if exc.code in {"REVISION_CONFLICT", "SESSION_CONFLICT", "SESSION_HELD"}:
                    raise ApiError(
                        exc.code, exc.message, details=exc.details, status_code=409
                    ) from exc
                raise
            finally:
                if temporary is not None:
                    await self._release_admin_lease(temporary)

    async def _authorize_capability_change(
        self,
        principal: Principal,
        agent: RegisteredAgent,
        session_id: str,
        host: HrnsACPAgent | None,
    ) -> None:
        record = host.sessions.store.staged(session_id) if host is not None else None
        if record is None:
            record = await self.store.get_visible(agent.session_namespace, session_id, principal.id)
        if record is None or record.effective_owner != principal.id:
            raise ApiError("NOT_FOUND", "No session is available to this principal.")
        if record.read_only:
            raise ApiError(
                "SESSION_READ_ONLY",
                "This session is archived and cannot be modified.",
                status_code=403,
            )
        context = self._capability_policy_context(principal, agent, record)
        decision = await decide_policy(self.session_policy, context, PolicyAction.CONFIGURE)
        if not decision.allowed:
            if decision.code == "SESSION_NOT_FOUND":
                raise ApiError("NOT_FOUND", "No session is available to this principal.")
            raise ApiError(decision.code, decision.message, status_code=403)

    @staticmethod
    def _capability_policy_context(
        principal: Principal, agent: RegisteredAgent, record: SessionRecord
    ) -> SessionContext:
        request = current_request.get()
        if request is not None and (
            request.principal != principal.id or request.credential_id != principal.credential_id
        ):
            request = None
        return SessionContext(
            session_id=record.session_id,
            principal=principal.id,
            deployment_id=agent.session_namespace,
            agent_id=agent.id,
            owner=record.effective_owner,
            access=record.access,
            collaborators=record.collaborators,
            credential_id=principal.credential_id,
            connection_id=request.connection_id if request is not None else None,
            request_id=request.request_id if request is not None else None,
            metadata={
                "clientCapabilities": "deployment"
                if record.accepts_client_capabilities is None
                else "enabled"
                if record.accepts_client_capabilities
                else "disabled"
            },
        )

    async def _authorized_session_candidate(
        self,
        principal: Principal,
        agent: RegisteredAgent,
        session_id: str,
        *,
        action: PolicyAction = PolicyAction.LOAD,
    ) -> SessionRecord:
        """Authorize an exact internal candidate before using its metadata."""
        missing = ApiError("NOT_FOUND", "No session is available to this principal.")
        record = await self.store.get_policy_candidate(agent.session_namespace, session_id)
        if record is None or record.agent_id != agent.id:
            raise missing
        context = self._capability_policy_context(principal, agent, record)
        if not (await decide_policy(self.session_policy, context, PolicyAction.LOAD)).allowed:
            raise missing
        if action is not PolicyAction.LOAD:
            decision = await decide_policy(self.session_policy, context, action)
            if not decision.allowed:
                if decision.code == "SESSION_NOT_FOUND":
                    raise missing
                raise ApiError(decision.code, decision.message, status_code=403)
        return record

    @_admitted
    async def load_session(self, principal: Principal, agent_id: str, session_id: str) -> None:
        """Load a pinned session headlessly without executing queued work.

        The embedding application must supply an authenticated principal. A missing
        credential ID is not itself an authorization grant.
        """
        missing = ApiError("NOT_FOUND", "No session is available to this principal.")
        if not isinstance(principal, Principal):
            raise missing
        ambient = current_request.get()
        if ambient is not None and (
            ambient.principal != principal.id or ambient.credential_id != principal.credential_id
        ):
            raise missing
        agent = self.get_agent(agent_id, principal, allow_hidden=True)
        record = await self._authorized_session_candidate(principal, agent, session_id)
        agent = self.get_revision(agent_id, principal, version=record.definition_version).agent
        scopes = principal.scopes
        if ambient is not None and ambient.scopes is not None:
            scopes = ambient.scopes if scopes is None else scopes & ambient.scopes
        context = RequestContext(
            principal=principal.id,
            credential_id=principal.credential_id,
            connection_id=ambient.connection_id if ambient is not None else None,
            request_id=ambient.request_id if ambient is not None else None,
            agent_id=agent_id,
            scopes=scopes,
            session_id=session_id,
            operation_id=ambient.operation_id if ambient is not None else None,
            origin=ambient.origin if ambient is not None else None,
        )
        token = current_request.set(context)
        connection_token = current_connection_id.set(None)
        retirement = None
        try:
            async with self._actor_scope(agent.session_namespace):
                host = self._actors.get(agent.session_namespace)
                temporary = host is None
                if host is None:
                    host = self._build_host(agent, principal)
                    self._actors[agent.session_namespace] = host
                try:
                    try:
                        await host.authorize(session_id, PolicyAction.LOAD)
                    except HrnsError:
                        raise missing from None
                    await host.load_headless_session(session_id)
                    self._headless_actors.add(agent.session_namespace)
                except BaseException:
                    if temporary:
                        retirement = self._retire_actor(agent.session_namespace, host)
                    raise
        except HrnsError as exc:
            if exc.code in {"SESSION_NOT_FOUND", "FORBIDDEN", "POLICY_DENIED"}:
                raise missing from None
            status = {
                "SESSION_HELD": 409,
                "SESSION_CONFLICT": 409,
                "SESSION_CLOSING": 409,
                "SESSION_READ_ONLY": 403,
                "DEPLOYMENT_UNAVAILABLE": 503,
                "SESSION_DURABILITY_FAILED": 503,
                "SESSION_REPLAY_UNAVAILABLE": 503,
            }.get(exc.code, 500)
            raise ApiError(exc.code, exc.message, details=exc.details, status_code=status) from exc
        finally:
            current_connection_id.reset(connection_token)
            current_request.reset(token)
            if retirement is not None:
                await asyncio.shield(retirement)

    @_admitted
    async def submit_session_turn(
        self, principal: Principal, agent_id: str, session_id: str, request: TurnRequest
    ) -> TurnReceipt:
        """Submit one durable headless turn using a server-supplied origin and mutation ID."""
        if not isinstance(request, TurnRequest):
            raise ApiError("INVALID_REQUEST", "A validated turn request is required.")
        if (
            not request.mutation_id
            or len(request.mutation_id) > 200
            or not request.origin
            or len(request.origin) > 200
        ):
            raise ApiError("INVALID_REQUEST", "Invalid turn mutation ID or origin.")
        request = request.model_copy(deep=True)
        ambient = current_request.get()
        if ambient is not None and (
            ambient.principal != principal.id or ambient.credential_id != principal.credential_id
        ):
            raise ApiError("NOT_FOUND", "No session is available to this principal.")
        if (principal.scopes is not None and "queue:mutate" not in principal.scopes) or (
            ambient is not None
            and ambient.scopes is not None
            and "queue:mutate" not in ambient.scopes
        ):
            raise ApiError(
                "INSUFFICIENT_SCOPE",
                "The credential does not grant queue mutation scope.",
                status_code=403,
            )
        agent = self.get_agent(agent_id, principal, allow_hidden=True)
        record = await self._authorized_session_candidate(
            principal, agent, session_id, action=PolicyAction.PROMPT
        )
        agent = self.get_revision(agent_id, principal, version=record.definition_version).agent
        scopes = principal.scopes
        if ambient is not None and ambient.scopes is not None:
            scopes = ambient.scopes if scopes is None else scopes & ambient.scopes
        context = RequestContext(
            principal=principal.id,
            credential_id=principal.credential_id,
            connection_id=ambient.connection_id if ambient is not None else None,
            request_id=ambient.request_id if ambient is not None else None,
            agent_id=agent_id,
            scopes=scopes,
            session_id=session_id,
            operation_id=ambient.operation_id if ambient is not None else None,
            origin=ambient.origin if ambient is not None else None,
        )
        token = current_request.set(context)
        connection_token = current_connection_id.set(None)
        try:
            async with self._actor_scope(agent.session_namespace):
                host = self._actors.get(agent.session_namespace)
                if host is None:
                    host = self._build_host(agent, principal)
                    self._actors[agent.session_namespace] = host
                # Headless work can outlive an embedding request and the last viewer.
                self._headless_actors.add(agent.session_namespace)
                return await host.submit_headless_turn(session_id, request)
        except HrnsError as exc:
            if exc.code in {"SESSION_NOT_FOUND", "QUEUE_ITEM_NOT_FOUND"}:
                raise ApiError("NOT_FOUND", "No session is available to this principal.") from exc
            status = {
                "MUTATION_ID_REUSED": 409,
                "SESSION_CONFLICT": 409,
                "SESSION_HELD": 409,
                "SESSION_CLOSING": 409,
                "FORBIDDEN": 403,
                "POLICY_DENIED": 403,
                "SESSION_READ_ONLY": 403,
                "INVALID_CONFIGURATION": 400,
                "SESSION_DURABILITY_FAILED": 503,
            }.get(exc.code, 500)
            raise ApiError(exc.code, exc.message, details=exc.details, status_code=status) from exc
        finally:
            current_connection_id.reset(connection_token)
            current_request.reset(token)

    @_admitted
    async def get_session_turn(
        self,
        principal: Principal,
        agent_id: str,
        session_id: str,
        operation_id: str | None = None,
        *,
        mutation_id: str | None = None,
    ) -> TurnReceipt:
        """Read a durable turn under LOAD policy.

        ``TURN_NOT_FOUND`` means an authorized active-session lookup observed no
        caller-owned receipt. A concurrent submission may still succeed; retry only
        with the same mutation ID and request.
        """
        missing = ApiError("NOT_FOUND", "No session turn is available to this principal.")
        if (
            (operation_id is None) == (mutation_id is None)
            or (
                operation_id is not None and (not isinstance(operation_id, str) or not operation_id)
            )
            or (mutation_id is not None and (not isinstance(mutation_id, str) or not mutation_id))
        ):
            raise missing
        request = current_request.get()
        if request is not None and (
            request.principal != principal.id or request.credential_id != principal.credential_id
        ):
            raise missing
        agent = self.get_agent(agent_id, principal, allow_hidden=True)
        record = await self._authorized_session_candidate(principal, agent, session_id)
        agent = self.get_revision(agent_id, principal, version=record.definition_version).agent
        scopes = principal.scopes
        if request is not None and request.scopes is not None:
            scopes = request.scopes if scopes is None else scopes & request.scopes
        context = RequestContext(
            principal=principal.id,
            credential_id=principal.credential_id,
            connection_id=request.connection_id if request is not None else None,
            request_id=request.request_id if request is not None else None,
            agent_id=agent_id,
            scopes=scopes,
        )
        token = current_request.set(context)
        connection_token = current_connection_id.set(None)
        retirement = None
        try:
            async with self._actor_scope(agent.session_namespace):
                host = self._actors.get(agent.session_namespace)
                temporary = host is None
                if host is None:
                    host = self._build_host(agent, principal)
                    self._actors[agent.session_namespace] = host
                try:
                    receipt = (
                        await host.get_turn_receipt(
                            session_id, operation_id, principal=principal.id
                        )
                        if operation_id is not None
                        else await host.get_turn_receipt_by_mutation(
                            session_id, mutation_id or "", principal=principal.id
                        )
                    )
                    if receipt is None:
                        current = await self.store.get_policy_candidate(
                            agent.session_namespace, session_id
                        )
                        if (
                            current is None
                            or current.read_only
                            or current.agent_id != agent_id
                            or current.definition_version != record.definition_version
                            or current.effective_owner != record.effective_owner
                        ):
                            raise missing
                        await host.authorize(session_id, PolicyAction.LOAD, record=current)
                        raise ApiError(
                            "TURN_NOT_FOUND",
                            "No turn receipt was observed for this principal.",
                            status_code=404,
                        )
                finally:
                    if temporary:
                        retirement = self._retire_actor(agent.session_namespace, host)
        except HrnsError:
            raise missing from None
        finally:
            current_connection_id.reset(connection_token)
            current_request.reset(token)
            if retirement is not None:
                await asyncio.shield(retirement)
        if receipt is None:
            raise missing
        return receipt

    @_admitted
    async def reconcile_session_turns(self) -> None:
        """Resume recoverable queued turns; live foreign leases remain untouched."""
        await self._reconcile_session_turns()

    async def _reconcile_session_turns(self) -> None:
        seen: set[tuple[str, str]] = set()
        for row in await self.store.list_recoverable_headless_turns():
            key = (row.deployment_id, row.session_id)
            if key in seen:
                continue
            seen.add(key)
            principal = Principal(row.principal, credential_id=row.credential_id)
            context = RequestContext(principal=principal.id, credential_id=principal.credential_id)
            token = current_request.set(context)
            connection_token = current_connection_id.set(None)
            try:
                record = await self.store.get_policy_candidate(row.deployment_id, row.session_id)
                if record is None:
                    continue
                revision = next(
                    (
                        candidate
                        for agent_id in self.deployments.agent_ids()
                        for candidate in self.deployments.revisions(agent_id)
                        if candidate.agent.session_namespace == row.deployment_id
                        and candidate.agent_id == record.agent_id
                        and candidate.version == record.definition_version
                        and (candidate.agent_id, candidate.version) in self._native_deployments
                    ),
                    None,
                )
                if revision is None:
                    continue
                async with self._actor_scope(row.deployment_id):
                    host = self._actors.get(row.deployment_id)
                    if host is None:
                        host = self._build_host(revision.agent, principal)
                        self._actors[row.deployment_id] = host
                    self._headless_actors.add(row.deployment_id)
                    await host.authorize(row.session_id, PolicyAction.PROMPT)
                    await host.resume_queued_turns(row.session_id)
            except (ApiError, HrnsError):
                # Live leases and current policy denials remain for a later reconciliation.
                continue
            finally:
                current_connection_id.reset(connection_token)
                current_request.reset(token)

    async def close_session(
        self,
        principal: Principal,
        agent_id: str,
        session_id: str,
    ) -> SessionLifecycleResult:
        """Authoritatively close a scoped session and retain its reloadable state."""
        return await self._terminate_session(
            principal,
            agent_id,
            session_id,
            OperationKind.CLOSE,
        )

    async def delete_session(
        self,
        principal: Principal,
        agent_id: str,
        session_id: str,
    ) -> SessionLifecycleResult:
        """Authoritatively close a scoped session and delete all reloadable state."""
        return await self._terminate_session(
            principal,
            agent_id,
            session_id,
            OperationKind.DELETE,
        )

    @_admitted
    async def _terminate_session(
        self,
        principal: Principal,
        agent_id: str,
        session_id: str,
        kind: OperationKind,
    ) -> SessionLifecycleResult:
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        agent = self.get_agent(agent_id, principal, allow_hidden=True)
        namespace = agent.session_namespace
        request = RequestContext(
            principal=principal.id,
            credential_id=principal.credential_id,
            agent_id=agent_id,
            scopes=principal.scopes,
        )
        request_token = current_request.set(request)
        retirement = None
        try:
            async with self._actor_scope(namespace):
                host = self._actors.get(namespace)
                temporary = host is None
                if host is None:
                    host = self._build_host(agent, principal)
                    self._actors[namespace] = host
                try:
                    operation = await host.terminate_session(session_id, kind)
                finally:
                    if temporary and not host.has_pending_lifecycle_tasks():
                        retirement = self._retire_actor(namespace, host)
            if retirement is not None:
                await asyncio.shield(retirement)
        except HrnsError as exc:
            raise self._embedding_lifecycle_error(exc) from exc
        finally:
            current_request.reset(request_token)
        return SessionLifecycleResult(
            operation_id=operation.prompt_id,
            session_id=session_id,
            agent_id=agent_id,
            operation=kind,
        )

    @_admitted
    async def get_session_lifecycle(
        self,
        principal: Principal,
        agent_id: str,
        session_id: str,
        kind: OperationKind,
        operation_id: str | None = None,
    ) -> QueuedPrompt:
        """Read one owner-scoped durable close/delete receipt, including after deletion.

        Without an operation ID, discovery succeeds only when exactly one receipt of
        this kind exists for the session. Ambiguous histories require an explicit ID.
        The returned state is the stored operation state; a failed or uncertain
        operation is not proof that its external effects did or did not occur.
        """
        missing = ApiError(
            "NOT_FOUND", "No session lifecycle operation is available to this principal."
        )
        if (
            not principal.may_use(agent_id)
            or not isinstance(kind, OperationKind)
            or kind not in (OperationKind.CLOSE, OperationKind.DELETE)
            or (
                operation_id is not None and (not isinstance(operation_id, str) or not operation_id)
            )
        ):
            raise missing
        request = current_request.get()
        if request is not None and (
            request.principal != principal.id or request.credential_id != principal.credential_id
        ):
            raise missing
        try:
            agent = self.get_agent(agent_id, principal, allow_hidden=True)
        except ApiError:
            raise missing from None
        ambiguous = False
        if operation_id is None:
            # list_prompts is a single ordered store snapshot. A full page may have
            # omitted later generations, so never guess when the bound is reached.
            limit = 10_001
            history = await self.store.list_prompts(
                agent.session_namespace, session_id, limit=limit
            )
            matches = [item for item in history if item.kind is kind]
            operation = matches[-1] if matches else None
            ambiguous = len(matches) > 1 or len(history) == limit
        else:
            operation = await self.store.get_lifecycle_operation(
                agent.session_namespace, session_id, kind, operation_id=operation_id
            )
        if operation is None:
            raise missing
        try:
            payload = json.loads(operation.payload)
        except (ValueError, TypeError):
            raise missing from None
        if (
            not isinstance(payload, dict)
            or payload.get("owner") != principal.id
            or payload.get("agentId") != agent_id
            or payload.get("deploymentId") != agent.session_namespace
            or payload.get("operationId") != operation.prompt_id
            or payload.get("fenceDomain") != "sessionLease"
        ):
            raise missing
        definition_version = payload.get("definitionVersion")
        if not isinstance(definition_version, str) or not definition_version:
            raise missing
        try:
            self.get_revision(agent_id, principal, version=definition_version)
        except ApiError:
            raise missing from None
        record = await self.store.get_visible(agent.session_namespace, session_id, principal.id)
        if record is None:
            collaborators = payload.get("collaborators")
            metadata = payload.get("metadata")
            if (
                not isinstance(collaborators, list)
                or not all(isinstance(item, str) for item in collaborators)
                or not isinstance(metadata, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in metadata.items()
                )
            ):
                raise missing
            try:
                access = SessionAccess(payload.get("access"))
            except (TypeError, ValueError):
                raise missing from None
            context = SessionContext(
                session_id=session_id,
                principal=principal.id,
                deployment_id=agent.session_namespace,
                agent_id=agent_id,
                owner=principal.id,
                access=access,
                collaborators=frozenset(collaborators),
                credential_id=principal.credential_id,
                connection_id=request.connection_id if request is not None else None,
                request_id=request.request_id if request is not None else None,
                metadata=metadata,
            )
        else:
            if (
                record.effective_owner != principal.id
                or record.definition_version != definition_version
            ):
                raise missing
            context = self._capability_policy_context(principal, agent, record)
        if not (await decide_policy(self.session_policy, context, PolicyAction.LOAD)).allowed:
            raise missing
        if ambiguous:
            raise ApiError("CONFLICT", "Multiple lifecycle generations require an operation ID.")
        return operation

    @_admitted
    async def reconcile_session_lifecycle(self) -> tuple[SessionLifecycleResult, ...]:
        """Finish durable close/delete intents left by cancellation or process loss."""
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        results: list[SessionLifecycleResult] = []
        for operation in await self.store.list_pending_lifecycle_operations():
            agent = next(
                (
                    revision.agent
                    for agent_id in self.deployments.agent_ids()
                    for revision in self.deployments.revisions(agent_id)
                    if revision.agent.session_namespace == operation.deployment_id
                    and (revision.agent_id, revision.version) in self._native_deployments
                ),
                None,
            )
            if agent is None:
                raise ApiError(
                    "CONFLICT",
                    "A pending session lifecycle operation has no native deployment revision.",
                    details={
                        "sessionId": operation.session_id,
                        "operationId": operation.prompt_id,
                    },
                )
            principal = Principal(operation.principal, credential_id=operation.credential_id)
            request = RequestContext(
                principal=operation.principal,
                credential_id=operation.credential_id,
                connection_id=operation.connection_id,
                request_id=operation.prompt_id,
                agent_id=agent.id,
            )
            request_token = current_request.set(request)
            retirement = None
            try:
                async with self._actor_scope(operation.deployment_id):
                    host = self._actors.get(operation.deployment_id)
                    temporary = host is None
                    if host is None:
                        host = self._build_host(agent, principal)
                        self._actors[operation.deployment_id] = host
                    try:
                        completed = await host.terminate_session(
                            operation.session_id,
                            operation.kind,
                            operation=operation,
                            authorize=False,
                        )
                    finally:
                        if temporary and not host.has_pending_lifecycle_tasks():
                            retirement = self._retire_actor(operation.deployment_id, host)
                if retirement is not None:
                    await asyncio.shield(retirement)
            except HrnsError as exc:
                raise self._embedding_lifecycle_error(exc) from exc
            finally:
                current_request.reset(request_token)
            results.append(
                SessionLifecycleResult(
                    operation_id=completed.prompt_id,
                    session_id=completed.session_id,
                    agent_id=agent.id,
                    operation=completed.kind,
                )
            )
        return tuple(results)

    @staticmethod
    def _embedding_lifecycle_error(error: HrnsError) -> ApiError:
        if error.code == "SESSION_NOT_FOUND":
            return ApiError(
                "NOT_FOUND",
                "No session with this id is available to this principal.",
            )
        if error.code in {"SESSION_READ_ONLY", "POLICY_DENIED"}:
            return ApiError("FORBIDDEN", error.message, details=error.details)
        if error.code in {"SESSION_HELD", "SESSION_CONFLICT", "SESSION_CLOSING"}:
            return ApiError("CONFLICT", error.message, details=error.details)
        if error.code == "LIFECYCLE_FAILED":
            return ApiError(
                "LIFECYCLE_FAILED",
                error.message,
                details=error.details,
                status_code=500,
            )
        return ApiError(
            "INTERNAL_ERROR",
            error.message,
            details={"hrnsCode": error.code, **error.details},
        )

    def _host_for_deployment(self, deployment_id: str) -> HrnsACPAgent | None:
        """A live actor for this logical agent, regardless of which revision opened it."""
        return self._actors.get(deployment_id)

    async def _admin_lease(
        self, principal: Principal, deployment_id: str, session_id: str
    ) -> SessionLease | None:
        """Guard an embedding-API write with the session lease.

        A live actor reuses its own lease. With no actor in this process a short-lived
        administrative lease is taken; a live other worker wins and the write is
        refused.
        """
        if await self.store.get(principal.id, deployment_id, session_id) is None:
            # Do not create a coordination row for a session that does not exist.
            return None
        for kind in (OperationKind.DELETE, OperationKind.CLOSE):
            operation = await self.store.get_lifecycle_operation(
                deployment_id,
                session_id,
                kind,
            )
            if operation is not None and operation.state is not PromptState.COMPLETED:
                raise ApiError(
                    "CONFLICT",
                    "The session is closing and no longer accepts administrative writes.",
                    details={
                        "sessionId": session_id,
                        "operationId": operation.prompt_id,
                    },
                )
        host = self._actors.get(deployment_id)
        if host is not None:
            try:
                await host.acquire_session_lease(session_id)
            except HrnsError as exc:
                raise ApiError("CONFLICT", exc.message) from exc
            return None
        owner = f"{self._instance_id}:admin:{uuid.uuid4().hex}"
        acquisition = asyncio.create_task(
            self.store.acquire_lease(
                principal.id, deployment_id, session_id, owner, self.lease_ttl_ms
            )
        )
        try:
            lease = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            while not acquisition.done():
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(acquisition)
            if not acquisition.cancelled() and acquisition.exception() is None:
                acquired = acquisition.result()
                if acquired is not None:
                    with suppress(asyncio.CancelledError, Exception):
                        await self._release_admin_lease(acquired)
            raise
        if lease is None:
            raise ApiError(
                "CONFLICT",
                "The session is owned by another live worker.",
                details={"sessionId": session_id},
            )
        return lease

    async def _release_admin_lease(self, lease: SessionLease) -> None:
        release = asyncio.create_task(
            self.store.release_lease(
                lease.principal,
                lease.deployment_id,
                lease.session_id,
                lease.owner_id,
                lease.fence,
            )
        )
        cancelled = False
        while not release.done():
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError:
                cancelled = True
        release.result()
        if cancelled:
            raise asyncio.CancelledError

    @_admitted
    async def revoke_credential(self, credential_id: str) -> int:
        """Close every live connection using ``credential_id`` before returning."""
        return await self.connections.revoke_credential(credential_id)

    @_admitted
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
        if self._running or self._starting or self._stopping:
            raise RuntimeError("AgentServer is already running")
        self._starting = True
        try:
            await self.store.initialize()
            await self._restore_deployments()
            await self._flush_deployments()
            await self._reconcile_session_creations()
            self._running = True
            await self._reconcile_session_turns()
            self._starting = False
            yield
        finally:
            # Close admission synchronously before taking any task or connection snapshot.
            self._running = False
            self._starting = False
            self._stopping = True
            cleanup = asyncio.create_task(self._shutdown())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            # Propagate cleanup errors before reporting cancellation.
            cleanup.result()
            self._stopping = False
            if cancelled:
                raise asyncio.CancelledError

    async def _shutdown(self) -> None:
        failures: list[BaseException] = []
        try:
            current = asyncio.current_task()
            requests = tuple(task for task in self._admitted_tasks if task is not current)
            creations = tuple(self._creation_tasks)
            for task in (*requests, *creations):
                task.cancel()
            if requests or creations:
                await asyncio.gather(*requests, *creations, return_exceptions=True)
            try:
                await self.connections.close_all()
            except Exception as exc:
                failures.append(exc)
            connections = tuple(task for task in self._connection_tasks if task is not current)
            for task in connections:
                task.cancel()
            if connections:
                await asyncio.gather(*connections, return_exceptions=True)
            if self._retirements:
                results = await asyncio.gather(
                    *tuple(self._retirements.values()), return_exceptions=True
                )
                failures.extend(result for result in results if isinstance(result, BaseException))
                self._retirements.clear()
            hosts = tuple(self._actors.values())
            self._actors.clear()
            self._headless_actors.clear()
            if hosts:
                results = await asyncio.gather(
                    *(host.shutdown() for host in hosts), return_exceptions=True
                )
                failures.extend(result for result in results if isinstance(result, BaseException))
            self._hosts.clear()
            if self._persist_tasks:
                results = await asyncio.gather(*tuple(self._persist_tasks), return_exceptions=True)
                failures.extend(result for result in results if isinstance(result, BaseException))
            try:
                await self._flush_deployments()
            except Exception as exc:
                failures.append(exc)
        finally:
            try:
                await self.store.close()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise BaseExceptionGroup("Agent server shutdown failed", failures)

    def _build_host(self, agent: RegisteredAgent, principal: Principal) -> HrnsACPAgent:
        version = agent.definition.definition_version or ""

        async def hosted_create(incoming: SessionNewInput) -> SessionCreationResult:
            request_context = current_request.get()
            acting = Principal(
                request_context.principal if request_context is not None else principal.id,
                agent_ids=frozenset({agent.id}),
                credential_id=request_context.credential_id
                if request_context is not None
                else principal.credential_id,
                scopes=request_context.scopes if request_context is not None else principal.scopes,
            )
            mutation_id = incoming.mutation_id or uuid.uuid4().hex
            selected = incoming.definition_version
            if selected is None:
                existing = await self.store.get_creation(acting.id, mutation_id)
                if existing is not None and existing.request.agent_id == agent.id:
                    selected = existing.request.definition_version
                else:
                    current = self._native_revision(agent.id)
                    if current is None:
                        raise HrnsError(
                            "DEPLOYMENT_UNAVAILABLE",
                            "No deployment is available to this principal.",
                        )
                    selected = current.version
            incoming = incoming.model_copy(
                update={
                    "mutation_id": mutation_id,
                    "definition_version": selected,
                }
            )
            policy_source = self.initial_session_policy
            try:
                policy = (
                    policy_source(acting, incoming) if callable(policy_source) else policy_source
                )
                if inspect.isawaitable(policy):
                    policy = await policy
            except Exception as exc:
                raise HrnsError(
                    "POLICY_EVALUATION_FAILED", "The initial session policy could not be resolved."
                ) from exc
            try:
                return await self.create_session(
                    acting,
                    agent.id,
                    definition_version=selected,
                    mutation_id=incoming.mutation_id or "",
                    policy=policy,
                    cwd=incoming.cwd,
                    initial_configuration=incoming.initial_configuration,
                    effort=incoming.effort,
                    client_capabilities=incoming.client_capabilities,
                )
            except ApiError as exc:
                raise HrnsError(exc.code, exc.message) from exc

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
            creation_handler=hosted_create,
            resource_resolver=self.resource_resolver,
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
        task = asyncio.current_task()
        if task is None:
            raise ApiError("SERVER_BUSY", "No ASGI handler task for this connection")
        self._connection_tasks.add(task)
        try:
            async with self._connection(socket, agent, principal) as host:
                yield host
        finally:
            self._connection_tasks.discard(task)

    @asynccontextmanager
    async def _connection(
        self, socket: WebSocket, agent: RegisteredAgent, principal: Principal
    ) -> AsyncGenerator[HrnsACPAgent, None]:
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        if self._pending_deployments:
            await self._flush_deployments()
        if not self._running:
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
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
        if not self._running:
            self.connections.unregister(info.connection_id)
            raise ApiError("SERVER_BUSY", "The agent server lifespan is not running")
        namespace = agent.session_namespace
        try:
            async with self._actor_scope(namespace):
                host = self._actors.get(namespace)
                if host is None:
                    host = self._build_host(agent, principal)
                    self._actors[namespace] = host
                self._hosts[info.connection_id] = host
        except BaseException:
            # A constructor failure must not keep counting against the connection limit.
            self.connections.unregister(info.connection_id)
            raise
        request = RequestContext(
            principal=principal.id,
            credential_id=principal.credential_id,
            connection_id=info.connection_id,
            agent_id=agent.id,
            scopes=principal.scopes,
        )
        request_token = current_request.set(request)
        connection_token = current_connection_id.set(info.connection_id)
        host.request_context = request
        try:
            yield host
        finally:
            current_request.reset(request_token)
            current_connection_id.reset(connection_token)
            cleanup = asyncio.create_task(self._detach_connection(namespace, info, host))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    async def _detach_connection(
        self, namespace: str, info: ConnectionInfo, host: HrnsACPAgent
    ) -> None:
        retirement = None
        async with self._actor_scope(namespace):
            host.detach(info.connection_id)
            self._hosts.pop(info.connection_id, None)
            self.connections.unregister(info.connection_id)
            # A reconnect either attaches first or waits outside the lock for retirement.
            still_attached = any(candidate is host for candidate in self._hosts.values())
            if (
                namespace not in self._headless_actors
                and not host.fanout.connections()
                and not still_attached
            ):
                retirement = self._retire_actor(namespace, host)
        if retirement is not None:
            await asyncio.shield(retirement)
