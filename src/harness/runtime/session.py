"""Sessions: native agent execution, next-turn composition, and configuration control.

A :class:`Session` owns one conversation. Every turn captures an immutable active
configuration revision, builds a *fresh composition* from the definition's fixed behavior
plus the enabled managed bindings, and runs it through the native Pydantic AI agent —
preserving capability behavior (hooks, instructions, model settings, toolsets) rather than
reimplementing it.

Disabled bindings are omitted entirely from the next turn. Configuration changes made while
a turn is running apply to the following turn; the running turn keeps its captured revision.
Persistence is SQLite WAL by default: selected revisions, idempotent transaction records,
turn outcomes, native message history, and the renderable transcript.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import inspect
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, CancellationToken, RunCancelled
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.usage import RunUsage
from pydantic_core import to_jsonable_python

from harness.protocol import (
    BindingChange,
    ConfigurationSnapshot,
    EnableChange,
    HarnessError,
    RuntimeEvent,
    changes_adapter,
    paginate,
)

from ._events import (
    TranslationState,
    deferred_requests_payload,
    output_payload,
    translate_event,
    usage_payload,
)
from .definitions import (
    AgentDefinition,
    AgentFactoryContext,
    factory_declared_params,
    factory_managed_param,
)
from .jobs import OwnedJob, SessionJobManager
from .policy import (
    PolicyCatalog,
    configuration_payload_hash,
    jsonable_config,
)
from .registry import PluginRegistry
from .resources import ResourceSet, resolve_workspace_value
from .store import SessionRecord, SessionStore, SessionSummary
from .workspaces import (
    WorkspaceLease,
    WorkspaceManager,
    WorkspaceProvider,
    WorkspaceRecord,
)

__all__ = [
    "HarnessRuntime",
    "Session",
    "SessionManager",
    "SessionOpener",
    "SessionSummary",
]

_END = object()
"""Queue sentinel marking the end of a streamed turn."""

_MISSING = object()

_RUN_TASK_SCOPE: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "harness_run_task_scope", default=None
)


def _task_context(task: asyncio.Task[Any]) -> contextvars.Context | None:
    """Best-effort access to a task's captured context across supported Pythons."""
    getter = getattr(task, "get_context", None)
    if callable(getter):
        try:
            return getter()
        except Exception:  # pragma: no cover - defensive across Python versions
            return None
    return getattr(task, "_context", None)


def _detached_run_context() -> contextvars.Context:
    """Copy the current context with run-scope tracking disabled.

    Explicit session jobs must outlive the turn that registered them, so they (and tasks
    they spawn) are not treated as run-scoped children.
    """
    context = contextvars.copy_context()
    context.run(_RUN_TASK_SCOPE.set, None)
    return context


def _new_session_id() -> str:
    return f"s_{uuid.uuid4().hex}"


def _new_turn_id() -> str:
    return f"t_{uuid.uuid4().hex}"


def _snapshot_definition(definition: AgentDefinition) -> AgentDefinition:
    """Copy the mutable parts of a definition so a registered pin cannot drift in place.

    Factories and fixed native capability objects stay shared because they are host code,
    not configuration. Specs, bindings, resources, and feature lists are cloned so a caller
    cannot mutate a session's pinned revision after it was registered or opened.
    """
    clone = definition.model_copy(deep=False)
    clone.bindings = [binding.model_copy(deep=True) for binding in definition.bindings]
    clone.resources = [requirement.model_copy(deep=True) for requirement in definition.resources]
    clone.interaction_features = list(definition.interaction_features)
    clone.fixed_capabilities = list(definition.fixed_capabilities)
    if isinstance(definition.spec, BaseModel):
        clone.spec = definition.spec.model_copy(deep=True)
    elif isinstance(definition.spec, dict):
        clone.spec = copy.deepcopy(definition.spec)
    return clone


def _coerce_store(store: SessionStore | str | Path | None) -> SessionStore:
    if isinstance(store, SessionStore):
        return store
    if store is not None:
        from .postgres import PostgresSessionStore, is_postgres_dsn

        if is_postgres_dsn(str(store)):
            return PostgresSessionStore(str(store))
    return SessionStore(store)


def _normalize_changes(changes: Any) -> list[BindingChange]:
    if not isinstance(changes, Sequence) or isinstance(changes, str | bytes):
        raise HarnessError("INVALID_CONFIGURATION", "changes must be a list of binding changes.")
    payload: list[Any] = []
    for change in changes:
        if isinstance(change, BaseModel):
            payload.append(change.model_dump(mode="json"))
        elif isinstance(change, Mapping):
            payload.append(dict(change))
        else:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Unsupported binding change of type {type(change).__name__}.",
            )
    try:
        return list(changes_adapter.validate_python(payload))
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()[:5]
        )
        raise HarnessError(
            "INVALID_CONFIGURATION",
            f"Invalid configuration transaction: {errors}",
            details={"errors": exc.errors()[:10]},
        ) from exc


def _prompt_payload(prompt: Any) -> Any:
    if isinstance(prompt, str):
        return prompt
    try:
        return to_jsonable_python(prompt)
    except Exception:  # pragma: no cover - defensive
        return str(prompt)


def _coerce_prompt(prompt: Any) -> Any:
    """Convert ACP prompt block mappings into native Pydantic AI user content.

    ACP hosts pass content blocks as JSON mappings (``{'type': 'text', ...}``); Python
    callers may pass native ``UserContent`` directly. Text and inline binary blocks map
    onto native content parts so images/audio retain model-visible semantics.
    """
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, Sequence) or isinstance(prompt, bytes | bytearray):
        return prompt
    from pydantic_ai.messages import BinaryContent

    converted: list[Any] = []
    for block in prompt:
        if isinstance(block, str) or not isinstance(block, Mapping):
            converted.append(block)
            continue
        block_type = block.get("type")
        if block_type == "text":
            converted.append(str(block.get("text", "")))
            continue
        if block_type in {"image", "audio"}:
            data = block.get("data")
            media_type = block.get("mimeType") or block.get("mime_type")
            if isinstance(data, str) and media_type:
                import base64

                try:
                    converted.append(
                        BinaryContent(data=base64.b64decode(data), media_type=str(media_type))
                    )
                    continue
                except Exception:  # pragma: no cover - fall back to a readable description
                    pass
            converted.append(f"[{block_type} attachment]")
            continue
        if block_type == "resource_link":
            uri = block.get("uri") or ""
            name = block.get("name") or uri
            converted.append(f"[resource: {name} ({uri})]")
            continue
        if block_type == "resource":
            resource = block.get("resource") or {}
            if isinstance(resource, Mapping):
                if isinstance(resource.get("text"), str):
                    converted.append(str(resource["text"]))
                    continue
                uri = resource.get("uri")
                if uri:
                    converted.append(f"[resource: {uri}]")
                    continue
            converted.append("[resource attachment]")
            continue
        # Unknown block kinds stay as readable text rather than crashing the turn.
        converted.append(f"[{block_type or 'content'} attachment]")
    return converted


def _messages_json(messages: Sequence[Any]) -> list[Any]:
    if not messages:
        return []
    return ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")


def _load_messages(text: str) -> list[Any]:
    try:
        return ModelMessagesTypeAdapter.validate_json(text)
    except Exception as exc:
        raise HarnessError(
            "INVALID_CONFIGURATION",
            f"Persisted conversation history could not be reconstructed: {exc}",
        ) from exc


def _error_event_payload(exc: BaseException, *, turn_id: str | None = None) -> dict[str, Any]:
    if isinstance(exc, HarnessError):
        try:
            details = to_jsonable_python(dict(exc.details or {}), serialize_unknown=True)
        except Exception:  # pragma: no cover - defensive
            details = {"error": str(exc)}
        payload = {
            "code": exc.code,
            "message": exc.message,
            "details": details,
        }
    elif isinstance(exc, RunCancelled):
        payload = {
            "code": "TURN_CANCELLED",
            "message": str(exc) or "The turn was cancelled.",
            "details": {"cancelled": True},
        }
    else:
        payload = {
            "code": "TURN_FAILED",
            "message": f"{type(exc).__name__}: {exc}",
            "details": {"exception": type(exc).__name__},
        }
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return payload


def _usage_from_payload(payload: Mapping[str, Any] | None) -> RunUsage:
    if not payload:
        return RunUsage()
    known = {
        key: value
        for key, value in payload.items()
        if key
        in {
            "requests",
            "tool_calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "input_audio_tokens",
            "output_audio_tokens",
            "details",
            "cost",
        }
    }
    try:
        return RunUsage(**known)
    except Exception:  # pragma: no cover - defensive
        return RunUsage()


@dataclass
class ActiveTurn:
    turn_id: str
    revision: int
    cancel_token: Any
    bindings: dict[str, dict[str, Any]]
    events: Any = None


@dataclass(frozen=True)
class WorkspaceBinding:
    """Deployment's configured managed-workspace binding for new sessions.

    ``provider`` names a provider registered on the session manager's
    :class:`~harness.runtime.workspaces.WorkspaceManager`; it is never client input.
    Named workspaces imply persistent retention.
    """

    provider: str
    mode: Literal["ephemeral", "persistent"] = "ephemeral"
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.provider.strip():
            raise ValueError("workspace binding provider must not be blank")
        if self.mode not in ("ephemeral", "persistent"):
            raise ValueError(f"unsupported workspace retention mode {self.mode!r}")
        if self.name is not None and self.mode != "persistent":
            raise ValueError("named workspaces must use persistent retention")


def _capability_tool_names(capability: Any) -> set[str]:
    names: set[str] = set()
    for tool in getattr(capability, "tools", ()) or ():
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
        if isinstance(name, str) and name:
            names.add(name)
    for toolset in getattr(capability, "toolsets", ()) or ():
        registered = getattr(toolset, "tools", None)
        if isinstance(registered, Mapping):
            names.update(str(name) for name in registered)
    return names


def _validate_composed_identities(
    definition: AgentDefinition,
    composed: Sequence[tuple[str, Any]],
) -> None:
    """Reject duplicate native capability IDs and tool names before a turn starts.

    Upstream Pydantic AI also rejects these, but for factories that receive managed
    bindings per run the failure would occur after the turn was accepted. CONCEPT
    §7.3/§8.2 require a collision to reject preparation instead.
    """
    owners: dict[str, str] = {}
    tool_owners: dict[str, str] = {}
    fixed = list(getattr(definition, "fixed_capabilities", ()) or ())
    claims: list[tuple[str, Any]] = [
        (f"fixed[{index}]", capability) for index, capability in enumerate(fixed)
    ]
    claims.extend((f"binding:{binding_id}", capability) for binding_id, capability in composed)
    for owner, capability in claims:
        capability_id = getattr(capability, "id", None)
        if isinstance(capability_id, str) and capability_id:
            previous = owners.get(capability_id)
            if previous is not None:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Capability id {capability_id!r} is used by both {previous} and {owner}; "
                    "native capability identities must be unique.",
                    details={"capability_id": capability_id, "owners": [previous, owner]},
                )
            owners[capability_id] = owner
        for tool_name in _capability_tool_names(capability):
            previous = tool_owners.get(tool_name)
            if previous is not None:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Tool name {tool_name!r} is contributed by both {previous} and {owner}; "
                    "rename or prefix the tool before composing this turn.",
                    details={"tool_name": tool_name, "owners": [previous, owner]},
                )
            tool_owners[tool_name] = owner


def _bind_workspace_lease(
    resources: ResourceSet,
    definition: AgentDefinition,
    lease: WorkspaceLease,
) -> ResourceSet:
    """Bind a managed lease under every workspace requirement name.

    Hosts commonly pre-bind a local cwd under a requirement such as ``workspace.main``.
    When the deployment selected a managed workspace, that lease is the workspace for this
    session: the exact requirement names and the conventional ``workspace`` alias must all
    resolve to it so a host path can never become a silent fallback.
    """
    values: dict[str, Any] = {"workspace": lease.workspace}
    for requirement in definition.resources:
        if requirement.kind == "workspace":
            values[requirement.name] = lease.workspace
    return resources.merge(values)


@dataclass
class TurnOptions:
    output_type: Any = None
    deps: Any = None
    model: Any = None
    model_settings: Any = None
    instructions: Any = None
    retries: Any = None
    usage_limits: Any = None
    deferred_tool_results: Any = None
    turn_id: str | None = None


class SessionOpener:
    """Dual-mode session opener: ``await`` it, or use it as an async context manager."""

    def __init__(self, coro: Any) -> None:
        self._coro = coro
        self._session: Session | None = None

    def __await__(self) -> Any:
        return self._coro.__await__()

    async def __aenter__(self) -> Session:
        self._session = await self._coro
        return self._session

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._session is not None:
            await self._session.close()


class SessionManager:
    """Owns definition/plugin resolution and durable session lifecycle.

    One manager can host many sessions. Different sessions run concurrently; one session has
    one controlling :class:`Session` object and one prompt turn at a time.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        *,
        store: SessionStore | str | Path | None = None,
        definitions: Sequence[AgentDefinition] | None = None,
        allow_local_binding_creation: bool = False,
        workspaces: WorkspaceManager | None = None,
        workspace_providers: Sequence[WorkspaceProvider] = (),
        workspace_binding: WorkspaceBinding | None = None,
    ) -> None:
        self.registry = registry
        if allow_local_binding_creation:
            registry.allow_local_binding_creation = True
        self._owns_store = not isinstance(store, SessionStore)
        self._store = _coerce_store(store)
        self._definitions: dict[tuple[str, str], AgentDefinition] = {}
        self._live: dict[str, Session] = {}
        if workspaces is not None and workspace_providers:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "Pass either a configured WorkspaceManager or workspace_providers, not both.",
            )
        if workspace_binding is not None and workspaces is None and not workspace_providers:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "workspace_binding requires a configured WorkspaceManager or workspace_providers.",
            )
        if workspace_providers:
            self._workspaces = WorkspaceManager(
                providers=list(workspace_providers),
                record_store=self._store.workspace_record_callbacks(),
            )
        else:
            self._workspaces = workspaces
            if workspaces is not None:
                self._attach_workspace_store(workspaces)
        self._workspace_binding = workspace_binding
        for definition in definitions or ():
            self.register_definition(definition)

    def _attach_workspace_store(self, workspaces: WorkspaceManager) -> None:
        """Persist a configured manager's records in this session store when unset.

        A manager created without a record store keeps them in an empty in-memory mapping;
        that would lose workspace identity across processes. Adopt this store's callbacks in
        that case. A manager the host wired to its own durable store is left untouched.
        """
        record_store = getattr(workspaces, "_record_store", None)
        if type(record_store).__name__ != "_MappingRecordStore":
            return
        values = getattr(record_store, "_values", None)
        if isinstance(values, dict) and not values:
            workspaces._record_store = self._store.workspace_record_callbacks()  # type: ignore[attr-defined]

    @property
    def store(self) -> SessionStore:
        """The manager's default session store."""
        return self._store

    @property
    def workspaces(self) -> WorkspaceManager | None:
        """The configured workspace lifecycle service, when this host has one."""
        return self._workspaces

    @property
    def workspace_binding(self) -> WorkspaceBinding | None:
        """Default managed-workspace binding used for new sessions."""
        return self._workspace_binding

    async def cleanup_workspaces(self, *, now: float | None = None) -> Any | None:
        """Run workspace TTL cleanup (hosts call this at startup and periodically)."""
        if self._workspaces is None:
            return None
        return await self._workspaces.cleanup(now=now)

    # --- definitions --------------------------------------------------

    def register_definition(self, definition: AgentDefinition) -> AgentDefinition:
        """Register a pinned definition revision without silently replacing it.

        The stored revision is a snapshot: later mutation of the caller's definition object
        cannot change an already-registered pin or an open session.
        """
        key = (definition.agent_id, definition.definition_version)
        snapshot = _snapshot_definition(definition)
        existing = self._definitions.get(key)
        if existing is not None:
            if existing != snapshot:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Definition {definition.agent_id!r} version "
                    f"{definition.definition_version!r} is already registered with different "
                    "behavior; publish a new version instead.",
                    details={
                        "agent_id": definition.agent_id,
                        "definition_version": definition.definition_version,
                    },
                )
            return _snapshot_definition(existing)
        self._definitions[key] = snapshot
        return _snapshot_definition(snapshot)

    def definition(self, agent_id: str, definition_version: str) -> AgentDefinition | None:
        stored = self._definitions.get((agent_id, definition_version))
        return _snapshot_definition(stored) if stored is not None else None

    # --- opening ------------------------------------------------------

    def open_session(
        self,
        definition: AgentDefinition,
        *,
        resources: ResourceSet | Mapping[str, Any] | None = None,
        store: SessionStore | str | Path | None = None,
        principal: str = "local",
        deployment_id: str = "local",
        initial_configuration: Sequence[BindingChange] | None = None,
        session_id: str | None = None,
        title: str | None = None,
        resume: Any | None = None,
    ) -> SessionOpener:
        """Open a new session, or resume a persisted one when ``resume`` is supplied.

        Awaitable and an async context manager. ``resume`` may be a stored session record
        mapping/object (anything exposing ``session_id``) or a session id string.
        """
        if resume is not None:
            resumed_id = (
                resume.get("session_id") if isinstance(resume, Mapping) else getattr(resume, "session_id", None)
            )
            if not resumed_id and isinstance(resume, str):
                resumed_id = resume
            if not resumed_id:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    "resume must identify a stored session.",
                )
            return SessionOpener(
                self._resume_for_opener(
                    principal=principal,
                    session_id=str(resumed_id),
                    resources=resources,
                    deployment_id=deployment_id,
                    store=store,
                )
            )
        return SessionOpener(
            self._open_session(
                definition,
                resources=resources,
                store=store,
                principal=principal,
                deployment_id=deployment_id,
                initial_configuration=initial_configuration,
                session_id=session_id,
                title=title,
            )
        )

    async def _resume_for_opener(
        self,
        *,
        principal: str,
        session_id: str,
        resources: ResourceSet | Mapping[str, Any] | None,
        deployment_id: str | None,
        store: SessionStore | str | Path | None,
    ) -> Session:
        return await self.open_saved_session(
            principal,
            session_id,
            resources=resources,
            deployment_id=deployment_id,
            store=store,
        )

    @staticmethod
    def _workspace_requirements(definition: AgentDefinition) -> list[Any]:
        return [requirement for requirement in definition.resources if requirement.kind == "workspace"]

    async def _managed_workspace_lease(
        self,
        session_id: str,
        definition: AgentDefinition,
        *,
        stored: WorkspaceRecord | None,
    ) -> WorkspaceLease | None:
        """Acquire the configured managed workspace, with no implicit local fallback.

        A provider choice comes from the deployment's :class:`WorkspaceBinding` for a new
        session, or from the persisted record on reload. Unknown or unreachable workspaces
        raise; the caller must never substitute a host path or an empty environment.
        """
        if self._workspaces is None:
            return None
        if not self._workspace_requirements(definition):
            return None
        if stored is not None:
            return await self._workspaces.acquire(
                session_id,
                provider=stored.provider,
                mode=stored.mode,
                name=stored.name,
            )
        binding = self._workspace_binding
        if binding is None:
            return None
        return await self._workspaces.acquire(
            session_id,
            provider=binding.provider,
            mode=binding.mode,
            name=binding.name,
        )

    async def _open_session(
        self,
        definition: AgentDefinition,
        *,
        resources: ResourceSet | Mapping[str, Any] | None,
        store: SessionStore | str | Path | None,
        principal: str,
        deployment_id: str,
        initial_configuration: Sequence[BindingChange] | None,
        session_id: str | None,
        title: str | None,
    ) -> Session:
        definition = self.register_definition(definition)
        resource_set = resources if isinstance(resources, ResourceSet) else ResourceSet(resources)
        context = f"Agent {definition.agent_id!r}"
        workspace_requirements = self._workspace_requirements(definition)
        other_requirements = [req for req in definition.resources if req.kind != "workspace"]
        resource_set.validate_requirements(other_requirements, context=context)
        managed_workspace = (
            self._workspaces is not None
            and bool(workspace_requirements)
            and self._workspace_binding is not None
        )
        if not managed_workspace:
            resource_set.validate_requirements(workspace_requirements, context=context)
        resolved_session_id = session_id or _new_session_id()
        if resolved_session_id in self._live:
            raise HarnessError(
                "SESSION_BUSY",
                f"Session {resolved_session_id!r} is already controlled in this process.",
                details={"session_id": resolved_session_id},
            )
        target_store = _coerce_store(store) if store is not None else self._store
        owns_store = store is not None and not isinstance(store, SessionStore)
        if (
            managed_workspace
            and self._workspaces is not None
            and str(target_store.path) != str(self._store.path)
        ):
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "Managed workspace records live in the session manager's store; a per-session "
                "store override is not supported with a configured workspace manager.",
                details={"session_id": resolved_session_id},
            )
        catalog = PolicyCatalog(
            definition,
            self.registry,
            session_id=resolved_session_id,
            principal=principal,
            deployment_id=deployment_id,
        )
        revision: int | None = None
        try:
            if initial_configuration:
                normalized = _normalize_changes(initial_configuration)
                outcome = catalog.apply_changes(normalized, initial_configuration=True)
                if outcome.changed:
                    catalog.commit(outcome)
            record = SessionRecord(
                session_id=resolved_session_id,
                principal=principal,
                deployment_id=deployment_id,
                agent_id=definition.agent_id,
                definition_version=definition.definition_version,
                title=title or definition.name,
                cwd=_cwd_from_resources(definition, resource_set),
                workspace_managed=managed_workspace,
            )
            stored = await target_store.create_session(
                record,
                initial_bindings=catalog.export_bindings(),
                policy_revision=catalog.policy_revision,
            )
            revision = stored.revision
            owner_token = uuid.uuid4().hex
            if not await target_store.acquire_control(resolved_session_id, principal, owner_token):
                raise HarnessError(
                    "SESSION_BUSY",
                    f"Session {resolved_session_id!r} is controlled by another worker.",
                    details={"session_id": resolved_session_id},
                )
            await target_store.mark_revision_applied(resolved_session_id, revision)
            await target_store.touch_session(resolved_session_id)
            workspace_lease: WorkspaceLease | None = None
            if managed_workspace:
                workspace_lease = await self._managed_workspace_lease(
                    resolved_session_id, definition, stored=None
                )
                if workspace_lease is not None:
                    resource_set = _bind_workspace_lease(
                        resource_set, definition, workspace_lease
                    )
                resource_set.validate_requirements(definition.resources, context=context)
        except BaseException:
            if revision is not None:
                if managed_workspace and self._workspaces is not None:
                    try:
                        await self._workspaces.delete(resolved_session_id)
                    except Exception:  # pragma: no cover - cleanup is best effort
                        pass
                try:
                    await target_store.delete_session(resolved_session_id)
                except Exception:  # pragma: no cover - cleanup is best effort
                    pass
            if owns_store:
                await target_store.close()
            raise
        session = Session(
            manager=self,
            definition=definition,
            catalog=catalog,
            store=target_store,
            owns_store=owns_store,
            resources=resource_set,
            principal=principal,
            deployment_id=deployment_id,
            session_id=resolved_session_id,
            selected_revision=revision,
            owner_token=owner_token,
            workspace_lease=workspace_lease,
        )
        self._live[resolved_session_id] = session
        return session

    async def open_saved_session(
        self,
        principal: str,
        session_id: str,
        *,
        resources: ResourceSet | Mapping[str, Any] | None = None,
        deployment_id: str | None = None,
        store: SessionStore | str | Path | None = None,
    ) -> Session:
        """Reconstruct a persisted session from its definition pin and stored state."""
        if session_id in self._live:
            raise HarnessError(
                "SESSION_BUSY",
                f"Session {session_id!r} is already controlled in this process.",
                details={"session_id": session_id},
            )
        target_store = _coerce_store(store) if store is not None else self._store
        summary = await target_store.get_session(session_id)
        if summary is None or summary.principal != principal or (
            deployment_id is not None and summary.deployment_id != deployment_id
        ):
            raise HarnessError(
                "POLICY_DENIED",
                f"Session {session_id!r} is not available to this principal.",
                details={"session_id": session_id, "reason": "ownership"},
            )
        definition = self.definition(summary.agent_id, summary.definition_version)
        if definition is None:
            raise HarnessError(
                "DEPLOYMENT_UNAVAILABLE",
                f"The pinned deployment for session {session_id!r} "
                f"({summary.agent_id}@{summary.definition_version}) is not installed on this host.",
                details={
                    "session_id": session_id,
                    "agent_id": summary.agent_id,
                    "definition_version": summary.definition_version,
                },
            )
        resource_set = resources if isinstance(resources, ResourceSet) else ResourceSet(resources)
        context = f"Agent {definition.agent_id!r}"
        stored_workspace = await target_store.get_workspace_record(session_id)
        if (
            stored_workspace is not None
            and self._workspaces is not None
            and str(target_store.path) != str(self._store.path)
        ):
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "Managed workspace records live in the session manager's store; load this "
                "session through the manager's own store.",
                details={"session_id": session_id},
            )
        if summary.workspace_managed and stored_workspace is None:
            raise HarnessError(
                "WORKSPACE_UNAVAILABLE",
                f"Session {session_id!r} was created with a managed workspace, but its saved "
                "workspace identity is missing; refusing to substitute a local workspace.",
                details={"session_id": session_id, "reason": "workspace_identity_missing"},
            )
        if stored_workspace is not None and self._workspaces is None:
            raise HarnessError(
                "WORKSPACE_UNAVAILABLE",
                f"Session {session_id!r} has a managed workspace, but this host has no workspace "
                "manager configured; refusing to run in a substituted workspace.",
                details={
                    "session_id": session_id,
                    "provider": stored_workspace.provider,
                    "reason": "workspace_manager_missing",
                },
            )
        workspace_requirements = self._workspace_requirements(definition)
        other_requirements = [req for req in definition.resources if req.kind != "workspace"]
        resource_set.validate_requirements(other_requirements, context=context)
        managed_workspace = (
            self._workspaces is not None
            and bool(workspace_requirements)
            and stored_workspace is not None
        )
        if summary.workspace_managed and not managed_workspace:
            raise HarnessError(
                "WORKSPACE_UNAVAILABLE",
                f"Session {session_id!r} was created with a managed workspace, but this host "
                "cannot acquire it; refusing to substitute a local workspace.",
                details={"session_id": session_id, "reason": "workspace_manager_missing"},
            )
        if not managed_workspace:
            resource_set.validate_requirements(workspace_requirements, context=context)
        owner_token = uuid.uuid4().hex
        if not await target_store.acquire_control(session_id, principal, owner_token):
            raise HarnessError(
                "SESSION_BUSY",
                f"Session {session_id!r} is controlled by another worker.",
                details={"session_id": session_id},
            )
        try:
            # Exclusive control means any in-progress configuration claim belongs to a
            # worker that died before it could commit. Let the client retry the same
            # request id instead of returning REVISION_CONFLICT forever.
            await target_store.fail_in_progress_transactions(session_id)
            latest = await target_store.latest_revision(session_id)
            if latest is None:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Session {session_id!r} has no configuration revision.",
                    details={"session_id": session_id},
                )
            catalog = PolicyCatalog(
                definition,
                self.registry,
                session_id=session_id,
                principal=principal,
                deployment_id=summary.deployment_id,
                # Deployment defaults may have changed since creation; the persisted
                # selected revision is what must be validated on load.
                validate_defaults=False,
            )
            catalog.restore_bindings(latest.bindings)
            catalog.policy_revision = latest.policy_revision
            workspace_lease: WorkspaceLease | None = None
            if managed_workspace:
                workspace_lease = await self._managed_workspace_lease(
                    session_id, definition, stored=stored_workspace
                )
                if workspace_lease is not None:
                    resource_set = _bind_workspace_lease(
                        resource_set, definition, workspace_lease
                    )
                resource_set.validate_requirements(definition.resources, context=context)
            interrupted = await target_store.mark_turn_interrupted(session_id)
            for turn_id in interrupted:
                await target_store.append_transcript(
                    session_id,
                    turn_id,
                    "turn_interrupted",
                    {"code": "TURN_INTERRUPTED", "message": "The host stopped while this turn was running."},
                )
            history = _load_messages(await target_store.load_messages_json(session_id))
            turns = await target_store.list_turns(session_id)
            total_usage = RunUsage()
            for turn in turns:
                if turn.usage:
                    total_usage.incr(_usage_from_payload(turn.usage))
            session = Session(
                manager=self,
                definition=definition,
                catalog=catalog,
                store=target_store,
                owns_store=store is not None and not isinstance(store, SessionStore),
                resources=resource_set,
                principal=principal,
                deployment_id=summary.deployment_id,
                session_id=session_id,
                selected_revision=latest.revision,
                owner_token=owner_token,
                history=history,
                total_usage=total_usage,
                workspace_lease=workspace_lease,
            )
        except BaseException:
            await target_store.release_control(session_id, owner_token)
            if store is not None and not isinstance(store, SessionStore):
                await target_store.close()
            raise
        self._live[session_id] = session
        return session

    # --- listing / deletion ------------------------------------------

    async def list_sessions(
        self, principal: str, *, limit: int | None = 100
    ) -> list[SessionSummary]:
        return await self._store.list_sessions(principal, limit=limit)

    async def delete_session(self, principal: str, session_id: str) -> None:
        summary = await self._store.get_session(session_id)
        if summary is None or summary.principal != principal:
            raise HarnessError(
                "POLICY_DENIED",
                f"Session {session_id!r} is not available to this principal.",
                details={"session_id": session_id, "reason": "ownership"},
            )
        live = self._live.get(session_id)
        if live is not None:
            await live.close(cancel_running=True)
        workspace_record = await self._store.get_workspace_record(session_id)
        if workspace_record is not None or (
            self._workspaces is not None and self._workspaces.live(session_id) is not None
        ):
            if self._workspaces is None:
                raise HarnessError(
                    "WORKSPACE_DELETE_UNSUPPORTED",
                    f"Session {session_id!r} owns a managed workspace, but no workspace manager is "
                    "configured to delete it; the session and its workspace identity are retained.",
                    details={"session_id": session_id, "provider": getattr(workspace_record, "provider", None)},
                )
            # Destroy the environment and its retained identity before forgetting the session.
            await self._workspaces.delete(session_id)
        await self._store.delete_session(session_id)

    async def close(self) -> None:
        """Close every live session and the manager-owned store."""
        for session in list(self._live.values()):
            await session.close()
        if self._owns_store:
            await self._store.close()

    def _forget(self, session: Session) -> None:
        if self._live.get(session.session_id) is session:
            del self._live[session.session_id]


HarnessRuntime = SessionManager
"""Alias: a runtime host is a session manager over a plugin registry and store."""


class Session:
    """One conversation with next-turn capability composition."""

    def __init__(
        self,
        *,
        manager: SessionManager,
        definition: AgentDefinition,
        catalog: PolicyCatalog,
        store: SessionStore,
        owns_store: bool,
        resources: ResourceSet,
        principal: str,
        deployment_id: str,
        session_id: str,
        selected_revision: int,
        owner_token: str,
        history: list[Any] | None = None,
        total_usage: RunUsage | None = None,
        workspace_lease: WorkspaceLease | None = None,
    ) -> None:
        self._manager = manager
        self._definition = definition
        self._catalog = catalog
        self._store = store
        self._owns_store = owns_store
        self._resources = resources
        self._principal = principal
        self._deployment_id = deployment_id
        self._session_id = session_id
        self._selected_revision = selected_revision
        self._owner_token = owner_token
        self._history: list[Any] = list(history or [])
        self._conversation_id: str | None = _conversation_from_history(self._history)
        self._total_usage: RunUsage = total_usage or RunUsage()
        self._turn_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._active_turn: ActiveTurn | None = None
        self._turn_task: asyncio.Task[Any] | None = None
        self._prepare_failed = False
        self._closed = False
        self._closing = False
        self._close_requested = False
        self._open_text: dict[tuple[str, int | None, str], int] = {}
        self._last_turn_error: BaseException | None = None
        self._model_override: Any = None
        self._workspace_lease = workspace_lease
        self._jobs = SessionJobManager()

    # --- identity -----------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def principal(self) -> str:
        return self._principal

    @property
    def deployment_id(self) -> str:
        return self._deployment_id

    @property
    def agent_id(self) -> str:
        return self._definition.agent_id

    @property
    def definition_version(self) -> str:
        return self._definition.definition_version

    @property
    def definition(self) -> AgentDefinition:
        """Read-only snapshot of the session's pinned definition revision."""
        return _snapshot_definition(self._definition)

    @property
    def catalog(self) -> PolicyCatalog:
        """The session's live policy catalog (descriptor listing for ACP adapters)."""
        return self._catalog

    @property
    def workspace_lease(self) -> WorkspaceLease | None:
        """The managed workspace lease bound this session, when one was acquired."""
        return self._workspace_lease

    async def workspace_record(self) -> WorkspaceRecord | None:
        """Durable workspace identity for this session, when one is retained."""
        if self._manager.workspaces is None:
            return await self._store.get_workspace_record(self._session_id)
        return await self._manager.workspaces.record(self._session_id)

    def _require_binding(self, binding_id: str) -> None:
        if binding_id not in self.snapshot.bindings:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Binding {binding_id!r} is not offered by this session.",
                details={"binding_id": binding_id},
            )

    async def get_binding_state(
        self, binding_id: str, *, schema_version: int | None = None
    ) -> dict[str, Any] | None:
        """Retained, non-secret state for a capability binding.

        State survives disable/enable. It is never an authorization decision and is
        never deleted by a toggle; use :meth:`clear_binding_state` explicitly.
        """
        self._require_binding(binding_id)
        return await self._store.get_plugin_state(
            self._session_id, binding_id, schema_version=schema_version
        )

    async def set_binding_state(
        self, binding_id: str, state: Mapping[str, Any], *, schema_version: int = 1
    ) -> None:
        """Persist capability state; a schema change is rejected as incompatible."""
        self._require_binding(binding_id)
        await self._store.set_plugin_state(
            self._session_id, binding_id, schema_version=schema_version, state=state
        )

    async def clear_binding_state(self, binding_id: str) -> None:
        """Explicitly forget a binding's retained state (never done by a toggle)."""
        self._require_binding(binding_id)
        await self._store.delete_plugin_state(self._session_id, binding_id)

    @property
    def jobs(self) -> SessionJobManager:
        """Explicitly registered work that outlives a turn (see concept §8.4)."""
        return self._jobs

    def register_job(
        self,
        binding_id: str,
        awaitable: Any,
        *,
        description: str = "",
        name: str | None = None,
    ) -> OwnedJob:
        """Register background work owned by an enabled managed binding."""
        state = self.snapshot.bindings.get(binding_id)
        if state is None or not state.enabled:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Binding {binding_id!r} is not enabled in this session.",
                details={"binding_id": binding_id},
            )
        return self._jobs.register(
            binding_id,
            self._selected_revision,
            awaitable,
            description=description,
            name=name,
            context=_detached_run_context(),
        )

    def register_external_job(
        self,
        binding_id: str,
        *,
        cancel: Callable[[], Any] | None = None,
        description: str = "",
    ) -> OwnedJob:
        """Track external work; it stays busy until cancelled or explicitly completed."""
        state = self.snapshot.bindings.get(binding_id)
        if state is None or not state.enabled:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Binding {binding_id!r} is not enabled in this session.",
                details={"binding_id": binding_id},
            )
        return self._jobs.register_external(
            binding_id,
            self._selected_revision,
            cancel=cancel,
            description=description,
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_turn_error(self) -> BaseException | None:
        """The last native turn failure, for diagnostics after a stream error event."""
        return self._last_turn_error

    @property
    def usage(self) -> RunUsage:
        """Cumulative usage across the session's turns."""
        copy = RunUsage()
        copy.incr(self._total_usage)
        return copy

    @property
    def snapshot(self) -> ConfigurationSnapshot:
        """Selected configuration plus active revision and application status."""
        selected = self._selected_revision
        active = self._active_turn.revision if self._active_turn is not None else selected
        if self._prepare_failed:
            status = "failed"
        elif self._active_turn is not None and active != selected:
            status = "pending"
        else:
            status = "applied"
        return self._catalog.snapshot(
            selected_revision=selected,
            active_revision=active,
            application_status=status,
        )

    async def history(self) -> list[Any]:
        """Native model message history (read-only view)."""
        return list(self._history)

    async def transcript(self, turn_id: str | None = None) -> list[dict[str, Any]]:
        """Persisted renderable transcript events."""
        return await self._store.load_transcript(self._session_id, turn_id=turn_id)

    # --- listing ------------------------------------------------------

    async def capabilities(self, cursor: str | None = None, limit: int = 50) -> tuple[list[Any], str | None]:
        """Paginated binding descriptors (the extension's capabilities list)."""
        descriptors = self._catalog.descriptors()
        return paginate(descriptors, cursor, limit)

    async def plugins(self, cursor: str | None = None, limit: int = 50) -> tuple[list[Any], str | None]:
        """Paginated registered plugins, when this host advertises local binding creation."""
        if not self._manager.registry.allow_local_binding_creation:
            raise HarnessError(
                "POLICY_DENIED",
                "This host does not advertise local binding creation.",
                details={"session_id": self._session_id},
            )
        return paginate(self._manager.registry.catalog(), cursor, limit)

    # --- configuration ------------------------------------------------

    async def update_configuration(
        self,
        changes: Sequence[BindingChange],
        request_id: str | int | None = None,
        expected_revision: int | str | None = None,
    ) -> ConfigurationSnapshot:
        """Atomically validate and commit a selected-configuration transaction.

        Accepts both ``(changes, request_id, expected_revision)`` and the host adapter's
        historical positional order ``(changes, expected_revision, request_id)``.
        """
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            request_id, expected_revision = (
                expected_revision if isinstance(expected_revision, str) else None,
                request_id,
            )
        elif expected_revision is not None and not isinstance(expected_revision, int):
            if isinstance(request_id, str) and isinstance(expected_revision, str):
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    "expected_revision must be an integer.",
                    details={"expected_revision": expected_revision},
                )
        if not isinstance(request_id, str) or not request_id:
            raise HarnessError("INVALID_CONFIGURATION", "request_id must be a non-empty string.")
        if expected_revision is not None and not isinstance(expected_revision, int):
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "expected_revision must be an integer.",
                details={"expected_revision": expected_revision},
            )
        if self._closed or self._closing:
            raise HarnessError(
                "SESSION_CLOSED",
                f"Session {self._session_id!r} is closed.",
                details={"session_id": self._session_id},
            )
        normalized = _normalize_changes(changes)
        payload_hash = configuration_payload_hash(
            self._session_id, request_id, expected_revision, normalized
        )
        # Consult retry identity before checking the expected revision: a completed retry with
        # the original expectedRevision must return the committed result (CONCEPT §10.3).
        claim = await self._store.claim_transaction(self._session_id, request_id, payload_hash)
        if claim.status == "committed" and claim.snapshot is not None:
            return ConfigurationSnapshot.model_validate(claim.snapshot)
        try:
            async with self._state_lock:
                if expected_revision is not None and expected_revision != self._selected_revision:
                    raise HarnessError(
                        "REVISION_CONFLICT",
                        "The configuration changed since the client's expected revision.",
                        details={"snapshot": self.snapshot.to_wire(), "expected_revision": expected_revision},
                    )
                outcome = self._catalog.apply_changes(normalized)
                # Only a validated disable may stop owned work. Same-ID retries
                # returned above, so a replay never cancels work a second time.
                current = self.snapshot.bindings
                disabled = {
                    change.binding_id
                    for change in normalized
                    if isinstance(change, EnableChange)
                    and not change.enabled
                    and current.get(change.binding_id) is not None
                    and current[change.binding_id].enabled
                }
                # While a turn is running, configuration changes are not cancellation:
                # owned jobs are drained at the next turn boundary instead.
                if self._active_turn is None:
                    for binding_id in sorted(disabled):
                        if self._jobs.has_active(binding_id) and not await self._jobs.cancel_binding(binding_id):
                            raise HarnessError(
                                "CAPABILITY_BUSY",
                                f"Binding {binding_id!r} still owns background work that could not be stopped.",
                                details={"binding_id": binding_id, "jobs": self._jobs.snapshot()},
                            )
                active_revision = (
                    self._active_turn.revision if self._active_turn is not None else None
                )
                if outcome.noop:
                    snapshot = self.snapshot
                    await self._store.commit_transaction(
                        self._session_id,
                        request_id,
                        bindings=None,
                        policy_revision=self._catalog.policy_revision,
                        snapshot_builder=lambda revision: snapshot.to_wire(),
                        create_revision=False,
                    )
                    return snapshot
                builder_snapshot: dict[str, Any] = {}

                def build(revision: int) -> dict[str, Any]:
                    built = self._snapshot_from_states(
                        outcome.states,
                        revision=revision,
                        active_revision=active_revision,
                    )
                    builder_snapshot["snapshot"] = built.to_wire()
                    return builder_snapshot["snapshot"]

                stored = await self._store.commit_transaction(
                    self._session_id,
                    request_id,
                    bindings=_bindings_payload(outcome.states, outcome.added),
                    policy_revision=self._catalog.policy_revision,
                    snapshot_builder=build,
                    create_revision=True,
                )
                self._catalog.commit(outcome)
                self._selected_revision = stored.revision
                if self._active_turn is None:
                    self._catalog.set_active_bindings(None)
                    await self._store.mark_revision_applied(self._session_id, stored.revision)
                return ConfigurationSnapshot.model_validate(builder_snapshot["snapshot"])
        except HarnessError:
            await self._store.fail_transaction(self._session_id, request_id)
            raise
        except BaseException:
            await self._store.fail_transaction(self._session_id, request_id)
            raise

    def _snapshot_from_states(
        self,
        states: Mapping[str, tuple[bool, Mapping[str, Any]]],
        *,
        revision: int,
        active_revision: int | None,
    ) -> ConfigurationSnapshot:
        from harness.protocol import BindingState

        bindings = {
            binding_id: BindingState(enabled=enabled, config=jsonable_config(config))
            for binding_id, (enabled, config) in states.items()
        }
        if active_revision is None or active_revision == revision:
            status = "applied"
        else:
            status = "pending"
        return ConfigurationSnapshot(
            session_id=self._session_id,
            selected_revision=revision,
            active_revision=active_revision if active_revision is not None else revision,
            policy_revision=self._catalog.policy_revision,
            application_status=status,
            bindings=bindings,
        )

    # --- turns --------------------------------------------------------

    async def run(
        self,
        prompt: Any,
        *,
        output_type: Any = None,
        deps: Any = None,
        model: Any = None,
        model_settings: Any = None,
        instructions: Any = None,
        retries: Any = None,
        usage_limits: Any = None,
        deferred_tool_results: Any = None,
        turn_id: str | None = None,
    ) -> Any:
        """Run one turn and return the native ``AgentRunResult`` (structured outputs preserved)."""
        options = TurnOptions(
            output_type=output_type,
            deps=deps,
            model=model,
            model_settings=model_settings,
            instructions=instructions,
            retries=retries,
            usage_limits=usage_limits,
            deferred_tool_results=deferred_tool_results,
            turn_id=turn_id,
        )
        result = await self._execute_turn(prompt, options, emit=None, raise_errors=True)
        return result

    async def stream(
        self,
        prompt: Any,
        *,
        output_type: Any = None,
        deps: Any = None,
        model: Any = None,
        model_settings: Any = None,
        instructions: Any = None,
        retries: Any = None,
        usage_limits: Any = None,
        deferred_tool_results: Any = None,
        turn_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Stream one turn's runtime events, ending with ``turn_complete`` or ``error``."""
        options = TurnOptions(
            output_type=output_type,
            deps=deps,
            model=model,
            model_settings=model_settings,
            instructions=instructions,
            retries=retries,
            usage_limits=usage_limits,
            deferred_tool_results=deferred_tool_results,
            turn_id=turn_id,
        )
        queue: asyncio.Queue[Any] = asyncio.Queue()
        task = asyncio.create_task(
            self._execute_turn(prompt, options, emit=queue.put_nowait, raise_errors=False)
        )
        self._turn_task = task
        task.add_done_callback(_consume_task_exception)
        task.add_done_callback(self._clear_turn_task)
        completed = False
        try:
            while True:
                event = await queue.get()
                if event is _END:
                    completed = True
                    break
                yield event
        finally:
            if not completed:
                await self.cancel()

    async def cancel(self) -> bool:
        """Request cancellation of the active turn; returns whether a turn was active."""
        active = self._active_turn
        if active is None:
            return False
        try:
            active.cancel_token.cancel()
        finally:
            if active.events is not None:
                try:
                    active.events.cancel()
                except Exception:  # pragma: no cover - teardown best effort
                    pass
        return True

    async def set_model(
        self,
        model: str,
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> ConfigurationSnapshot:
        """Select the model used by subsequent turns (ACP model option).

        This is a session preference, not a capability transaction: it applies from the next
        turn and does not create a configuration revision. ``expected_revision`` guards
        against a stale client applying a model after it observed another revision.
        """
        del request_id
        if self._closed or self._closing:
            raise HarnessError(
                "SESSION_CLOSED",
                f"Session {self._session_id!r} is closed.",
                details={"session_id": self._session_id},
            )
        if isinstance(model, str) and not model:
            raise HarnessError("INVALID_CONFIGURATION", "model must be a non-empty model id.")
        if model is None:
            raise HarnessError("INVALID_CONFIGURATION", "model must not be None.")
        if expected_revision is not None and expected_revision != self._selected_revision:
            raise HarnessError(
                "REVISION_CONFLICT",
                "The configuration changed since the client's expected revision.",
                details={"snapshot": self.snapshot.to_wire(), "expected_revision": expected_revision},
            )
        self._model_override = model
        return self.snapshot

    def _clear_turn_task(self, task: asyncio.Task[Any]) -> None:
        if self._turn_task is task:
            self._turn_task = None

    async def close(self, *, cancel_running: bool = True) -> None:
        """Cancel/drain active work, release ownership, and retain conversation history."""
        if self._closed:
            return
        self._closing = True
        self._close_requested = True
        try:
            if self._turn_lock.locked():
                if cancel_running:
                    await self.cancel()
                task = self._turn_task
                if task is not None:
                    try:
                        await asyncio.shield(task)
                    except BaseException:  # pragma: no cover - task never raises on stream path
                        pass
                else:
                    async with self._turn_lock:
                        pass
            # Cancel and drain explicitly registered session work. External work that
            # cannot be confirmed stopped remains recorded in `jobs.snapshot()`.
            await self._jobs.close(timeout=10.0)
            if self._workspace_lease is not None and self._manager.workspaces is not None:
                # Release/stop the live handle; the identity record is retained for reload.
                await self._manager.workspaces.close(self._session_id)
                self._workspace_lease = None
            self._closed = True
            await self._store.close_session(self._session_id)
            await self._store.release_control(self._session_id, self._owner_token)
            self._manager._forget(self)
        finally:
            self._closing = False
            if self._owns_store:
                await self._store.close()

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    # --- turn machinery ----------------------------------------------

    async def _execute_turn(
        self,
        prompt: Any,
        options: TurnOptions,
        *,
        emit: Callable[[Any], None] | None,
        raise_errors: bool,
    ) -> Any:
        try:
            return await self._turn_impl(prompt, options, emit)
        except BaseException as exc:
            self._last_turn_error = exc
            if raise_errors:
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            if emit is not None:
                emit(RuntimeEvent(type="error", data=_error_event_payload(exc)))
            return None
        finally:
            if emit is not None:
                emit(_END)

    async def _turn_impl(
        self,
        prompt: Any,
        options: TurnOptions,
        emit: Callable[[Any], None] | None,
    ) -> Any:
        if self._closed or self._closing:
            raise HarnessError(
                "SESSION_CLOSED",
                f"Session {self._session_id!r} is closed.",
                details={"session_id": self._session_id},
            )
        if self._turn_lock.locked():
            raise HarnessError(
                "SESSION_BUSY",
                f"Session {self._session_id!r} already has an active turn.",
                details={"session_id": self._session_id},
            )
        await self._turn_lock.acquire()
        turn_created = False
        finalized = False
        turn_id = options.turn_id or _new_turn_id()
        try:
            if self._closed or self._closing:
                raise HarnessError(
                    "SESSION_CLOSED",
                    f"Session {self._session_id!r} is closed.",
                    details={"session_id": self._session_id},
                )
            selected_bindings = self.snapshot.bindings
            busy_disabled = {
                binding_id
                for binding_id, state in selected_bindings.items()
                if not state.enabled
            } & self._jobs.busy_bindings()
            # Work left over from a disable requested during the previous turn is
            # stopped here, before acquiring the new composition.
            for binding_id in sorted(busy_disabled):
                if not await self._jobs.cancel_binding(binding_id):
                    raise HarnessError(
                        "CAPABILITY_BUSY",
                        "Background work still owns a disabled binding; the next turn cannot start.",
                        details={"binding_ids": sorted(busy_disabled), "jobs": self._jobs.snapshot()},
                    )
            active_revision = self._selected_revision
            async with self._state_lock:
                captured = self._catalog.snapshot(
                    selected_revision=active_revision,
                    active_revision=active_revision,
                    application_status="applied",
                )
                self._open_text = {}
                try:
                    composed = self._catalog.compose(
                        resources=self._resources,
                        active_configuration=captured,
                        agent_context=self._factory_context(),
                    )
                    agent, run_capabilities, model_handled = await self._build_agent(
                        composed, captured, options
                    )
                except HarnessError:
                    self._prepare_failed = True
                    raise
                except Exception as exc:
                    self._prepare_failed = True
                    raise HarnessError(
                        "CAPABILITY_UNAVAILABLE",
                        f"Failed to prepare the selected composition for definition "
                        f"{self._definition.agent_id!r}: {exc}",
                        details={
                            "session_id": self._session_id,
                            "revision": active_revision,
                            "exception": type(exc).__name__,
                        },
                    ) from exc
                self._prepare_failed = False
                active_view = {
                    binding_id: {"enabled": state.enabled, "config": state.config}
                    for binding_id, state in captured.bindings.items()
                }
                self._catalog.set_active_bindings(active_view)
            token = CancellationToken()
            active_turn = ActiveTurn(
                turn_id=turn_id,
                revision=active_revision,
                cancel_token=token,
                bindings=active_view,
            )
            self._active_turn = active_turn
            await self._store.create_turn(
                self._session_id,
                turn_id,
                active_revision=active_revision,
                prompt=_prompt_payload(prompt),
            )
            turn_created = True
            if prompt is not None:
                # ACP session/load must replay the complete conversation, including
                # the user's prompts, to a fresh client. Approval continuations pass
                # prompt=None and are not new user messages.
                await self._store.append_transcript(
                    self._session_id,
                    turn_id,
                    "user_message",
                    {"prompt": _prompt_payload(prompt)},
                )
            if self._close_requested or self._closing:
                # close() raced the composition window before ActiveTurn existed.
                token.cancel()
            await self._store.mark_revision_applied(self._session_id, active_revision)
            await self._store.touch_session(self._session_id)
            kwargs = self._run_kwargs(
                prompt, options, active_turn, run_capabilities, model_handled=model_handled
            )
            try:
                result = await self._drive_scoped(agent, kwargs, turn_id, emit)
            except (RunCancelled, asyncio.CancelledError) as exc:
                if isinstance(exc, RunCancelled):
                    messages = self._cancelled_new_messages(exc)
                else:
                    messages = self._partial_new_messages()
                self._history.extend(messages)
                usage = self._current_usage()
                await self._finalize_turn(
                    turn_id,
                    "cancelled",
                    messages=messages,
                    usage=usage,
                    error={
                        "code": "TURN_CANCELLED",
                        "message": str(exc) or "cancelled",
                        "details": {"cancelled": True},
                    },
                )
                finalized = True
                self._total_usage.incr(usage)
                raise
            except BaseException as exc:
                messages = self._partial_new_messages()
                self._history.extend(messages)
                usage = self._current_usage()
                error = _error_event_payload(exc, turn_id=turn_id)
                await self._finalize_turn(
                    turn_id,
                    "failed",
                    messages=messages,
                    usage=usage,
                    error=error,
                )
                finalized = True
                self._total_usage.incr(usage)
                raise
            usage = result.usage
            new_messages = list(result.new_messages())
            self._history.extend(new_messages)
            if result.conversation_id:
                self._conversation_id = result.conversation_id
            output, output_type = output_payload(result.output)
            deferred = None
            if _is_deferred_requests(result.output):
                deferred = deferred_requests_payload(result.output)
            await self._finalize_turn(
                turn_id,
                "completed",
                messages=new_messages,
                output=output,
                usage=usage,
                deferred=deferred,
            )
            finalized = True
            self._total_usage.incr(usage)
            if emit is not None:
                emit(RuntimeEvent(type="usage", data=usage_payload(usage)))
                completion: dict[str, Any] = {
                    "turn_id": turn_id,
                    "run_id": result.run_id,
                    "conversation_id": result.conversation_id,
                    "active_revision": active_revision,
                    "output": output,
                    "output_type": output_type,
                    "usage": usage_payload(usage),
                }
                if deferred is not None:
                    completion["deferred_tool_requests"] = deferred
                emit(RuntimeEvent(type="turn_complete", data=completion))
            return result
        finally:
            if turn_created and not finalized:
                try:
                    await self._finalize_turn(
                        turn_id,
                        "failed",
                        messages=self._partial_new_messages(),
                        usage=self._current_usage(),
                        error={"code": "TURN_FAILED", "message": "The turn ended without a recorded outcome."},
                    )
                except Exception:  # pragma: no cover - persistence best effort during teardown
                    pass
            self._active_turn = None
            try:
                self._catalog.set_active_bindings(None)
            finally:
                self._turn_lock.release()

    async def _drive_scoped(
        self,
        agent: Any,
        kwargs: dict[str, Any],
        turn_id: str,
        emit: Callable[[Any], None] | None,
    ) -> Any:
        """Run the agent and cancel/drain tasks created in this run's context.

        Child tasks created by capability hooks or tools inherit a context marker. Tasks
        created before the run (or by another session) never match, and explicit session
        jobs are launched with the marker cleared because they intentionally outlive the
        turn (CONCEPT §8.4).
        """
        scope = object()
        token = _RUN_TASK_SCOPE.set(scope)
        baseline = set(asyncio.all_tasks())
        try:
            return await self._drive(agent, kwargs, turn_id, emit)
        finally:
            try:
                _RUN_TASK_SCOPE.reset(token)
            finally:
                await self._drain_run_scoped(scope, baseline)

    async def _drain_run_scoped(
        self, scope: object, baseline: set[asyncio.Task[Any]]
    ) -> None:
        current = asyncio.current_task()
        owned = self._jobs.tasks()
        children: list[asyncio.Task[Any]] = []
        for task in asyncio.all_tasks():
            if task is current or task in baseline or task in owned or task.done():
                continue
            context = _task_context(task)
            if context is None or context.get(_RUN_TASK_SCOPE) is not scope:
                continue
            children.append(task)
        if not children:
            return
        for task in children:
            task.cancel()
        _, pending = await asyncio.wait(children, timeout=5.0)
        for task in pending:
            # A child that suppresses cancellation is reported by its own capability;
            # the runtime must not claim the run is still active.
            task.cancel()

    async def _drive(
        self,
        agent: Any,
        kwargs: dict[str, Any],
        turn_id: str,
        emit: Callable[[Any], None] | None,
    ) -> Any:
        final: Any = None
        translation = TranslationState()
        async with agent.run_stream_events(**kwargs) as events:
            if self._active_turn is not None:
                self._active_turn.events = events
            async for event in events:
                if isinstance(event, AgentRunResultEvent):
                    final = event.result
                    continue
                for runtime_event in translate_event(event, translation):
                    await self._journal(runtime_event, turn_id)
                    if emit is not None:
                        emit(runtime_event)
            if final is None:
                final = events.result
        if final is None:  # pragma: no cover - a completed run always yields a result
            raise HarnessError("TURN_FAILED", "The agent run completed without a result.")
        return final

    def _run_kwargs(
        self,
        prompt: Any,
        options: TurnOptions,
        active_turn: ActiveTurn,
        run_capabilities: list[Any] | None,
        *,
        model_handled: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "user_prompt": _coerce_prompt(prompt),
            "run_id": active_turn.turn_id,
            "cancellation_token": active_turn.cancel_token,
        }
        if self._history:
            kwargs["message_history"] = list(self._history)
        if self._conversation_id:
            kwargs["conversation_id"] = self._conversation_id
        if options.output_type is not None:
            kwargs["output_type"] = options.output_type
        if options.deps is not None:
            kwargs["deps"] = options.deps
        if not model_handled:
            if options.model is not None:
                kwargs["model"] = options.model
            elif self._model_override is not None:
                kwargs["model"] = self._model_override
        if options.model_settings is not None:
            kwargs["model_settings"] = options.model_settings
        if options.instructions is not None:
            kwargs["instructions"] = options.instructions
        if options.retries is not None:
            kwargs["retries"] = options.retries
        if options.usage_limits is not None:
            kwargs["usage_limits"] = options.usage_limits
        if options.deferred_tool_results is not None:
            kwargs["deferred_tool_results"] = options.deferred_tool_results
        if run_capabilities:
            kwargs["capabilities"] = run_capabilities
        workspace = self._workspace_value()
        if workspace is not _MISSING:
            kwargs["workspace"] = workspace
        return kwargs

    # --- agent composition -------------------------------------------

    def _factory_context(self) -> AgentFactoryContext:
        return AgentFactoryContext(
            session_id=self._session_id,
            principal=self._principal,
            deployment_id=self._deployment_id,
            binding_ids=tuple(
                offering.binding_id for offering in self._catalog.enabled_ordered()
            ),
        )

    async def _build_agent(
        self,
        composed: list[tuple[str, Any]],
        captured: ConfigurationSnapshot,
        options: TurnOptions,
    ) -> tuple[Any, list[Any] | None, bool]:
        """Build this turn's agent: ``(agent, run_capabilities, model_handled)``.

        ``model_handled`` is true when the model selection was applied while constructing the
        agent, so it must not be applied a second time as a per-run override (a factory may
        interpret model ids itself rather than exposing them to Pydantic AI).
        """
        capabilities = [capability for _, capability in composed]
        _validate_composed_identities(self._definition, composed)
        # Precedence: per-run override > session selection > deployment resource binding >
        # definition fallback (plugin defaults sit beneath definition values in policy).
        model = (
            options.model
            or self._model_override
            or self._resources.get_model()
            or self._definition.model
        )
        definition = self._definition
        if definition.spec is not None:
            spec: Any = definition.spec
            if isinstance(spec, BaseModel):
                spec = spec.model_dump(mode="json", exclude_defaults=True)
            agent = Agent.from_spec(
                spec,
                model=model,
                capabilities=[*definition.fixed_capabilities, *capabilities],
            )
            return agent, None, True
        if definition.factory is not None:
            factory = definition.factory
            managed_at_agent = factory_managed_param(factory)
            factory_caps = capabilities if managed_at_agent else None
            kwargs = self._factory_kwargs(factory, captured, composed, factory_caps, model)
            agent = factory(**kwargs)
            if inspect.isawaitable(agent):
                agent = await agent
            if not hasattr(agent, "run_stream_events") or not hasattr(agent, "run"):
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Definition factory for {definition.agent_id!r} did not return a native agent.",
                    details={"returned": type(agent).__name__},
                )
            model_handled = _factory_declares_model(factory)
            # A Python factory may embed private fixed behavior; the definition's
            # ``fixed_capabilities`` are still added by the framework every turn.
            # Managed bindings are bound at construction only when the factory asks
            # for them (self-delegating capabilities need that native placement).
            fixed = list(definition.fixed_capabilities)
            if managed_at_agent:
                return agent, (fixed or None), model_handled
            return agent, ([*fixed, *capabilities] or None), model_handled
        agent = Agent(
            model=model,
            capabilities=[*definition.fixed_capabilities, *capabilities],
        )
        return agent, None, True

    def _factory_kwargs(
        self,
        factory: Callable[..., Any],
        captured: ConfigurationSnapshot,
        composed: list[tuple[str, Any]],
        managed_capabilities: list[Any] | None,
        model: Any,
    ) -> dict[str, Any]:
        declared = set(factory_declared_params(factory))
        try:
            parameters = inspect.signature(factory).parameters.values()
            accepts_var_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_var_kwargs = True
        values: dict[str, Any] = {
            "resources": self._resources,
            "active_config": captured,
            "model": model,
            "managed_capabilities": managed_capabilities,
            "context": AgentFactoryContext(
                session_id=self._session_id,
                principal=self._principal,
                deployment_id=self._deployment_id,
                binding_ids=tuple(binding_id for binding_id, _ in composed),
            ),
            "principal": self._principal,
            "deployment_id": self._deployment_id,
        }
        kwargs: dict[str, Any] = {}
        for name, value in values.items():
            if accepts_var_kwargs or name in declared:
                kwargs[name] = value
        return kwargs

    def _workspace_value(self) -> Any:
        for requirement in self._definition.resources:
            if requirement.kind != "workspace":
                continue
            found, value = self._resources.try_resolve(requirement)
            if not found:
                return _MISSING
            return resolve_workspace_value(value)
        return _MISSING

    # --- journaling and outcomes -------------------------------------

    async def _journal(self, event: RuntimeEvent, turn_id: str) -> None:
        if event.type == "text_delta":
            kind = str(event.data.get("kind", "text"))
            part_index = event.data.get("part_index")
            key = (turn_id, part_index if isinstance(part_index, int) else None, kind)
            # A new part start begins a new model response/part even when providers reuse
            # index 0; appending to an earlier row would reorder the transcript.
            seq = None if event.data.get("part_start") else self._open_text.get(key)
            text = str(event.data.get("text", ""))
            if seq is None:
                seq = await self._store.append_transcript(
                    self._session_id,
                    turn_id,
                    "thinking_delta" if kind == "thinking" else "text_delta",
                    {"text": text, "part_index": part_index, "kind": kind},
                )
                self._open_text[key] = seq
            else:
                await self._store.append_transcript_text(self._session_id, seq, text, turn_id)
            return
        if event.type in {
            "tool_call",
            "tool_update",
            "deferred_tool_requests",
            "custom",
            "capability",
            "error",
        }:
            # Close the current text block: assistant text after a tool event belongs to a
            # later model response and must not be coalesced into a row emitted before it.
            self._open_text.clear()
            await self._store.append_transcript(self._session_id, turn_id, event.type, event.data)

    async def _finalize_turn(
        self,
        turn_id: str,
        status: str,
        *,
        messages: Sequence[Any],
        output: Any = None,
        usage: RunUsage | None = None,
        error: Any = None,
        deferred: Any = None,
    ) -> None:
        await self._store.finalize_turn(
            self._session_id,
            turn_id,
            status=status,
            output=output,
            error=error,
            usage=usage_payload(usage) if usage is not None else None,
            messages=_messages_json(messages),
        )
        if error is not None:
            await self._store.append_transcript(self._session_id, turn_id, "error", error)
        del deferred  # already journaled as a live event by the drive loop

    def _partial_new_messages(self) -> list[Any]:
        active = self._active_turn
        if active is None or active.events is None:
            return []
        try:
            return list(active.events.new_messages())
        except Exception:  # pragma: no cover - handle not started
            return []

    def _cancelled_new_messages(self, exc: RunCancelled) -> list[Any]:
        try:
            all_messages = list(exc.all_messages())
        except Exception:  # pragma: no cover - legacy shape
            return self._partial_new_messages()
        return all_messages[len(self._history) :]

    def _current_usage(self) -> RunUsage:
        active = self._active_turn
        if active is not None and active.events is not None:
            try:
                return active.events.usage
            except Exception:  # pragma: no cover - run not started
                pass
        return RunUsage()


def _factory_declares_model(factory: Callable[..., Any]) -> bool:
    """Whether the factory consumes the ``model`` keyword (and therefore applied it)."""
    import inspect

    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return False
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return True
    return "model" in parameters


def _bindings_payload(
    states: Mapping[str, tuple[bool, Mapping[str, Any]]],
    added: Sequence[tuple[str, str, bool, Mapping[str, Any]]] = (),
) -> dict[str, dict[str, Any]]:
    plugin_ids = {binding_id: plugin_id for binding_id, plugin_id, _, _ in added}
    return {
        binding_id: {
            "enabled": enabled,
            "config": jsonable_config(config),
            **({"plugin_id": plugin_ids[binding_id]} if binding_id in plugin_ids else {}),
        }
        for binding_id, (enabled, config) in states.items()
    }


def _is_deferred_requests(value: Any) -> bool:
    try:
        from pydantic_ai.tools import DeferredToolRequests

        return isinstance(value, DeferredToolRequests)
    except Exception:  # pragma: no cover - import shape guard
        return False


def _conversation_from_history(history: Sequence[Any]) -> str | None:
    for message in reversed(history):
        conversation_id = getattr(message, "conversation_id", None)
        if conversation_id:
            return conversation_id
    return None


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):  # pragma: no cover - defensive
        return



def _cwd_from_resources(definition: AgentDefinition, resources: ResourceSet) -> str | None:
    """Record the session working directory for authoritative reloads."""
    for requirement in definition.resources:
        if requirement.kind != "workspace":
            continue
        found, value = resources.try_resolve(requirement)
        if found and isinstance(value, str | Path):
            return str(value)
    fallback = resources.get("workspace")
    return str(fallback) if isinstance(fallback, str | Path) else None


def _load_definition_source(source: str) -> AgentDefinition:
    """Resolve an agent definition from an import reference or a manifest file."""
    from .definitions import manifest_from_file

    path = Path(source).expanduser()
    if path.exists():
        return manifest_from_file(path).to_definition()
    if ":" not in source:
        raise HarnessError(
            "DEPLOYMENT_UNAVAILABLE",
            f"Agent definition {source!r} is neither an installed import reference "
            "(package.module:attribute) nor an existing manifest file.",
            details={"definition": source},
        )
    module_name, _, attribute = source.partition(":")
    try:
        import importlib

        module = importlib.import_module(module_name)
        target = getattr(module, attribute)
    except Exception as exc:
        raise HarnessError(
            "DEPLOYMENT_UNAVAILABLE",
            f"Failed to import agent definition {source!r}: {exc}",
            details={"definition": source},
        ) from exc
    value = target() if callable(target) and not isinstance(target, AgentDefinition) else target
    if isinstance(value, AgentDefinition):
        return value
    raise HarnessError(
        "DEPLOYMENT_UNAVAILABLE",
        f"Agent definition reference {source!r} did not resolve to an AgentDefinition.",
        details={"definition": source, "resolved": type(value).__name__},
    )


def build_host_bundle(setup: Any) -> Any:
    """Runtime-module entry point for ``harness-host --runtime-module harness.runtime.session``.

    Unlike the entry-points-only default bundle, this composes the bundled upstream catalog
    (lazy managed descriptors) plus any installed ``harness.capabilities`` entry points, so a
    manifest referencing catalog plugin ids resolves on a local host. Hosted mode never
    advertises local binding creation or discovery beyond the deployment catalog.
    """
    from .catalog import build_catalog_registry
    from .host import HostBundle, HostMetadataStore

    definition = _load_definition_source(setup.definition)
    hosted = bool(getattr(setup, "hosted", False))
    registry = build_catalog_registry(allow_local_binding_creation=not hosted)
    registry.discover()
    store_path = getattr(setup, "store_path", None)
    store = SessionStore(store_path) if store_path else SessionStore()
    manager = SessionManager(
        registry,
        store=store,
        definitions=[definition],
        allow_local_binding_creation=not hosted,
    )
    resolved_path = None if str(store.path) == ":memory:" else str(store.path)
    return HostBundle(
        definition=definition,
        registry=registry,
        manager=manager,
        store_path=resolved_path,
        meta=HostMetadataStore(resolved_path),
    )
