"""ACP hosting built on ``PydanticAIACPAgent``.

The upstream adapter owns ACP execution: prompts, streaming, approvals, cancellation,
conversation history, transcript replay, and session locks. This module only composes a
native agent per session, scopes persistence to the authenticated principal and registered
deployment, and adds the small host-owned metadata surface (list/delete/title/usage).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import uuid
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import acp as acp_sdk
from acp import schema
from acp.exceptions import RequestError
from acp.schema import DeleteSessionResponse
from pydantic import TypeAdapter, ValidationError
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.messages import (
    AgentStreamEvent,
    EnqueuedMessagesEvent,
    ModelRequest,
    UserPromptPart,
)
from pydantic_ai_harness.experimental import HarnessExperimentalWarning
from pydantic_ai_harness.experimental.acp import (
    AcpSession,
    PydanticAIACPAgent,
    StoredSession,
    chain_presenters,
    default_coding_presenter,
)

from hrns.config import EFFORT_DEFAULT
from hrns.protocol import (
    MAX_MESSAGE_BYTES,
    BindingListResponse,
    CommandCancelRequest,
    CommandInvokeRequest,
    CommandListRequest,
    CommandListResponse,
    ConfigurationUpdate,
    ExtensionAdvertisement,
    HrnsError,
    ListRequest,
    PluginListResponse,
    PresentationListRequest,
    PresentationListResponse,
    QueueEditRequest,
    QueueItem,
    QueueRemoveRequest,
    QueueSnapshot,
    QueueUpdate,
    SessionUpdatePosition,
    SessionUpdateProtocolError,
    SteerRequest,
    canonical_hash,
    maybe_session_update_position,
    order_session_updates,
    paginate,
    with_session_update_position,
)

from . import acp as acp_map
from .acp_store import (
    LIFECYCLE_PENDING_STATES,
    OperationKind,
    PromptState,
    QueuedPrompt,
    SessionLease,
    SessionRecord,
    finalize_usage,
    merge_usage,
    now_ms,
    usage_payload,
)
from .ask_user import QuestionChannel
from .capabilities.coder._acp import coder_tool_presenter
from .command_service import CommandService
from .configuration_service import ConfigurationService
from .creation import SessionCreationResult, SessionCreationState, SessionNewInput
from .creation_service import CreationLease, creation_lifecycle_context, recover_creation
from .fanout import FanoutClient
from .host_support import (
    HostBundle,
    HostPolicy,
    _ComposedAgent,
    _prompt_text,
)
from .lifecycle import LifecycleEvent, SessionLifecycleContext, run_hook
from .request_context import (
    RequestContext,
    current_connection_id,
    current_request,
)
from .session_policy import (
    READ_ONLY_ACTIONS,
    PolicyAction,
    SessionAccess,
    SessionContext,
    decide_policy,
)
from .session_service import SessionService

if TYPE_CHECKING:
    from pydantic_ai_harness.experimental.acp._adapter import _TurnState

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

__all__ = ["HrnsACPAgent"]

_PROMPT_BLOCKS = TypeAdapter(
    list[
        schema.TextContentBlock
        | schema.ImageContentBlock
        | schema.AudioContentBlock
        | schema.ResourceContentBlock
        | schema.EmbeddedResourceContentBlock
    ]
)

#: What the client on the current connection advertised during ``initialize``.
_current_capabilities: contextvars.ContextVar[schema.ClientCapabilities | None] = (
    contextvars.ContextVar("hrns_client_capabilities", default=None)
)

#: A per-task replay target for ``load_session``. Turn starts in other tasks keep the
#: actor-wide fan-out connection.
_conn_override: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "hrns_conn_override", default=None
)


class HrnsACPAgent(PydanticAIACPAgent[Any, str]):
    """ACP agent whose sessions are composed native Pydantic AI agents."""

    def __init__(
        self,
        bundle: HostBundle,
        policy: HostPolicy | None = None,
        *,
        refresh_models: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        """``refresh_models`` re-advertises ids that appear after the runtime started."""
        self._base_conn: Any | None = None
        self.bundle = bundle
        self._refresh_models = refresh_models
        self.policy = policy or HostPolicy()
        self.definition = bundle.definition
        bundle.registry.allow_local_binding_creation = self.policy.can_create_bindings
        self.store = bundle.store.scoped(self.policy.principal, self.policy.deployment_id)
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self.config = ConfigurationService(
            agent=self,
            policy=self.policy,
            registry=bundle.registry,
        )
        self.commands = CommandService(agent=self)
        self._client_conn: Any | None = None
        self.request_context: RequestContext | None = None
        self.fanout = FanoutClient(local=not self.policy.hosted)
        self.fanout.set_approval_guard(self._guard_approval)
        self._connection_capabilities: dict[str, schema.ClientCapabilities | None] = {}
        self._connection_principals: dict[str, str] = {}
        self.question_channel = QuestionChannel()
        self._leases: dict[str, SessionLease] = {}
        self._lease_ttl_ms = self.policy.lease_ttl_ms
        self._lease_task: asyncio.Task[None] | None = None
        self._session_cwd: dict[str, str] = {}
        self._creating_updates: dict[str, list[acp_map.SessionUpdate]] = {}
        self._destroyed: set[str] = set()
        self._lifecycle_tasks: dict[str, asyncio.Task[QueuedPrompt]] = {}
        self._operation_results: dict[str, asyncio.Future[Any]] = {}
        self._operation_kwargs: dict[str, dict[str, Any]] = {}
        models = tuple(self.policy.models)
        default = self.policy.default_model or (models[0] if models else None)
        self._default_model = default
        self.allow_new_sessions = self.policy.allow_new_sessions
        self.sessions = SessionService(
            agent=self,
            store=self.store,
            policy=self.policy,
            definition=self.definition,
            registry=bundle.registry,
            revision_resolver=bundle.revision_resolver,
        )
        super().__init__(
            cast(
                AbstractAgent[Any, str],
                _ComposedAgent(
                    name=self.definition.name or self.definition.agent_id,
                    report_usage=self.sessions.report_model_usage,
                ),
            ),
            name=self.definition.name or self.definition.agent_id,
            # ACP requires a string even when the definition has no version.
            version=self.definition.version or "",
            session_config=self.sessions.compose_session,
            tool_presenter=chain_presenters(coder_tool_presenter, default_coding_presenter),
            session_store=self.store,
            models=models or None,
            model_resolver=bundle.model_resolver,
            prompt_capabilities=schema.PromptCapabilities(embedded_context=True),
        )

    # --- connection and lifecycle ---------------------------------------

    @property
    def _conn(self) -> Any | None:
        override = _conn_override.get()
        return override if override is not None else self._base_conn

    @_conn.setter
    def _conn(self, value: Any | None) -> None:
        self._base_conn = value

    def on_connect(self, conn: Any) -> None:
        connection_id = current_connection_id.get() or uuid.uuid4().hex
        request = current_request.get()
        if request is not None:
            self._connection_principals[connection_id] = request.principal
        self.fanout.attach(connection_id, conn)
        self._client_conn = self.fanout
        self.question_channel.bind(self.fanout)
        super().on_connect(self.fanout)

    def detach(self, connection_id: str) -> None:
        """Remove one viewer without disturbing the shared session state."""
        self.fanout.detach(connection_id)
        self._connection_capabilities.pop(connection_id, None)
        self._connection_principals.pop(connection_id, None)

    def connection_principal(self, connection_id: str) -> str:
        """The authenticated identity of a connection, for subscriber rechecks.

        Local stdio has no request context; its sole client acts as the host policy's
        principal.
        """
        return self._connection_principals.get(connection_id, self.policy.principal)

    def connection_key(self) -> str | None:
        """The current connection, or the sole local stdio connection."""
        connection_id = current_connection_id.get()
        if connection_id is not None:
            return connection_id
        if self.policy.hosted:
            return None
        attached = self.fanout.connections()
        return attached[0] if len(attached) == 1 else None

    def current_client(self) -> Any | None:
        """The ACP client for the connection that initiated the current request."""
        return self.fanout.current()

    def current_capabilities(self) -> schema.ClientCapabilities | None:
        """What the current connection advertised during ``initialize``."""
        connection_id = current_connection_id.get()
        if connection_id is None:
            return _current_capabilities.get()
        return self._connection_capabilities.get(connection_id)

    def acting_principal(self) -> str:
        """The authenticated principal for this request, not the actor's first one."""
        request = current_request.get()
        if request is not None:
            return request.principal
        return self.policy.principal

    async def shutdown(self) -> None:
        """Close every live session through the public adapter method, then the store."""
        lifecycle_tasks = tuple(self._lifecycle_tasks.values())
        for task in lifecycle_tasks:
            task.cancel()
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        for session_id in list(self.sessions.compositions):
            if await self.pending_lifecycle_operation(session_id) is not None:
                with contextlib.suppress(Exception):
                    await super().cancel(session_id)
                self.sessions.forget(session_id)
                self._turn_locks.pop(session_id, None)
                self.config.forget(session_id)
                self.commands.forget(session_id)
                self._session_cwd.pop(session_id, None)
                self.fanout.unsubscribe_session(session_id)
                continue
            with contextlib.suppress(Exception):
                await self._close_session_unchecked(session_id)
        owner = self._lease_owner()
        if owner is not None:
            with contextlib.suppress(Exception):
                await self.store.backend.mark_uncertain_prompts(
                    owner, deployment_id=self.policy.deployment_id
                )
        for session_id in list(self._leases):
            with contextlib.suppress(Exception):
                await self.release_session_lease(session_id)
        if self._lease_task is not None:
            self._lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._lease_task
            self._lease_task = None
        self.sessions.compositions.clear()
        if self.bundle.close_store:
            with contextlib.suppress(Exception):
                await self.store.backend.close()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Cancel a turn only when the caller may mutate this session."""
        try:
            await self.authorize(session_id, PolicyAction.CANCEL)
            await super().cancel(session_id, **kwargs)
        except HrnsError as exc:
            if exc.code == "SESSION_NOT_FOUND":
                return
            raise acp_map.hrns_rpc_error(exc) from exc

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse | None:
        del kwargs
        try:
            await self.terminate_session(session_id, OperationKind.CLOSE)
        except HrnsError as exc:
            if exc.code == "SESSION_NOT_FOUND":
                return None
            raise acp_map.hrns_rpc_error(exc) from exc
        return None

    async def _close_session_unchecked(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse | None:
        try:
            response = await super().close_session(session_id, **kwargs)
            await self.commands.cancel_session(session_id)
            await self.run_lifecycle(LifecycleEvent.DESTROY, session_id)
        finally:
            self.sessions.forget(session_id)
            self._turn_locks.pop(session_id, None)
            self.config.forget(session_id)
            self.commands.forget(session_id)
            self._session_cwd.pop(session_id, None)
            self.fanout.unsubscribe_session(session_id)
            await self.release_session_lease(session_id)
        return response

    def session_is_live(self, session_id: str) -> bool:
        """Whether this process still has authoritative in-memory state for a session."""
        return session_id in self._sessions or session_id in self.sessions.compositions

    def has_pending_lifecycle_tasks(self) -> bool:
        return bool(self._lifecycle_tasks)

    async def pending_lifecycle_operation(self, session_id: str) -> QueuedPrompt | None:
        for kind in (OperationKind.DELETE, OperationKind.CLOSE):
            operation = await self.store.get_lifecycle_operation(session_id, kind)
            if operation is not None and operation.state in LIFECYCLE_PENDING_STATES:
                return operation
        return None

    async def terminate_session(
        self,
        session_id: str,
        kind: OperationKind,
        *,
        operation: QueuedPrompt | None = None,
        authorize: bool = True,
    ) -> QueuedPrompt:
        """Run or resume one durable authoritative close/delete operation."""
        if kind not in (OperationKind.CLOSE, OperationKind.DELETE):
            raise ValueError("session lifecycle kind must be close or delete")
        if operation is not None:
            try:
                payload = json.loads(operation.payload)
            except json.JSONDecodeError:
                payload = {}
            cwd = payload.get("cwd") if isinstance(payload, dict) else None
            if isinstance(cwd, str):
                self._session_cwd[session_id] = cwd
        if operation is None:
            completed = await self.store.get_lifecycle_operation(
                session_id,
                kind,
                principal=self.acting_principal(),
            )
            if (
                completed is not None
                and completed.state is PromptState.COMPLETED
                and not self.session_is_live(session_id)
            ):
                lease = await self.store.get_lease(session_id)
                if lease is None or kind is OperationKind.DELETE:
                    return completed
            record = (
                await self.authorize(
                    session_id,
                    PolicyAction.DELETE if kind is OperationKind.DELETE else PolicyAction.CLOSE,
                )
                if authorize
                else await self.session_record(session_id)
            )
            if record is None:
                raise HrnsError(
                    "SESSION_NOT_FOUND",
                    "no stored session with this id",
                    {"sessionId": session_id},
                )
            self._session_cwd[session_id] = record.cwd
            payload = json.dumps(
                {
                    "agentId": record.agent_id,
                    "cwd": record.cwd,
                },
                separators=(",", ":"),
            )
            operation = await self.store.begin_lifecycle_operation(
                self._queue_row(session_id, payload, kind=kind),
                reuse_completed=not self.session_is_live(session_id),
            )
        if operation.state is PromptState.COMPLETED:
            return operation
        lease = await self.acquire_session_lease(session_id)
        owner = self._lease_owner()
        if lease is None or owner is None:
            raise HrnsError(
                "SESSION_HELD",
                "Closing a hosted session requires its authoritative lease.",
                {"sessionId": session_id},
            )
        task = self._lifecycle_tasks.get(operation.prompt_id)
        if task is None:
            task = asyncio.create_task(
                self._execute_lifecycle_operation(operation, owner=owner, fence=lease.fence)
            )
            self._lifecycle_tasks[operation.prompt_id] = task
            task.add_done_callback(
                lambda _task, operation_id=operation.prompt_id: self._lifecycle_tasks.pop(
                    operation_id, None
                )
            )
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._lifecycle_tasks.pop(operation.prompt_id, None)

    async def _execute_lifecycle_operation(
        self,
        operation: QueuedPrompt,
        *,
        owner: str,
        fence: int,
    ) -> QueuedPrompt:
        claimed = await self.store.claim_lifecycle_operation(
            operation.prompt_id,
            owner,
            fence,
        )
        if claimed is None:
            completed = await self.store.get_lifecycle_operation(
                operation.session_id,
                operation.kind,
                principal=operation.principal,
            )
            if completed is not None and completed.state is PromptState.COMPLETED:
                return completed
            raise HrnsError(
                "SESSION_CONFLICT",
                "The lifecycle operation could not be claimed.",
                {
                    "sessionId": operation.session_id,
                    "operationId": operation.prompt_id,
                },
            )
        session_id = claimed.session_id
        delete_session = claimed.kind is OperationKind.DELETE
        try:
            with contextlib.suppress(Exception):
                await super().cancel(session_id)
            async with self._turn_lock(session_id):
                await self.store.cancel_queued_operations(
                    session_id,
                    exclude_prompt_id=claimed.prompt_id,
                )
                await self.commands.cancel_session(session_id)
                await super().close_session(session_id)
            await self.run_lifecycle(
                LifecycleEvent.DESTROY,
                session_id,
                operation_id=claimed.prompt_id,
            )
            if delete_session:
                database = self.sessions.plan_database()
                if database is not None:
                    from .todo import forget_plan

                    await asyncio.to_thread(forget_plan, database, session_id)
            self.sessions.forget(session_id)
            self._turn_locks.pop(session_id, None)
            self.config.forget(session_id)
            self.commands.forget(session_id)
            self._session_cwd.pop(session_id, None)
            settled = await self.store.settle_lifecycle_operation(
                claimed.prompt_id,
                PromptState.COMPLETED,
                owner_id=owner,
                fence=fence,
                delete_session=delete_session,
            )
            if not settled:
                raise HrnsError(
                    "SESSION_CONFLICT",
                    "The lifecycle result could not be committed with its session fence.",
                    {
                        "sessionId": session_id,
                        "operationId": claimed.prompt_id,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self.store.settle_lifecycle_operation(
                    claimed.prompt_id,
                    PromptState.FAILED,
                    owner_id=owner,
                    fence=fence,
                    delete_session=False,
                )
            self._leases.pop(session_id, None)
            self.store.set_fence(session_id, None)
            raise

        self._leases.pop(session_id, None)
        self.store.set_fence(session_id, None)
        await self.fanout.session_notification(
            session_id,
            "hrns/session/terminated",
            {
                "sessionId": session_id,
                "operation": claimed.kind.value,
                "operationId": claimed.prompt_id,
            },
        )
        self.fanout.unsubscribe_session(session_id)
        return replace(
            claimed,
            state=PromptState.COMPLETED,
            updated_at=now_ms(),
        )

    def _turn_lock(self, session_id: str) -> asyncio.Lock:
        return self._turn_locks.setdefault(session_id, asyncio.Lock())

    # --- session leases ---------------------------------------------------

    def _lease_owner(self) -> str | None:
        return self.policy.worker_id

    async def acquire_session_lease(self, session_id: str) -> SessionLease | None:
        """Take (or renew) exclusive ownership of a session across workers.

        A live other owner is a hard ``SESSION_HELD`` failure: the caller must not run a
        second actor from an independently loaded snapshot. Taking over an expired lease
        fences out the old owner and marks its in-flight prompts uncertain.
        """
        owner = self._lease_owner()
        if owner is None:
            return None
        previous = await self.store.get_lease(session_id)
        lease = await self.store.acquire_lease(session_id, owner, self._lease_ttl_ms)
        if lease is None:
            raise HrnsError(
                "SESSION_HELD",
                "Another live worker owns this session; retry after its lease expires.",
                {
                    "sessionId": session_id,
                    "owner": previous.owner_id if previous is not None else None,
                    "expiresAt": previous.expires_at if previous is not None else None,
                },
            )
        if previous is not None and previous.owner_id != owner:
            with contextlib.suppress(Exception):
                await self.store.backend.mark_uncertain_prompts(
                    previous.owner_id,
                    deployment_id=self.policy.deployment_id,
                    session_id=session_id,
                )
        self._leases[session_id] = lease
        self.store.set_fence(session_id, lease.fence)
        self._start_lease_keeper()
        return lease

    async def release_session_lease(self, session_id: str) -> None:
        # Only the actor that actually holds this lease may release it. A failed
        # ``session/load`` from a second principal must not expire the live owner's
        # lease or clear its fence.
        if self._leases.pop(session_id, None) is None:
            return
        owner = self._lease_owner()
        self.store.set_fence(session_id, None)
        if owner is not None:
            with contextlib.suppress(Exception):
                await self.store.release_lease(session_id, owner)

    def _start_lease_keeper(self) -> None:
        if self._lease_task is None or self._lease_task.done():
            self._lease_task = asyncio.create_task(self._renew_leases())

    async def _renew_leases(self) -> None:
        owner = self._lease_owner()
        if owner is None:
            return
        interval = max(self._lease_ttl_ms / 3000, 0.05)
        try:
            while self._leases:
                await asyncio.sleep(interval)
                for session_id in list(self._leases):
                    try:
                        renewed = await self.store.renew_lease(
                            session_id, owner, self._lease_ttl_ms
                        )
                    except Exception:
                        # A transient store failure must not kill the keeper; retry on
                        # the next tick before the lease can expire.
                        continue
                    if renewed is None:
                        # The lease was lost. Fence every subsequent save with an
                        # impossible token and stop the turn rather than overwrite the
                        # new owner's state. The in-flight row becomes uncertain so it
                        # can never be silently replayed.
                        with contextlib.suppress(Exception):
                            await self.store.backend.mark_uncertain_prompts(
                                owner,
                                deployment_id=self.policy.deployment_id,
                                session_id=session_id,
                            )
                        self._leases.pop(session_id, None)
                        self.store.set_fence(session_id, -1)
                        with contextlib.suppress(Exception):
                            await super().cancel(session_id)
                    else:
                        self._leases[session_id] = renewed
                        self.store.set_fence(session_id, renewed.fence)
        except asyncio.CancelledError:
            return

    # --- lifecycle hooks --------------------------------------------------

    def lifecycle_context(
        self,
        session_id: str,
        *,
        operation_id: str | None = None,
    ) -> SessionLifecycleContext:
        request = self.attribution()
        return SessionLifecycleContext(
            session_id=session_id,
            principal=self.acting_principal(),
            deployment_id=self.policy.deployment_id,
            agent_id=self.definition.agent_id,
            cwd=self._session_cwd.get(session_id, ""),
            credential_id=request.credential_id,
            connection_id=request.connection_id,
            operation_id=operation_id,
        )

    async def run_lifecycle(
        self,
        event: str,
        session_id: str,
        *,
        operation_id: str | None = None,
    ) -> None:
        if event == LifecycleEvent.DESTROY:
            if session_id in self._destroyed:
                return
            await run_hook(
                self.policy.lifecycle,
                event,
                self.lifecycle_context(session_id, operation_id=operation_id),
            )
            # Only a successful destroy is final; a failed hook must remain retryable.
            self._destroyed.add(session_id)
            return
        if event in (LifecycleEvent.CREATE, LifecycleEvent.RESUME):
            self._destroyed.discard(session_id)
        await run_hook(self.policy.lifecycle, event, self.lifecycle_context(session_id))

    async def pending_prompts(self, session_id: str) -> int:
        if self._lease_owner() is None:
            return 0
        states = (PromptState.QUEUED, PromptState.CLAIMED)
        rows = await self.store.list_prompts(session_id, states=states)
        return len(rows)

    async def notify_queue(self, session_id: str) -> None:
        pending = await self.pending_prompts(session_id)
        await run_hook(
            self.policy.lifecycle,
            LifecycleEvent.QUEUE,
            self.lifecycle_context(session_id),
            pending,
        )
        if pending == 0:
            await run_hook(
                self.policy.lifecycle,
                LifecycleEvent.SUSPEND,
                self.lifecycle_context(session_id),
            )

    async def notify_queue_update(self, row: QueuedPrompt) -> None:
        """Fan out one durable queue transition to currently authorized viewers."""

        authorized: list[str] = []
        for connection_id in self.fanout.subscribed_connections(row.session_id):
            principal = self.connection_principal(connection_id)
            visible = await self.store.get_visible(row.session_id, principal)
            if visible is None:
                continue
            decision = await decide_policy(
                self.policy.session_policy,
                self.session_context(
                    visible,
                    row.session_id,
                    principal=principal,
                    connection_id=connection_id,
                ),
                PolicyAction.LOAD,
            )
            if decision.allowed:
                authorized.append(connection_id)
        await self.fanout.session_notification_connections(
            tuple(authorized),
            "hrns/queue/changed",
            self._public_queue_update(row).to_wire(),
        )

    # --- per-session authorization ---------------------------------------

    def attribution(self) -> RequestContext:
        """Attribution for the current request, or the host's default identity."""
        request = current_request.get()
        if request is not None:
            return request
        if self.request_context is not None:
            return self.request_context
        return RequestContext(
            principal=self.acting_principal(),
            agent_id=self.definition.agent_id,
        )

    async def session_record(self, session_id: str) -> SessionRecord | None:
        """The visible stored record, or the staged record of a session not yet used."""
        record = await self.store.get_visible(session_id, self.acting_principal())
        if record is not None:
            return record
        return self.store.staged(session_id)

    def session_context(
        self,
        record: SessionRecord,
        session_id: str,
        *,
        principal: str | None = None,
        connection_id: str | None = None,
    ) -> SessionContext:
        request = self.attribution()
        acting = principal if principal is not None else self.acting_principal()
        return SessionContext(
            session_id=session_id,
            principal=acting,
            deployment_id=self.policy.deployment_id,
            agent_id=self.definition.agent_id,
            owner=record.effective_owner,
            access=record.access,
            collaborators=record.collaborators,
            credential_id=request.credential_id if principal is None else None,
            connection_id=connection_id
            if connection_id is not None
            else (request.connection_id if principal is None else None),
            request_id=request.request_id if principal is None else None,
            metadata={
                "clientCapabilities": (
                    "deployment"
                    if record.accepts_client_capabilities is None
                    else ("enabled" if record.accepts_client_capabilities else "disabled")
                )
            },
        )

    async def _guard_approval(self, session_id: str) -> None:
        await self.authorize(session_id, PolicyAction.APPROVE)

    async def authorize(
        self,
        session_id: str,
        action: PolicyAction,
        *,
        record: SessionRecord | None = None,
    ) -> SessionRecord:
        """Apply the session policy or raise a stable ACP error.

        A denial for a session the caller cannot see uses the same message as a missing
        session so existence is not leaked. A read-only denial uses
        ``SESSION_READ_ONLY`` and is reported separately because the caller can replay
        the session.
        """
        if session_id in self._creating_updates:
            raise HrnsError("SESSION_NOT_FOUND", "no stored session with this id")
        known = record if record is not None else await self.session_record(session_id)
        if known is None:
            raise HrnsError(
                "SESSION_NOT_FOUND",
                "no stored session with this id",
                {"sessionId": session_id},
            )
        decision = await decide_policy(
            self.policy.session_policy,
            self.session_context(known, session_id),
            action,
        )
        if not decision.allowed:
            raise HrnsError(
                decision.code,
                decision.message,
                {
                    "sessionId": session_id,
                    "action": action.value,
                    "readOnly": decision.read_only,
                },
            )
        pending = await self.pending_lifecycle_operation(session_id)
        expected_kind = {
            PolicyAction.CLOSE: OperationKind.CLOSE,
            PolicyAction.DELETE: OperationKind.DELETE,
        }.get(action)
        if pending is not None and pending.kind is not expected_kind:
            raise HrnsError(
                "SESSION_CLOSING",
                "The session is closing and no longer accepts this operation.",
                {
                    "sessionId": session_id,
                    "operationId": pending.prompt_id,
                    "operation": pending.kind.value,
                },
            )
        if action in READ_ONLY_ACTIONS and self._lease_owner() is not None:
            # Every mutation is fenced. A stale actor that lost its lease cannot write
            # metadata, delete, or re-share a session even though policy still allows it.
            await self.acquire_session_lease(session_id)
        return known

    def require_queue_mutation_scope(self) -> None:
        request = self.attribution()
        if request.scopes is not None and "queue:mutate" not in request.scopes:
            raise HrnsError(
                "FORBIDDEN",
                "The authenticated credential does not grant queue mutation scope.",
                {"requiredScope": "queue:mutate"},
            )

    async def authorize_queue_mutation(self, session_id: str) -> SessionRecord:
        self.require_queue_mutation_scope()
        try:
            return await self.authorize(session_id, PolicyAction.PROMPT)
        except HrnsError as exc:
            if exc.code == "SESSION_NOT_FOUND":
                raise HrnsError(
                    "QUEUE_ITEM_NOT_FOUND",
                    "No queued prompt with this id is available in the session.",
                    {"sessionId": session_id},
                ) from exc
            raise

    # --- initialization --------------------------------------------------

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        # Retain what the client advertised even when this deployment's default gate is
        # closed: a session may carry a per-session override that opens it later. The
        # composition gate, not this negotiation, decides whether client-origin tools
        # are actually bound.
        _current_capabilities.set(client_capabilities)
        connection_id = current_connection_id.get()
        if connection_id is not None:
            self._connection_capabilities[connection_id] = client_capabilities
        response = await super().initialize(
            protocol_version,
            client_capabilities=client_capabilities,
            client_info=client_info,
            **kwargs,
        )
        capabilities = response.agent_capabilities or schema.AgentCapabilities()
        capabilities.load_session = True
        capabilities.session_capabilities = schema.SessionCapabilities(
            list=schema.SessionListCapabilities(),
            delete=schema.SessionDeleteCapabilities(),
            close=schema.SessionCloseCapabilities(),
        )
        advertisement = ExtensionAdvertisement(
            version=1,
            methods=list(self.policy.extension_methods()),
            local_binding_creation=self.policy.can_create_bindings,
            accepts_client_capabilities=self.policy.accepts_client_capabilities,
        )
        return response.model_copy(
            update={
                "agent_capabilities": capabilities,
                "field_meta": {"hrns": advertisement.to_wire()},
            }
        )

    # --- session lifecycle -----------------------------------------------

    async def create_authoritative(self, lease: CreationLease) -> SessionCreationResult:
        """Compose provisionally, invoke create, and atomically publish durable state.

        The upstream adapter still owns session execution and replay. Its public load
        operation adopts the already prepared configuration only after the durable
        creation commit; no parallel session runtime is introduced.
        """
        operation = lease.operation
        request = operation.request
        session_id = operation.session_id
        record = SessionRecord(
            session_id=session_id,
            principal=operation.principal,
            deployment_id=operation.deployment_id,
            agent_id=request.agent_id,
            definition_version=request.definition_version,
            cwd=request.cwd,
            access=request.policy.access,
            owner=operation.principal,
            collaborators=request.policy.collaborators,
            accepts_client_capabilities=request.policy.accepts_client_capabilities,
            created_at=operation.created_at,
            last_active_at=operation.created_at,
        )
        if not self.policy.can_create_bindings and any(
            change.type == "add" for change in request.initial_configuration
        ):
            raise HrnsError(
                "POLICY_DENIED", "Local binding creation is prohibited for this deployment."
            )
        self._creating_updates[session_id] = []
        self.fanout.mute_session(session_id)
        self._session_cwd[session_id] = request.cwd
        capability_token = _current_capabilities.set(request.client_capabilities)
        if self._base_conn is None:
            self._base_conn = self.fanout
        try:
            async with self.sessions.new_lock:
                # Intent precedes any factory/hook that may allocate external state.
                await lease.transition(SessionCreationState.CREATING)
                self.sessions.pending_creation = record
                self.sessions.pending_initial = list(request.initial_configuration)
                self.sessions.pending_effort = request.effort
                self.store.stage(record)
                try:
                    config = await self.sessions.compose_session(
                        AcpSession(
                            cwd=request.cwd,
                            mcp_servers=[],
                            session_id=session_id,
                            client_capabilities=request.client_capabilities,
                            client=self.fanout,
                        )
                    )
                finally:
                    self.sessions.pending_creation = None
                    self.sessions.pending_initial = None
                    self.sessions.pending_effort = None
                await run_hook(
                    self.policy.lifecycle,
                    LifecycleEvent.CREATE,
                    creation_lifecycle_context(lease.operation),
                )
                result = SessionCreationResult(
                    operation_id=operation.operation_id,
                    session_id=session_id,
                    agent_id=request.agent_id,
                    deployment_id=operation.deployment_id,
                    definition_version=request.definition_version,
                    config_options=self.sessions.options_for(session_id),
                )
                owner = self._lease_owner()
                session_lease = (
                    SessionLease(
                        session_id=session_id,
                        principal=operation.principal,
                        deployment_id=operation.deployment_id,
                        owner_id=owner,
                        fence=1,
                        expires_at=now_ms() + self._lease_ttl_ms,
                    )
                    if owner is not None
                    else None
                )
                try:
                    await lease.commit(
                        record,
                        StoredSession(
                            messages=[],
                            updates=list(self._creating_updates[session_id]),
                            model=record.model,
                        ),
                        result,
                        session_lease,
                    )
                except HrnsError:
                    raise
                except Exception as exc:
                    raise HrnsError(
                        "SESSION_DURABILITY_FAILED",
                        "The session creation commit could not be confirmed.",
                    ) from exc
                self.store.discard(session_id)
                self.sessions.prepared_configs[session_id] = config
                await self.acquire_session_lease(session_id)
                await super().load_session(request.cwd, session_id)
            self.fanout.unmute_session(session_id)
            connection_id = self.connection_key()
            if connection_id is not None:
                self.fanout.subscribe(connection_id, session_id)
                for update in self._creating_updates[session_id]:
                    await self.fanout.session_update(session_id=session_id, update=update)
            return result
        except BaseException as exc:
            # A failed/unknown commit must be read back before compensation. Never
            # destroy a successfully committed session because its response was lost.
            try:
                current = await self.store.backend.get_creation(
                    operation.principal, operation.mutation_id
                )
                if current is not None and current.state is not SessionCreationState.SUCCEEDED:
                    if current.owner_id == operation.owner_id and current.fence == operation.fence:
                        code = (
                            exc.code if isinstance(exc, HrnsError) else "SESSION_COMPOSITION_FAILED"
                        )
                        message = (
                            exc.message
                            if isinstance(exc, HrnsError)
                            else "Session creation failed."
                        )
                        if isinstance(exc, asyncio.CancelledError):
                            code = None
                            message = None
                        await lease.transition(
                            SessionCreationState.CLEANUP,
                            error_code=code,
                            error_message=message,
                        )
                        await recover_creation(lease, self.policy.lifecycle)
            except BaseException:
                # Keep the durable pending intent for reconciliation, including a
                # cleanup hook failure or an unavailable database.
                pass
            await super().close_session(session_id)
            self.sessions.forget(session_id)
            self.config.forget(session_id)
            self.commands.forget(session_id)
            self._session_cwd.pop(session_id, None)
            self.fanout.unsubscribe_session(session_id)
            with contextlib.suppress(Exception):
                await self.release_session_lease(session_id)
                owner = self._lease_owner()
                if owner is not None:
                    # Commit may have installed the initial lease before an unknown
                    # response prevented this host from adopting it into _leases.
                    await self.store.backend.release_lease(
                        operation.principal,
                        operation.deployment_id,
                        session_id,
                        owner,
                    )
            raise
        finally:
            _current_capabilities.reset(capability_token)
            self._creating_updates.pop(session_id, None)
            self.fanout.unmute_session(session_id)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        try:
            if self.bundle.creation_handler is not None:
                self.sessions.reject_additional_directories(additional_directories)
                self.sessions.reject_client_mcp(mcp_servers)
                meta = kwargs.get("hrns")
                initial = acp_map.initial_configuration_changes(meta)
                values = meta if isinstance(meta, Mapping) else {}
                try:
                    incoming = SessionNewInput(
                        cwd=cwd,
                        initial_configuration=initial,
                        effort=values.get("effort"),
                        mutation_id=values.get("clientMutationId"),
                        definition_version=values.get("definitionVersion"),
                        client_capabilities=self.current_capabilities(),
                    )
                except ValidationError as exc:
                    raise HrnsError(
                        "INVALID_CONFIGURATION", "Invalid session creation input."
                    ) from exc
                result = await self.bundle.creation_handler(incoming)
                if not self.session_is_live(result.session_id):
                    await self.load_session(cwd, result.session_id)
                connection_id = self.connection_key()
                if connection_id is not None:
                    self.fanout.subscribe(connection_id, result.session_id)
                return schema.NewSessionResponse(
                    session_id=result.session_id,
                    config_options=result.config_options,
                    field_meta={"hrns": {"creation": result.model_dump(mode="json")}},
                )
            if not self.allow_new_sessions:
                raise HrnsError(
                    "DEPLOYMENT_UNAVAILABLE",
                    "This deployment revision is hidden; existing sessions can still load.",
                    {"agentId": self.definition.agent_id},
                )
            self.sessions.reject_additional_directories(additional_directories)
            self.sessions.reject_client_mcp(mcp_servers)
            self.policy.validate_cwd(cwd)
            meta = kwargs.get("hrns")
            initial = acp_map.initial_configuration_changes(meta)
            if not self.policy.can_create_bindings:
                added = [change for change in initial if getattr(change, "type", None) == "add"]
                if added:
                    raise HrnsError(
                        "POLICY_DENIED",
                        "Local binding creation is prohibited for this deployment.",
                        {"bindingIds": [change.binding_id for change in added]},
                    )
            effort = None
            if isinstance(meta, Mapping):
                value = meta.get("effort")
                if isinstance(value, str):
                    effort = value
            async with self.sessions.new_lock:
                self.sessions.pending_initial = list(initial) or None
                self.sessions.pending_effort = effort
                try:
                    response = await super().new_session(
                        cwd,
                        additional_directories=None,
                        mcp_servers=None,
                        **kwargs,
                    )
                finally:
                    self.sessions.pending_initial = None
                    self.sessions.pending_effort = None
            self._session_cwd[response.session_id] = cwd
            await self.acquire_session_lease(response.session_id)
            try:
                await self.run_lifecycle(LifecycleEvent.CREATE, response.session_id)
            except Exception:
                await self._close_session_unchecked(response.session_id)
                raise
            connection_id = self.connection_key()
            if connection_id is not None:
                self.fanout.subscribe(connection_id, response.session_id)
            return response.model_copy(
                update={"config_options": self.sessions.options_for(response.session_id)}
            )
        except HrnsError as exc:
            raise acp_map.hrns_rpc_error(exc) from exc
        except RequestError:
            raise

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> schema.LoadSessionResponse | None:
        owned_before = session_id in self._leases
        try:
            self.sessions.reject_additional_directories(additional_directories)
            self.sessions.reject_client_mcp(mcp_servers)
            record = await self.store.get_visible(session_id, self.acting_principal())
            if record is None:
                raise RequestError.invalid_params(
                    {"sessionId": session_id, "reason": "no stored session with this id"}
                )
            await self.authorize(session_id, PolicyAction.LOAD, record=record)
            await self.authorize(session_id, PolicyAction.REPLAY, record=record)
            revision = self.sessions.revision_for(record)
            await self.acquire_session_lease(session_id)
            connection_id = self.connection_key()
            if connection_id is not None:
                self.fanout.subscribe(connection_id, session_id)
            live = self._sessions.get(session_id)
            if live is not None:
                # Another connection already owns the live actor. Attach this viewer and
                # replay its transcript to it only; do not tear down or rebuild the
                # session that the first connection is using.
                client = self.current_client()
                if client is None:
                    raise HrnsError(
                        "SESSION_REPLAY_UNAVAILABLE",
                        "The replay target connection is unavailable.",
                        {"sessionId": session_id, "restartFullLoad": True},
                    )
                async with live.lock:
                    try:
                        replay = order_session_updates(live.transcript)
                    except SessionUpdateProtocolError as exc:
                        raise HrnsError(
                            "SESSION_UPDATE_PROTOCOL_ERROR",
                            "The live session transcript has conflicting update positions.",
                            {"sessionId": session_id, "restartFullLoad": True},
                        ) from exc
                    for update in replay:
                        await client.session_update(session_id=session_id, update=update)
                return schema.LoadSessionResponse(
                    config_options=self.sessions.options_for(session_id)
                )
            try:
                stored = await self.store.load(session_id)
            except Exception as exc:
                raise HrnsError(
                    "SESSION_REPLAY_UNAVAILABLE",
                    "The durable session transcript could not be validated.",
                    {"sessionId": session_id, "restartFullLoad": True},
                ) from exc
            if stored is None:
                raise HrnsError(
                    "SESSION_REPLAY_UNAVAILABLE",
                    "The durable session transcript is unavailable.",
                    {"sessionId": session_id, "restartFullLoad": True},
                )
            stored_cwd = self.policy.validate_cwd(record.cwd, allowed=revision.allowed_cwds)
            # Replay only to the connection that asked to load. The shared host keeps
            # fan-out for live turns, but a fresh viewer must not duplicate another
            # viewer's transcript.
            requesting = self.current_client()
            if requesting is None:
                raise HrnsError(
                    "SESSION_REPLAY_UNAVAILABLE",
                    "The replay target connection is unavailable.",
                    {"sessionId": session_id, "restartFullLoad": True},
                )
            token = _conn_override.set(requesting)
            try:
                response = await super().load_session(
                    stored_cwd,
                    session_id,
                    mcp_servers=None,
                    additional_directories=None,
                    **kwargs,
                )
            finally:
                _conn_override.reset(token)
            self._session_cwd[session_id] = stored_cwd
            if not record.read_only:
                await self.run_lifecycle(LifecycleEvent.RESUME, session_id)
        except HrnsError as exc:
            if not owned_before:
                await self.release_session_lease(session_id)
            raise acp_map.hrns_rpc_error(exc) from exc
        except RequestError:
            if not owned_before:
                await self.release_session_lease(session_id)
            raise
        if response is None:
            return response
        return response.model_copy(update={"config_options": self.sessions.options_for(session_id)})

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> schema.ListSessionsResponse:
        del kwargs
        return await self.sessions.list_sessions(cwd=cwd, cursor=cursor)

    async def delete_session(self, session_id: str, **kwargs: Any) -> DeleteSessionResponse | None:
        del kwargs
        try:
            await self.terminate_session(session_id, OperationKind.DELETE)
            return DeleteSessionResponse()
        except HrnsError as exc:
            raise acp_map.hrns_rpc_error(exc) from exc

    async def _emit_event(self, turn: _TurnState, event: AgentStreamEvent) -> None:
        """Record and render steering at delivery time, in stream order."""
        if isinstance(event, EnqueuedMessagesEvent):
            for message in event.messages:
                if not isinstance(message, ModelRequest):
                    continue
                for part in message.parts:
                    if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                        await self._send_update(
                            turn,
                            schema.UserMessageChunk(
                                session_update="user_message_chunk",
                                content=schema.TextContentBlock(type="text", text=part.content),
                            ),
                        )
            return
        await super()._emit_event(turn, event)

    async def _position_updates(
        self,
        session_id: str,
        updates: Sequence[acp_map.SessionUpdate],
    ) -> list[acp_map.SessionUpdate]:
        """Assign durable, non-reusable positions to updates that do not have one."""
        try:
            missing = [
                index
                for index, update in enumerate(updates)
                if maybe_session_update_position(update) is None
            ]
        except SessionUpdateProtocolError as exc:
            raise HrnsError(
                "SESSION_UPDATE_PROTOCOL_ERROR",
                "A session update has invalid or conflicting position metadata.",
                {"sessionId": session_id},
            ) from exc
        if not missing:
            return list(updates)
        try:
            revisions = iter(await self.store.reserve_update_revisions(session_id, len(missing)))
        except HrnsError:
            raise
        except Exception as exc:
            raise HrnsError(
                "SESSION_DURABILITY_FAILED",
                "Could not reserve durable session update revisions.",
                {"sessionId": session_id},
            ) from exc
        positioned = list(updates)
        for index in missing:
            positioned[index] = with_session_update_position(
                positioned[index],
                SessionUpdatePosition(update_id=uuid.uuid4().hex, revision=next(revisions)),
            )
        return positioned

    async def _send_update(
        self,
        turn: _TurnState,
        update: acp_map.SessionUpdate,
    ) -> None:
        """Position live updates before delivery, including the replay-only user prompt."""
        turn.updates[:] = await self._position_updates(turn.session_id, turn.updates)
        positioned = (await self._position_updates(turn.session_id, [update]))[0]
        await super()._send_update(turn, positioned)

    async def _fail_outstanding_tool_calls(
        self,
        turn: _TurnState,
        *,
        record: bool = False,
    ) -> None:
        """Finish announced tools with positioned updates even on cancellation."""
        for tool_call_id in turn.started - turn.resulted:
            update = acp_sdk.update_tool_call(tool_call_id=tool_call_id, status="failed")
            positioned = (await self._position_updates(turn.session_id, [update]))[0]
            if record:
                turn.updates.append(positioned)
            with contextlib.suppress(Exception):
                await turn.conn.session_update(session_id=turn.session_id, update=positioned)

    async def _persist(self, state: Any) -> None:
        """Persist canonical positioned updates and fail closed on durability loss."""
        try:
            state.transcript[:] = order_session_updates(
                await self._position_updates(state.session_id, state.transcript)
            )
            await self.store.save(
                state.session_id,
                StoredSession(
                    messages=list(state.history),
                    updates=list(state.transcript),
                    model=state.model,
                ),
            )
        except SessionUpdateProtocolError as exc:
            raise HrnsError(
                "SESSION_UPDATE_PROTOCOL_ERROR",
                "The session transcript has conflicting update positions.",
                {"sessionId": state.session_id},
            ) from exc
        except HrnsError:
            raise
        except Exception as exc:
            raise HrnsError(
                "SESSION_DURABILITY_FAILED",
                "The committed session state could not be persisted.",
                {"sessionId": state.session_id},
            ) from exc

    async def publish_session_update(
        self,
        session_id: str,
        update: acp_map.SessionUpdate,
        *,
        durable: bool,
    ) -> None:
        """Position and publish a host-owned update, persisting it first when durable."""
        pending = self._creating_updates.get(session_id)
        if pending is not None:
            pending.append(
                with_session_update_position(
                    update,
                    SessionUpdatePosition(
                        update_id=uuid.uuid4().hex,
                        revision=len(pending) + 1,
                    ),
                )
            )
            return
        positioned = (await self._position_updates(session_id, [update]))[0]
        if durable:
            state = self._sessions.get(session_id)
            if state is None:
                raise HrnsError(
                    "SESSION_REPLAY_UNAVAILABLE",
                    "The session is not open, so its durable update cannot be journaled.",
                    {"sessionId": session_id, "restartFullLoad": True},
                )
            state.transcript.append(positioned)
            await self._persist(state)
        await self.fanout.session_update(session_id=session_id, update=positioned)

    # --- prompts, effort, and metadata -----------------------------------

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        try:
            return await self._prompt_impl(session_id, prompt, **kwargs)
        except HrnsError as exc:
            raise acp_map.hrns_rpc_error(exc) from exc

    async def _prompt_impl(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        # A composed session is only in memory; its first ordered operation makes it
        # durable. Hosted prompts are appended before waiting for the turn boundary, so
        # an older queued prompt or configuration mutation cannot be overtaken.
        await self.authorize(session_id, PolicyAction.PROMPT)
        await self.sessions.ensure_persisted(session_id)
        await self.acquire_session_lease(session_id)
        connection_id = self.connection_key()
        if connection_id is not None:
            self.fanout.subscribe(connection_id, session_id)
        if self._lease_owner() is None:
            async with self._turn_lock(session_id):
                return await self._run_prompt_turn(session_id, prompt, kwargs)
        payload = self._queue_payload(prompt)
        self._decode_prompt(payload)
        row = self._queue_row(session_id, payload)
        result = await self._submit_ordered_operation(row, kwargs=kwargs)
        if not isinstance(result, schema.PromptResponse):
            raise HrnsError(
                "SESSION_CONFLICT",
                "The ordered prompt completed without a prompt response.",
                {"sessionId": session_id, "promptId": row.prompt_id},
            )
        return result

    async def _run_prompt_turn(
        self,
        session_id: str,
        prompt: list[Any],
        kwargs: Mapping[str, Any],
    ) -> schema.PromptResponse:
        await self.sessions.ensure_title(session_id, _prompt_text(prompt))
        self.sessions.turn_usage[session_id] = {}
        response = None
        try:
            response = await super().prompt(session_id, prompt, **dict(kwargs))
            return response
        finally:
            channel = self.sessions.steering.get(session_id)
            if channel is not None:
                channel.detach()
            increment = self.sessions.turn_usage.pop(session_id, {})
            if response is not None and response.usage is not None:
                for name, value in usage_payload(response.usage).items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        increment[name] = max(increment.get(name, 0), value)
            record = await self.store.get_visible(session_id, self.acting_principal())
            if record is not None and (self._lease_owner() is None or session_id in self._leases):
                title = record.title
                if not title and response is not None:
                    title = _prompt_text(prompt) or None
                totals = merge_usage(record.usage, increment)
                request = self.attribution()
                totals["last_request"] = {
                    "principal": request.principal,
                    "credential_id": request.credential_id,
                    "connection_id": request.connection_id,
                    "request_id": request.request_id,
                }
                await self.store.update(
                    session_id,
                    title=title,
                    usage=totals,
                    touch=True,
                )
                await self.sessions.send_session_usage(session_id, finalize_usage(totals))
                if title and title != record.title:
                    await self.sessions.send_session_title(session_id, title)

    def _new_operation_waiter(self, operation_id: str) -> asyncio.Future[Any]:
        waiter: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._operation_results[operation_id] = waiter
        return waiter

    # --- durable prompt queue --------------------------------------------

    @staticmethod
    def _queue_payload(prompt: Sequence[Any]) -> str:
        entries: list[object] = []
        for block in prompt:
            dump = getattr(block, "model_dump", None)
            if callable(dump):
                entries.append(dump(mode="json", by_alias=True, exclude_none=True))
            elif isinstance(block, Mapping):
                entries.append(dict(block))
            else:
                entries.append(block)
        try:
            validated = _PROMPT_BLOCKS.validate_python(entries)
        except ValidationError as exc:
            raise HrnsError(
                "INVALID_CONFIGURATION",
                "prompt must be a valid ACP ContentBlock array.",
                {"errors": exc.errors()[:10]},
            ) from exc
        payload = json.dumps(
            [
                block.model_dump(mode="json", by_alias=True, exclude_none=True)
                for block in validated
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise HrnsError(
                "INVALID_CONFIGURATION",
                "The queued prompt payload exceeds the transport bound.",
                {"maxBytes": MAX_MESSAGE_BYTES},
            )
        return payload

    def _queue_row(
        self,
        session_id: str,
        payload: str,
        *,
        kind: OperationKind = OperationKind.PROMPT,
        parent_id: str | None = None,
    ) -> QueuedPrompt:
        request = self.attribution()
        now = now_ms()
        return QueuedPrompt(
            prompt_id=uuid.uuid4().hex,
            session_id=session_id,
            principal=self.acting_principal(),
            deployment_id=self.policy.deployment_id,
            credential_id=request.credential_id,
            connection_id=request.connection_id,
            sequence=0,
            kind=kind,
            payload=payload,
            state=PromptState.QUEUED,
            attempt=0,
            parent_id=parent_id,
            owner_id=None,
            fence=None,
            update_id=uuid.uuid4().hex,
            revision=0,
            created_at=now,
            updated_at=now,
        )

    async def enqueue_prompt(
        self,
        session_id: str,
        prompt: Sequence[Any],
        *,
        mutation_id: str,
    ) -> QueuedPrompt:
        """Persist a prompt without executing it; a later run drains it in order."""
        await self.authorize_queue_mutation(session_id)
        await self.sessions.ensure_persisted(session_id)
        await self.acquire_session_lease(session_id)
        payload = self._queue_payload(prompt)
        request_hash = canonical_hash(
            {
                "operation": "enqueue",
                "sessionId": session_id,
                "prompt": json.loads(payload),
                "mutationId": mutation_id,
            }
        )
        try:
            result = await self.store.enqueue_prompt_mutation(
                self._queue_row(session_id, payload),
                mutation_id=mutation_id,
                request_hash=request_hash,
            )
        except HrnsError:
            raise
        except Exception as exc:
            raise HrnsError(
                "SESSION_DURABILITY_FAILED",
                "The queued prompt could not be durably written.",
                {"sessionId": session_id},
            ) from exc
        if not result.replayed:
            await self.notify_queue_update(result.item)
            await self.notify_queue(session_id)
        return result.item

    async def edit_queued_prompt(self, request: QueueEditRequest) -> QueueUpdate:
        """Replace one still-queued prompt without changing its identity or order."""

        await self.authorize_queue_mutation(request.session_id)
        payload = self._queue_payload(request.prompt)
        request_hash = canonical_hash(
            {
                "operation": "edit",
                **request.to_wire(),
                "prompt": json.loads(payload),
            }
        )
        try:
            result = await self.store.edit_prompt(
                request.session_id,
                request.prompt_id,
                expected_revision=request.expected_revision,
                payload=payload,
                mutation_id=request.mutation_id,
                request_hash=request_hash,
                update_id=uuid.uuid4().hex,
            )
        except HrnsError:
            raise
        except Exception as exc:
            raise HrnsError(
                "SESSION_DURABILITY_FAILED",
                "The queued prompt edit could not be durably written.",
                {"sessionId": request.session_id, "promptId": request.prompt_id},
            ) from exc
        if not result.replayed:
            await self.notify_queue_update(result.item)
        return self._public_queue_update(result.item)

    async def remove_queued_prompt(self, request: QueueRemoveRequest) -> QueueUpdate:
        """Cancel one still-queued prompt as a durable tombstone."""

        await self.authorize_queue_mutation(request.session_id)
        request_hash = canonical_hash({"operation": "remove", **request.to_wire()})
        try:
            result = await self.store.remove_prompt(
                request.session_id,
                request.prompt_id,
                expected_revision=request.expected_revision,
                mutation_id=request.mutation_id,
                request_hash=request_hash,
                update_id=uuid.uuid4().hex,
            )
        except HrnsError:
            raise
        except Exception as exc:
            raise HrnsError(
                "SESSION_DURABILITY_FAILED",
                "The queued prompt removal could not be durably written.",
                {"sessionId": request.session_id, "promptId": request.prompt_id},
            ) from exc
        if not result.replayed:
            await self.notify_queue_update(result.item)
            await self.notify_queue(request.session_id)
        return self._public_queue_update(result.item)

    @staticmethod
    def _decode_prompt(payload: str) -> list[Any]:
        """Rebuild the complete ACP content array stored by :meth:`_queue_payload`."""
        try:
            return list(_PROMPT_BLOCKS.validate_json(payload))
        except ValidationError as exc:
            raise HrnsError(
                "INVALID_CONFIGURATION",
                "The queued prompt payload is malformed.",
                {"errors": exc.errors()[:10]},
            ) from exc

    @classmethod
    def _public_queue_item(cls, row: QueuedPrompt) -> QueueItem:
        prompt = [
            block.model_dump(mode="json", by_alias=True, exclude_none=True)
            for block in cls._decode_prompt(row.payload)
        ]
        return QueueItem(
            update_id=row.update_id,
            revision=row.revision,
            prompt_id=row.prompt_id,
            session_id=row.session_id,
            principal=row.principal,
            deployment_id=row.deployment_id,
            credential_id=row.credential_id,
            connection_id=row.connection_id,
            sequence=row.sequence,
            prompt=prompt,
            state=row.state.value,
            attempt=row.attempt,
            parent_id=row.parent_id,
            owner_id=row.owner_id,
            fence=row.fence,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @classmethod
    def _public_queue_update(cls, row: QueuedPrompt) -> QueueUpdate:
        item = cls._public_queue_item(row)
        return QueueUpdate(update_id=item.update_id, revision=item.revision, item=item)

    async def run_queued_prompts(self, session_id: str, *, limit: int = 100) -> list[QueuedPrompt]:
        """Execute durable session operations in their authoritative order."""
        await self.authorize(session_id, PolicyAction.PROMPT)
        self.sessions.require_composition(session_id)
        await self.acquire_session_lease(session_id)
        return await self._drain_ordered_operations(session_id, limit=limit)

    async def _drain_ordered_operations(
        self,
        session_id: str,
        *,
        stop_id: str | None = None,
        limit: int = 100,
    ) -> list[QueuedPrompt]:
        owner = self._lease_owner()
        if owner is None:
            raise HrnsError("SESSION_HELD", "Draining ordered work requires a hosted session.")
        done: list[QueuedPrompt] = []
        async with self._turn_lock(session_id):
            processed = 0
            while stop_id is not None or processed < limit:
                lifecycle = await self.pending_lifecycle_operation(session_id)
                if lifecycle is not None:
                    if stop_id is not None:
                        raise HrnsError(
                            "SESSION_CLOSING",
                            "The session stopped accepting ordered work.",
                            {
                                "sessionId": session_id,
                                "operationId": lifecycle.prompt_id,
                                "operation": lifecycle.kind.value,
                            },
                        )
                    break
                lease = self._leases.get(session_id)
                if lease is None:
                    raise HrnsError(
                        "SESSION_HELD",
                        "Draining ordered work requires an owned session lease.",
                    )
                row = await self.store.claim_next_prompt(
                    session_id, owner, lease.fence, self._lease_ttl_ms
                )
                if row is None:
                    if stop_id is not None:
                        raise HrnsError(
                            "SESSION_CONFLICT",
                            "Earlier ordered work is unresolved; refusing to overtake it.",
                            {"sessionId": session_id, "operationId": stop_id},
                        )
                    break
                processed += 1
                if row.kind is OperationKind.PROMPT:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await self.notify_queue_update(row)
                outcome = PromptState.FAILED
                result: Any = None
                failure: BaseException | None = None
                try:
                    result = await self._execute_ordered_operation(row)
                    if (
                        row.kind is OperationKind.PROMPT
                        and getattr(result, "stop_reason", None) == "cancelled"
                    ):
                        outcome = PromptState.UNCERTAIN
                    else:
                        outcome = PromptState.COMPLETED
                except asyncio.CancelledError as exc:
                    outcome = PromptState.UNCERTAIN
                    failure = exc
                except Exception as exc:
                    failure = exc
                finally:
                    finished = await self.finish_queued_prompt(row, outcome)
                    if not finished and failure is None:
                        failure = HrnsError(
                            "SESSION_CONFLICT",
                            "The ordered operation executed but its completion could not be fenced.",
                            {
                                "sessionId": session_id,
                                "operationId": row.prompt_id,
                            },
                        )
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await self.notify_queue(session_id)
                waiter = self._operation_results.get(row.prompt_id)
                if waiter is not None and not waiter.done():
                    if isinstance(failure, asyncio.CancelledError):
                        waiter.cancel()
                    elif failure is not None:
                        waiter.set_exception(failure)
                    else:
                        waiter.set_result(result)
                done.append(await self.store.get_prompt(row.prompt_id) or row)
                if isinstance(failure, asyncio.CancelledError):
                    raise failure
                if row.prompt_id == stop_id:
                    break
        return done

    async def _execute_ordered_operation(self, row: QueuedPrompt) -> Any:
        request = RequestContext(
            principal=row.principal,
            credential_id=row.credential_id,
            connection_id=row.connection_id,
            agent_id=self.definition.agent_id,
        )
        request_token = current_request.set(request)
        connection_token = current_connection_id.set(row.connection_id)
        fanout_token = (
            self.fanout.select(row.connection_id) if row.connection_id is not None else None
        )
        try:
            if row.kind is OperationKind.PROMPT:
                kwargs = self._operation_kwargs.pop(row.prompt_id, {})
                return await self._run_prompt_turn(
                    row.session_id,
                    self._decode_prompt(row.payload),
                    kwargs,
                )
            if row.kind is OperationKind.CONFIGURATION:
                payload = json.loads(row.payload)
                request = ConfigurationUpdate.model_validate(payload)
                return await self.config.apply(request)
            if row.kind is OperationKind.CONFIG_OPTION:
                payload = json.loads(row.payload)
                if not isinstance(payload, dict):
                    raise HrnsError(
                        "INVALID_CONFIGURATION", "The queued config option is malformed."
                    )
                config_id = payload.get("configId")
                value = payload.get("value")
                if not isinstance(config_id, str) or not isinstance(value, (str, bool)):
                    raise HrnsError(
                        "INVALID_CONFIGURATION", "The queued config option is malformed."
                    )
                kwargs = self._operation_kwargs.pop(row.prompt_id, {})
                return await self._apply_config_option(config_id, row.session_id, value, **kwargs)
            raise HrnsError(
                "INVALID_CONFIGURATION",
                "The queued operation kind is not supported.",
                {"kind": row.kind.value},
            )
        finally:
            if fanout_token is not None:
                self.fanout.reset(fanout_token)
            current_connection_id.reset(connection_token)
            current_request.reset(request_token)

    async def finish_queued_prompt(
        self, queued: QueuedPrompt | None, state: PromptState
    ) -> QueuedPrompt | None:
        if queued is None:
            return None
        # The row's own owner/fence is authoritative even when the in-memory lease was
        # already lost; otherwise an interrupted finish leaves the row claimed forever.
        owner = queued.owner_id or self._lease_owner()
        fence = queued.fence
        if owner is None or fence is None:
            return None
        try:
            finished = await asyncio.shield(
                self.store.finish_prompt(queued.prompt_id, state, owner_id=owner, fence=fence)
            )
        except (Exception, asyncio.CancelledError):
            return None
        if finished is not None and finished.kind is OperationKind.PROMPT:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self.notify_queue_update(finished)
        return finished

    async def list_queued_prompts(self, session_id: str) -> QueueSnapshot:
        """Read the complete authoritative prompt queue at one durable revision."""
        await self.authorize(session_id, PolicyAction.LOAD)
        connection_id = self.connection_key()
        if connection_id is not None:
            self.fanout.subscribe(connection_id, session_id)
        snapshot = await self.store.queue_snapshot(session_id)
        return QueueSnapshot(
            session_id=session_id,
            snapshot_revision=snapshot.revision,
            items=[self._public_queue_item(item) for item in snapshot.items],
        )

    async def retry_queued_prompt(
        self,
        session_id: str,
        prompt_id: str,
        *,
        mutation_id: str,
    ) -> QueueUpdate:
        """Link a new attempt to an uncertain prompt; execution stays explicit."""
        self.require_queue_mutation_scope()
        await self.authorize(session_id, PolicyAction.RETRY)
        original = await self.store.get_prompt(prompt_id)
        if original is None or original.kind is not OperationKind.PROMPT:
            raise HrnsError(
                "INVALID_REQUEST",
                "Only an uncertain prompt can be retried.",
                {"promptId": prompt_id},
            )
        lease = await self.acquire_session_lease(session_id)
        owner = self._lease_owner()
        if lease is None or owner is None:
            raise HrnsError("SESSION_HELD", "Retrying a prompt requires an owned session lease.")
        request_hash = canonical_hash(
            {
                "operation": "retry",
                "sessionId": session_id,
                "promptId": prompt_id,
                "mutationId": mutation_id,
            }
        )
        result = await self.store.retry_prompt(
            prompt_id,
            session_id=session_id,
            new_prompt_id=uuid.uuid4().hex,
            owner_id=owner,
            fence=lease.fence,
            mutation_id=mutation_id,
            request_hash=request_hash,
        )
        if result is None:
            raise HrnsError(
                "INVALID_REQUEST",
                "Only an uncertain prompt can be retried.",
                {"promptId": prompt_id},
            )
        if not result.replayed:
            await self.notify_queue_update(result.item)
            await self.notify_queue(session_id)
        return self._public_queue_update(result.item)

    async def _submit_ordered_operation(
        self,
        row: QueuedPrompt,
        *,
        kwargs: Mapping[str, Any] | None = None,
    ) -> Any:
        waiter = self._new_operation_waiter(row.prompt_id)
        if kwargs:
            self._operation_kwargs[row.prompt_id] = dict(kwargs)
        try:
            stored = await self.store.enqueue_prompt(row)
        except BaseException:
            self._operation_results.pop(row.prompt_id, None)
            self._operation_kwargs.pop(row.prompt_id, None)
            raise
        await self.notify_queue(row.session_id)
        try:
            await self._drain_ordered_operations(row.session_id, stop_id=stored.prompt_id)
            return await asyncio.shield(waiter)
        finally:
            self._operation_results.pop(stored.prompt_id, None)
            self._operation_kwargs.pop(stored.prompt_id, None)

    async def order_configuration_update(self, request: ConfigurationUpdate) -> dict[str, Any]:
        """Order a binding transaction with prompts at the next turn boundary."""
        await self.authorize(request.session_id, PolicyAction.CONFIGURE)
        self.config.validate(request)
        await self.sessions.ensure_persisted(request.session_id)
        await self.acquire_session_lease(request.session_id)
        if self._lease_owner() is None:
            async with self._turn_lock(request.session_id):
                return await self.config.apply(request)
        payload = json.dumps(
            request.to_wire(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self._submit_ordered_operation(
            self._queue_row(
                request.session_id,
                payload,
                kind=OperationKind.CONFIGURATION,
            )
        )
        if not isinstance(result, dict):
            raise HrnsError(
                "SESSION_CONFLICT",
                "The ordered configuration update completed without a snapshot.",
                {"sessionId": request.session_id, "requestId": request.request_id},
            )
        return result

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse | None:
        try:
            await self.authorize(session_id, PolicyAction.CONFIGURE)
            await self.sessions.ensure_persisted(session_id)
            await self.acquire_session_lease(session_id)
            if self._lease_owner() is None:
                async with self._turn_lock(session_id):
                    return await self._apply_config_option(config_id, session_id, value, **kwargs)
            payload = json.dumps(
                {"configId": config_id, "value": value},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            result = await self._submit_ordered_operation(
                self._queue_row(
                    session_id,
                    payload,
                    kind=OperationKind.CONFIG_OPTION,
                ),
                kwargs=kwargs,
            )
            if result is not None and not isinstance(result, schema.SetSessionConfigOptionResponse):
                raise HrnsError(
                    "SESSION_CONFLICT",
                    "The ordered config option completed without a valid response.",
                    {"sessionId": session_id, "configId": config_id},
                )
            return result
        except HrnsError as exc:
            raise acp_map.hrns_rpc_error(exc) from exc

    async def _apply_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse | None:
        if config_id == acp_map.EFFORT_OPTION_ID:
            composition = self.sessions.require_composition(session_id)
            if not isinstance(value, str):
                raise RequestError.invalid_params(
                    {"configId": config_id, "reason": "effort expects a string"}
                )
            support = composition.effort.support
            option = acp_map.effort_option(None, support=support)
            if option is None:
                raise RequestError.invalid_params(
                    {
                        "configId": config_id,
                        "reason": "the selected model has no reasoning effort option",
                    }
                )
            allowed = {
                item.value
                for item in option.options
                if isinstance(item, schema.SessionConfigSelectOption)
            }
            if value not in allowed:
                raise RequestError.invalid_params(
                    {"configId": config_id, "value": value, "options": sorted(allowed)}
                )
            composition.effort.effort = None if value == EFFORT_DEFAULT else value
            await self.sessions.persist_metadata(session_id, effort=value)
            return schema.SetSessionConfigOptionResponse(
                config_options=self.sessions.options_for(session_id)
            )
        if config_id == acp_map.MODEL_OPTION_ID and isinstance(value, str):
            record = await self.session_record(session_id)
            revision = self.sessions.revision_for(record)
            pinned = record is not None and record.definition_version not in (
                "",
                self.definition.version or "",
            )
            if pinned and revision.models:
                # The revision advertises its own model set.
                allowed = revision.models
                model_support = revision.effort_support(value)
            else:
                # An empty pinned revision carries no model set of its own, so the live
                # actor's advertised set applies, exactly as composition falls back.
                self._adopt_added_models(value)
                allowed = self._models
                model_support = self.policy.effort_support(value)
            if value not in allowed:
                raise RequestError.invalid_params(
                    {"model_id": value, "reason": "not an advertised model"}
                )
            if value not in self._models:
                # The upstream adapter validates against its process-wide advertised set;
                # retain a pinned model there so switching back to it remains possible.
                self._models = (*self._models, value)
        else:
            model_support = None
        response = await super().set_config_option(config_id, session_id, value, **kwargs)
        if config_id == acp_map.MODEL_OPTION_ID and self._default_model is not None:
            composition = self.sessions.composition(session_id)
            if composition is not None and isinstance(value, str):
                support = model_support
                composition.effort.set_model(value, support)
                support_after = composition.effort.support
                keep = composition.effort.effort
                if keep is not None and acp_map.effort_option(keep, support=support_after) is None:
                    composition.effort.effort = None
                    keep = None
                await self.sessions.persist_metadata(session_id, effort=keep or EFFORT_DEFAULT)
        if response is None:
            return response
        return schema.SetSessionConfigOptionResponse(
            config_options=self.sessions.options_for(session_id)
        )

    def _adopt_added_models(self, model_id: str) -> None:
        """Adopt ids a config edit added after this runtime started.

        The adapter only accepts advertised ids, and the terminal adds providers and
        custom models while a session is open. The local runtime re-reads its config
        file here instead of being restarted, so switching stays live.
        """
        if self._refresh_models is None or model_id in self._models:
            return
        models = self._refresh_models()
        if models:
            self._models = models

    # --- project extension ------------------------------------------------

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if method == "hrns/session/rename":
                from hrns.protocol.models import SessionTitleUpdate

                request = SessionTitleUpdate.model_validate(params)
                await self.authorize(request.session_id, PolicyAction.RENAME)
                if not await self.sessions.ensure_persisted(request.session_id):
                    raise HrnsError("SESSION_CLOSED", "Load the session before renaming it.")
                await self.store.update(request.session_id, title=request.title, touch=True)
                await self.sessions.send_session_title(request.session_id, request.title)
                return {"sessionId": request.session_id, "title": request.title}
            if method == "hrns/session/steer":
                request = SteerRequest.model_validate(dict(params))
                await self.authorize(request.session_id, PolicyAction.STEER)
                if await self.session_record(request.session_id) is None:
                    raise HrnsError("SESSION_CLOSED", "Load the session before steering it.")
                channel = self.sessions.steering.get(request.session_id)
                if channel is None or not channel.enqueue(request.text):
                    raise HrnsError(
                        "TURN_NOT_RUNNING",
                        "The turn is no longer accepting steering; send the message as a prompt.",
                    )
                return {"sessionId": request.session_id, "steered": True}
            if method == "hrns/capabilities/list":
                request = ListRequest.model_validate(dict(params))
                if not request.session_id:
                    raise HrnsError(
                        "INVALID_CONFIGURATION", "sessionId is required for a capability listing."
                    )
                await self.authorize(request.session_id, PolicyAction.LOAD)
                composition = self.sessions.require_composition(request.session_id)
                descriptors = composition.descriptors()
                page, next_cursor = paginate(
                    descriptors, cursor=request.cursor, limit=request.limit or 50
                )
                snapshot = composition.snapshot
                return BindingListResponse(
                    items=page,
                    next_cursor=next_cursor,
                    selected_revision=snapshot.selected_revision,
                    active_revision=snapshot.active_revision,
                    policy_revision=snapshot.policy_revision,
                ).to_wire()
            if method == "hrns/plugins/list":
                if not self.policy.can_create_bindings:
                    raise RequestError.method_not_found("_hrns/plugins/list")
                request = ListRequest.model_validate(dict(params))
                page, next_cursor = paginate(
                    self.bundle.registry.catalog(),
                    cursor=request.cursor,
                    limit=request.limit or 50,
                )
                return PluginListResponse(items=page, next_cursor=next_cursor).to_wire()
            if method == "hrns/configuration/get":
                session_id = params.get("sessionId", params.get("session_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HrnsError("INVALID_CONFIGURATION", "sessionId is required.")
                await self.authorize(session_id, PolicyAction.LOAD)
                composition = self.sessions.require_composition(session_id)
                return composition.snapshot.to_wire()
            if method == "hrns/configuration/update":
                request = ConfigurationUpdate.model_validate(dict(params))
                await self.authorize(request.session_id, PolicyAction.CONFIGURE)
                return await self.config.update(request)
            if method == "hrns/commands/list":
                request = CommandListRequest.model_validate(dict(params))
                await self.authorize(request.session_id, PolicyAction.LOAD)
                composition = self.sessions.require_composition(request.session_id)
                page, next_cursor = paginate(
                    composition.commands.descriptors(),
                    cursor=request.cursor,
                    limit=request.limit,
                )
                return CommandListResponse(
                    commands=page,
                    selected_revision=composition.snapshot.selected_revision,
                    active_revision=composition.snapshot.active_revision,
                    supported_operations=sorted(composition.commands.supported_operations),
                    next_cursor=next_cursor,
                ).to_wire()
            if method == "hrns/commands/invoke":
                request = CommandInvokeRequest.model_validate(dict(params))
                await self.authorize(request.session_id, PolicyAction.COMMAND)
                return await self.commands.invoke(request)
            if method == "hrns/commands/cancel":
                request = CommandCancelRequest.model_validate(dict(params))
                await self.authorize(request.session_id, PolicyAction.COMMAND)
                if await self.session_record(request.session_id) is None:
                    raise HrnsError(
                        "SESSION_CLOSED", "Load the session before cancelling a command."
                    )
                return self.commands.cancel(request)
            if method == "hrns/session/access":
                session_id = params.get("sessionId", params.get("session_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HrnsError("INVALID_CONFIGURATION", "sessionId is required.")
                record = await self.authorize(session_id, PolicyAction.ARCHIVE)
                if record.effective_owner != self.acting_principal():
                    raise HrnsError(
                        "POLICY_DENIED",
                        "Only the session owner may change its access mode.",
                        {"sessionId": session_id},
                    )
                raw_access = params.get("access", record.access.value)
                try:
                    access = SessionAccess(raw_access)
                except ValueError as exc:
                    raise HrnsError(
                        "INVALID_CONFIGURATION",
                        "access must be one of private, shared, or archived.",
                        {"value": raw_access},
                    ) from exc
                raw_collaborators = params.get("collaborators", sorted(record.collaborators))
                if not isinstance(raw_collaborators, list) or not all(
                    isinstance(item, str) and item.strip() for item in raw_collaborators
                ):
                    raise HrnsError(
                        "INVALID_CONFIGURATION",
                        "collaborators must be a list of non-empty principal IDs.",
                    )
                collaborators = frozenset(raw_collaborators)
                if not await self.sessions.set_access(
                    session_id,
                    access=access,
                    collaborators=collaborators,
                    principal=self.acting_principal(),
                ):
                    raise HrnsError("SESSION_CLOSED", "Load the session before changing access.")
                return {
                    "sessionId": session_id,
                    "access": access.value,
                    "collaborators": sorted(collaborators),
                }
            if method == "hrns/session/capabilities":
                session_id = params.get("sessionId", params.get("session_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HrnsError("INVALID_CONFIGURATION", "sessionId is required.")
                record = await self.authorize(session_id, PolicyAction.CONFIGURE)
                if record.effective_owner != self.acting_principal():
                    raise HrnsError(
                        "POLICY_DENIED",
                        "Only the session owner may change its capability gate.",
                        {"sessionId": session_id},
                    )
                accepts = params.get("acceptsClientCapabilities")
                if accepts is not None and not isinstance(accepts, bool):
                    raise HrnsError(
                        "INVALID_CONFIGURATION",
                        "acceptsClientCapabilities must be a boolean or null.",
                    )
                if not await self.sessions.set_client_capabilities(
                    session_id, accepts, principal=self.acting_principal()
                ):
                    raise HrnsError("SESSION_CLOSED", "Load the session before changing its gate.")
                return {"sessionId": session_id, "acceptsClientCapabilities": accepts}
            if method == "hrns/queue/list":
                session_id = params.get("sessionId", params.get("session_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HrnsError("INVALID_CONFIGURATION", "sessionId is required.")
                return (await self.list_queued_prompts(session_id)).to_wire()
            if method == "hrns/queue/enqueue":
                session_id = params.get("sessionId", params.get("session_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HrnsError("INVALID_CONFIGURATION", "sessionId is required.")
                raw_prompt = params.get("prompt")
                if not isinstance(raw_prompt, list):
                    raise HrnsError(
                        "INVALID_CONFIGURATION", "prompt must be a list of content blocks."
                    )
                mutation_id = params.get("mutationId", params.get("mutation_id"))
                if (
                    not isinstance(mutation_id, str)
                    or not mutation_id.strip()
                    or len(mutation_id) > 200
                ):
                    raise HrnsError(
                        "INVALID_CONFIGURATION",
                        "mutationId must be a non-empty string of at most 200 characters.",
                    )
                row = await self.enqueue_prompt(
                    session_id,
                    raw_prompt,
                    mutation_id=mutation_id,
                )
                return self._public_queue_update(row).to_wire()
            if method == "hrns/queue/edit":
                request = QueueEditRequest.model_validate(dict(params))
                return (await self.edit_queued_prompt(request)).to_wire()
            if method == "hrns/queue/remove":
                request = QueueRemoveRequest.model_validate(dict(params))
                return (await self.remove_queued_prompt(request)).to_wire()
            if method == "hrns/queue/run":
                session_id = params.get("sessionId", params.get("session_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HrnsError("INVALID_CONFIGURATION", "sessionId is required.")
                rows = await self.run_queued_prompts(session_id)
                return {"sessionId": session_id, "items": [row.to_public() for row in rows]}
            if method == "hrns/queue/retry":
                session_id = params.get("sessionId", params.get("session_id"))
                prompt_id = params.get("promptId", params.get("prompt_id"))
                mutation_id = params.get("mutationId", params.get("mutation_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HrnsError("INVALID_CONFIGURATION", "sessionId is required.")
                if not isinstance(prompt_id, str) or not prompt_id:
                    raise HrnsError("INVALID_CONFIGURATION", "promptId is required.")
                if (
                    not isinstance(mutation_id, str)
                    or not mutation_id.strip()
                    or len(mutation_id) > 200
                ):
                    raise HrnsError(
                        "INVALID_CONFIGURATION",
                        "mutationId must be a non-empty string of at most 200 characters.",
                    )
                return (
                    await self.retry_queued_prompt(
                        session_id,
                        prompt_id,
                        mutation_id=mutation_id,
                    )
                ).to_wire()
            if method == "hrns/presentation/get":
                request = PresentationListRequest.model_validate(dict(params))
                await self.authorize(request.session_id, PolicyAction.LOAD)
                self.sessions.require_composition(request.session_id)
                snapshots = await self.sessions.presentation_snapshots(
                    request.session_id, request.tool_call_ids
                )
                return PresentationListResponse(snapshots=snapshots).to_wire()
            raise RequestError.method_not_found(f"_{method}")
        except HrnsError as exc:
            raise acp_map.hrns_rpc_error(exc) from exc
        except ValidationError as exc:
            raise acp_map.hrns_rpc_error(
                HrnsError(
                    "INVALID_CONFIGURATION",
                    "The extension request payload is invalid.",
                    {"errors": exc.errors()[:10]},
                )
            ) from exc
        except RequestError:
            raise
