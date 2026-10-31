"""Scoped persistence for the upstream ACP session model.

The upstream adapter persists a :class:`StoredSession` (native model messages, the
client-visible update transcript, and the selected model). A store instance is bound to one
authenticated principal and one registered deployment, so a session id from another scope can
never be read or overwritten. Host-owned metadata (cwd, title, effort, selected bindings,
usage totals) lives beside the upstream payload.
"""

from __future__ import annotations

import asyncio
import base64
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from acp import schema
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage

from hrns.protocol import HrnsError, order_session_updates

from .creation import SessionCreationOperation
from .session_policy import SessionAccess

_CreationList = list[SessionCreationOperation]

if TYPE_CHECKING:
    from pydantic_ai_harness.experimental.acp import StoredSession

__all__ = [
    "SESSION_COLUMNS",
    "DeploymentRecord",
    "LIFECYCLE_OPERATION_KINDS",
    "LIFECYCLE_PENDING_STATES",
    "OperationKind",
    "PromptState",
    "QueuedPrompt",
    "SessionLease",
    "SessionPage",
    "SessionRecord",
    "SessionStore",
    "ScopedSessionStore",
    "decode_session",
    "decode_session_cursor",
    "deserialize_collaborators",
    "encode_session",
    "encode_session_cursor",
    "now_ms",
    "serialize_collaborators",
]

#: Metadata projection shared by both stores. Session bodies are only read by ``load_scoped``.
SESSION_COLUMNS = (
    "session_id, principal, deployment_id, agent_id, definition_version, cwd, title, effort, "
    "bindings, usage, model, access, owner, collaborators, client_capabilities, created_at, "
    "last_active_at"
)

_MESSAGES = TypeAdapter(list[ModelMessage])

#: Priced amount for a usage total, kept as a decimal string so JSON round-trips exactly.
USAGE_COST = "cost"
#: Display status derived in :func:`finalize_usage`.
USAGE_STATUS = "cost_status"
#: Counter an earlier build wrote next to the price; dropped on merge and on emit.
_LEGACY_UNPRICED = "unpriced_requests"

#: Public ``session/update`` payload classes, keyed by their discriminator value.
_UPDATE_TYPES: dict[str, type[Any]] = {
    "user_message_chunk": schema.UserMessageChunk,
    "agent_message_chunk": schema.AgentMessageChunk,
    "agent_thought_chunk": schema.AgentThoughtChunk,
    "tool_call": schema.ToolCallStart,
    "tool_call_update": schema.ToolCallProgress,
    "plan_update": schema.AgentPlanUpdate,
    "available_commands_update": schema.AvailableCommandsUpdate,
    "current_mode_update": schema.CurrentModeUpdate,
    "config_option_update": schema.ConfigOptionUpdate,
    "session_info_update": schema.SessionInfoUpdate,
    "usage_update": schema.UsageUpdate,
}


def now_ms() -> int:
    """Wall-clock milliseconds used for host metadata ordering."""
    import time

    return time.time_ns() // 1_000_000


def encode_session(session: "StoredSession") -> tuple[str, str, str | None]:
    """Serialize an upstream stored session into JSON columns."""
    messages = _MESSAGES.dump_json(list(session.messages)).decode("utf-8")
    ordered_updates = order_session_updates(session.updates)
    updates = json.dumps(
        [
            {
                "type": update.session_update,
                "data": update.model_dump(mode="json", by_alias=True, exclude_none=True),
            }
            for update in ordered_updates
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return messages, updates, session.model


def decode_session(messages: str, updates: str, model: str | None) -> "StoredSession":
    """Deserialize an upstream stored session from its JSON columns."""
    from pydantic_ai_harness.experimental.acp import StoredSession

    parsed_messages = _MESSAGES.validate_json(messages or "[]")
    raw_updates = json.loads(updates or "[]")
    parsed_updates: list[Any] = []
    if not isinstance(raw_updates, list):
        raise ValueError("stored updates must be a list")
    for entry in raw_updates:
        if not isinstance(entry, dict):
            raise ValueError("stored update entries must be objects")
        update_type = entry.get("type")
        data = entry.get("data")
        cls = _UPDATE_TYPES.get(str(update_type))
        if cls is None or not isinstance(data, dict):
            raise ValueError(f"unsupported stored update type {update_type!r}")
        parsed_updates.append(cls.model_validate(data))
    return StoredSession(
        messages=parsed_messages,
        updates=order_session_updates(parsed_updates),
        model=model,
    )


def encode_session_cursor(record: SessionRecord) -> str:
    """Opaque keyset cursor for ``(last_active_at DESC, session_id ASC)``."""
    payload = {"v": 2, "last": record.last_active_at, "id": record.session_id}
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii")


def decode_session_cursor(cursor: str) -> tuple[int, str]:
    """Return the ``(last_active_at, session_id)`` position named by *cursor*."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        last = decoded["last"]
        session_id = decoded["id"]
        if decoded.get("v") != 2 or type(last) is not int or not isinstance(session_id, str):
            raise ValueError("invalid cursor")
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise HrnsError("INVALID_CONFIGURATION", "Invalid session pagination cursor") from exc
    return last, session_id


class PromptState(StrEnum):
    """Lifecycle of one durable operation in the per-session queue."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class OperationKind(StrEnum):
    """Kinds of work sharing the authoritative per-session order."""

    PROMPT = "prompt"
    CONFIGURATION = "configuration"
    CONFIG_OPTION = "config_option"
    CLOSE = "close"
    DELETE = "delete"


LIFECYCLE_OPERATION_KINDS: frozenset[OperationKind] = frozenset(
    {OperationKind.CLOSE, OperationKind.DELETE}
)
LIFECYCLE_PENDING_STATES: frozenset[PromptState] = frozenset(
    {
        PromptState.QUEUED,
        PromptState.CLAIMED,
        PromptState.FAILED,
        PromptState.UNCERTAIN,
    }
)


@dataclass(frozen=True, slots=True)
class SessionLease:
    """A fenced, expiring grant to be the only writer for one session."""

    session_id: str
    principal: str
    deployment_id: str
    owner_id: str
    fence: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class SessionPage:
    """One page of session metadata plus the cursor for the next page."""

    records: list[SessionRecord]
    next_cursor: str | None = None


@dataclass
class SessionRecord:
    """Host-owned metadata for one ACP session."""

    session_id: str
    principal: str
    deployment_id: str
    agent_id: str
    definition_version: str
    cwd: str
    title: str | None = None
    effort: str | None = None
    model: str | None = None
    bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    access: SessionAccess = SessionAccess.PRIVATE
    owner: str | None = None
    collaborators: frozenset[str] = frozenset()
    accepts_client_capabilities: bool | None = None
    created_at: int = 0
    last_active_at: int = 0

    @property
    def effective_owner(self) -> str:
        """The principal that owns the session; older rows fall back to their creator."""
        return self.owner or self.principal

    @property
    def read_only(self) -> bool:
        return self.access is SessionAccess.ARCHIVED

    def admits(self, principal: str) -> bool:
        """Whether ``principal`` may load or replay this session."""
        if principal == self.effective_owner:
            return True
        if self.access is SessionAccess.PRIVATE:
            return False
        return principal in self.collaborators


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    """Durable metadata for one registered ``(agent_id, definition_version)`` revision.

    Only the descriptor is persisted: the agent definition itself can hold native
    objects, so the application re-registers it and this row just records that the
    revision existed, whether it is visible, and whether it is retained.
    """

    agent_id: str
    version: str
    name: str
    description: str
    visible: bool
    retained: bool
    registered_at: int


#: Return type of ``list_deployments``; aliased because the store classes define ``list``.
_DeploymentList = list[DeploymentRecord]


@dataclass(frozen=True, slots=True)
class QueuedPrompt:
    """One durably ordered session operation.

    The historic name is retained because prompt queue records are part of the public
    runtime API. ``kind`` distinguishes prompts from configuration mutations.
    """

    prompt_id: str
    session_id: str
    principal: str
    deployment_id: str
    credential_id: str | None
    connection_id: str | None
    sequence: int
    kind: OperationKind
    payload: str
    state: PromptState
    attempt: int
    parent_id: str | None
    owner_id: str | None
    fence: int | None
    update_id: str
    revision: int
    created_at: int
    updated_at: int

    def to_public(self) -> dict[str, object]:
        """Render the prompt as a transport-friendly dict."""
        return {
            "prompt_id": self.prompt_id,
            "session_id": self.session_id,
            "principal": self.principal,
            "deployment_id": self.deployment_id,
            "credential_id": self.credential_id,
            "connection_id": self.connection_id,
            "sequence": self.sequence,
            "kind": self.kind.value,
            "payload": self.payload,
            "state": self.state.value,
            "attempt": self.attempt,
            "parent_id": self.parent_id,
            "owner_id": self.owner_id,
            "fence": self.fence,
            "update_id": self.update_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class QueueMutationResult:
    """The durable result of one idempotent client queue mutation."""

    item: QueuedPrompt
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class PromptQueueSnapshot:
    """A complete prompt queue read at one durable session revision."""

    revision: int
    items: list[QueuedPrompt]


#: Return type of ``list_prompts``. Aliased because the store classes define a ``list``
#: method, which shadows the builtin inside their bodies.
_PromptList = list[QueuedPrompt]


def serialize_collaborators(collaborators: frozenset[str] | set[str]) -> str:
    """Encode the collaborator set for a text column, deterministically."""
    return json.dumps(sorted(collaborators), ensure_ascii=False, separators=(",", ":"))


def deserialize_collaborators(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("stored collaborators must be a list")
    return frozenset(str(item) for item in parsed)


def _session_conflict(deployment_id: str, session_id: str, fence: int) -> HrnsError:
    """The error raised when a fenced write finds the lease moved on."""
    return HrnsError(
        "SESSION_CONFLICT",
        "The session lease was lost; refusing to overwrite a newer actor's state.",
        {"deployment_id": deployment_id, "session_id": session_id, "fence": fence},
    )


class SessionStore(ABC):
    """Durable store shared by SQLite and PostgreSQL backends.

    ``save_scoped``/``load_scoped`` implement the upstream ``SessionStore`` protocol through
    :class:`ScopedSessionStore`; the metadata methods back list/delete/title/usage.
    """

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def create(self, record: SessionRecord) -> None: ...

    @abstractmethod
    async def get(
        self, principal: str, deployment_id: str, session_id: str
    ) -> SessionRecord | None: ...

    @abstractmethod
    async def list(
        self,
        principal: str,
        deployment_id: str,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SessionPage: ...

    @abstractmethod
    async def delete(self, principal: str, deployment_id: str, session_id: str) -> bool: ...

    @abstractmethod
    async def update(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        *,
        title: str | None = None,
        effort: str | None = None,
        bindings: Mapping[str, Mapping[str, Any]] | None = None,
        usage: Mapping[str, Any] | None = None,
        touch: bool = False,
    ) -> None: ...

    @abstractmethod
    async def set_access(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        *,
        access: SessionAccess,
        owner: str | None = None,
        collaborators: frozenset[str] | None = None,
    ) -> bool:
        """Change a session's access mode; the caller must already own it."""

    @abstractmethod
    async def set_client_capabilities(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        accepts: bool | None,
    ) -> bool:
        """Override (or clear, with ``None``) the deployment's capability gate."""

    @abstractmethod
    async def get_visible(
        self, deployment_id: str, session_id: str, principal: str
    ) -> SessionRecord | None:
        """Read a session visible to ``principal`` under its own access mode."""

    @abstractmethod
    async def list_visible(
        self,
        deployment_id: str,
        principal: str,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SessionPage:
        """List sessions visible to ``principal`` under each session's access mode."""

    @abstractmethod
    async def save_scoped(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        session: StoredSession,
        *,
        fence: int | None = None,
    ) -> None: ...

    @abstractmethod
    async def load_scoped(
        self, principal: str, deployment_id: str, session_id: str
    ) -> StoredSession | None: ...

    @abstractmethod
    async def reserve_update_revisions(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        count: int,
        *,
        fence: int | None = None,
    ) -> int:
        """Durably reserve ``count`` revisions and return the first one."""

    # -- Cross-worker session leases -----------------------------------------

    @abstractmethod
    async def acquire_lease(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        ttl_ms: int,
    ) -> SessionLease | None:
        """Take or renew the writer lease; ``None`` when a live other owner holds it."""

    @abstractmethod
    async def renew_lease(
        self,
        principal: str,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        ttl_ms: int,
    ) -> SessionLease | None:
        """Extend a lease that this owner still holds and that has not expired."""

    @abstractmethod
    async def release_lease(
        self, principal: str, deployment_id: str, session_id: str, owner_id: str
    ) -> bool:
        """Drop the lease, but only when ``owner_id`` still holds it."""

    @abstractmethod
    async def get_lease(self, deployment_id: str, session_id: str) -> SessionLease | None: ...

    # -- Durable per-session prompt queue ------------------------------------

    @abstractmethod
    async def enqueue_prompt(self, prompt: QueuedPrompt) -> QueuedPrompt:
        """Append a prompt, assigning the next per-session ``sequence``."""

    @abstractmethod
    async def enqueue_prompt_mutation(
        self,
        prompt: QueuedPrompt,
        *,
        mutation_id: str,
        request_hash: str,
    ) -> QueueMutationResult:
        """Idempotently append a client-requested prompt."""

    @abstractmethod
    async def edit_prompt(
        self,
        deployment_id: str,
        session_id: str,
        prompt_id: str,
        *,
        expected_revision: int,
        payload: str,
        mutation_id: str,
        request_hash: str,
        update_id: str,
    ) -> QueueMutationResult:
        """Atomically replace one queued prompt's complete ACP payload."""

    @abstractmethod
    async def remove_prompt(
        self,
        deployment_id: str,
        session_id: str,
        prompt_id: str,
        *,
        expected_revision: int,
        mutation_id: str,
        request_hash: str,
        update_id: str,
    ) -> QueueMutationResult:
        """Atomically transition one queued prompt to a cancelled tombstone."""

    @abstractmethod
    async def claim_next_prompt(
        self,
        deployment_id: str,
        session_id: str,
        owner_id: str,
        fence: int,
        ttl_ms: int,
    ) -> QueuedPrompt | None:
        """Claim the earliest queued operation.

        An earlier unresolved claim blocks later work until recovery marks it uncertain.
        """

    @abstractmethod
    async def claim_prompt(self, prompt_id: str, owner_id: str, fence: int) -> QueuedPrompt | None:
        """Claim one specific queued operation."""

    @abstractmethod
    async def finish_prompt(
        self,
        prompt_id: str,
        state: PromptState,
        *,
        owner_id: str,
        fence: int,
    ) -> QueuedPrompt | None:
        """Move a claimed prompt to a terminal state; ``None`` when it was not ours."""

    @abstractmethod
    async def get_prompt(self, prompt_id: str) -> QueuedPrompt | None: ...

    @abstractmethod
    async def list_prompts(
        self,
        deployment_id: str,
        session_id: str,
        *,
        states: Sequence[PromptState] | None = None,
        limit: int = 100,
    ) -> _PromptList:
        """List a session's prompts in queue order."""

    @abstractmethod
    async def queue_snapshot(
        self,
        deployment_id: str,
        session_id: str,
    ) -> PromptQueueSnapshot:
        """Read every public prompt item and the exact queue revision atomically."""

    @abstractmethod
    async def mark_uncertain_prompts(
        self,
        owner_id: str,
        *,
        deployment_id: str | None = None,
        session_id: str | None = None,
    ) -> _PromptList:
        """Park claims held by a worker that lost a lease as ``UNCERTAIN``.

        ``session_id`` narrows the operation to one session; without it every claim of
        that owner in the deployment is affected.
        """

    @abstractmethod
    async def retry_prompt(
        self,
        prompt_id: str,
        *,
        session_id: str,
        new_prompt_id: str,
        owner_id: str,
        fence: int,
        mutation_id: str,
        request_hash: str,
    ) -> QueueMutationResult | None:
        """Idempotently re-queue an ``UNCERTAIN`` prompt as a fresh linked row."""

    @abstractmethod
    async def begin_lifecycle_operation(
        self,
        operation: QueuedPrompt,
        *,
        reuse_completed: bool,
    ) -> QueuedPrompt:
        """Create or reuse one durable close/delete intent for a session."""

    @abstractmethod
    async def get_lifecycle_operation(
        self,
        deployment_id: str,
        session_id: str,
        kind: OperationKind,
        *,
        principal: str | None = None,
    ) -> QueuedPrompt | None:
        """Return the latest lifecycle operation matching the requested intent."""

    @abstractmethod
    async def list_pending_lifecycle_operations(self) -> _PromptList:
        """Return every retryable close/delete operation in durable order."""

    @abstractmethod
    async def claim_lifecycle_operation(
        self,
        prompt_id: str,
        owner_id: str,
        fence: int,
    ) -> QueuedPrompt | None:
        """Claim or reclaim a retryable close/delete operation."""

    @abstractmethod
    async def cancel_queued_operations(
        self,
        deployment_id: str,
        session_id: str,
        *,
        exclude_prompt_id: str,
    ) -> _PromptList:
        """Cancel queued non-lifecycle work after lifecycle admission has stopped."""

    @abstractmethod
    async def settle_lifecycle_operation(
        self,
        prompt_id: str,
        state: PromptState,
        *,
        owner_id: str,
        fence: int,
        delete_session: bool,
    ) -> bool:
        """Atomically settle lifecycle state, release ownership, and optionally delete."""

    # -- Deployment revision metadata ----------------------------------------

    @abstractmethod
    async def upsert_deployment(self, record: DeploymentRecord) -> None:
        """Insert or replace the metadata of one revision."""

    @abstractmethod
    async def list_deployments(self) -> _DeploymentList:
        """List every persisted revision ordered by agent, registration, and version."""

    @abstractmethod
    async def set_deployment_visible(self, agent_id: str, version: str, visible: bool) -> bool:
        """Hide or show one revision; ``False`` when the row does not exist."""

    @abstractmethod
    async def set_deployment_retained(self, agent_id: str, version: str, retained: bool) -> bool:
        """Change whether a revision is protected from deletion."""

    @abstractmethod
    async def delete_deployment(self, agent_id: str, version: str) -> bool:
        """Delete an unretained revision; ``False`` when absent or retained."""

    def scoped(self, principal: str, deployment_id: str) -> ScopedSessionStore:
        return ScopedSessionStore(self, principal, deployment_id)

    @abstractmethod
    async def reserve_creation(
        self, operation: SessionCreationOperation
    ) -> SessionCreationOperation:
        """Atomically reserve a principal-scoped intent or replay/conflict."""

    @abstractmethod
    async def get_creation(
        self, principal: str, mutation_id: str
    ) -> SessionCreationOperation | None: ...

    @abstractmethod
    async def list_pending_creations(self) -> _CreationList: ...

    @abstractmethod
    async def claim_creation(
        self, principal: str, mutation_id: str, owner_id: str, ttl_ms: int
    ) -> SessionCreationOperation | None:
        """Claim an unowned/expired operation, advancing its fence."""

    @abstractmethod
    async def update_creation(self, operation: SessionCreationOperation) -> None:
        """Fenced state transition/renewal; must reject an expired or superseded owner."""

    @abstractmethod
    async def commit_creation(
        self,
        operation: SessionCreationOperation,
        record: SessionRecord,
        session: StoredSession,
        lease: SessionLease | None = None,
    ) -> None:
        """Atomically publish the complete session and successful creation result."""


class ScopedSessionStore:
    """The upstream ``SessionStore`` protocol for exactly one principal/deployment scope.

    A session that ``session/new`` only composed has no row yet: its metadata is staged here and
    the row is written the first time the session is used — when its first turn is persisted, or
    when the host materializes it explicitly (a rename). An unused session therefore leaves
    nothing behind, and a turn can never be lost to a row that was staged but never written.
    """

    def __init__(self, backend: SessionStore, principal: str, deployment_id: str) -> None:
        self.backend = backend
        self.principal = principal
        self.deployment_id = deployment_id
        self._staged: dict[str, SessionRecord] = {}
        self._materialize_lock = asyncio.Lock()
        self._fences: dict[str, int] = {}
        self._revision_lock = asyncio.Lock()
        self._revision_blocks: dict[str, tuple[int, int, int | None]] = {}

    def stage(self, record: SessionRecord) -> None:
        """Hold a composed session's metadata in memory until the session is first used."""
        if record.deployment_id != self.deployment_id:
            raise HrnsError(
                "POLICY_DENIED",
                "A scoped store can only stage sessions in its own deployment.",
                details={"session_id": record.session_id},
            )
        self._staged[record.session_id] = record

    def staged(self, session_id: str) -> SessionRecord | None:
        """The record of a session that is open but not yet written, if any."""
        return self._staged.get(session_id)

    def discard(self, session_id: str) -> None:
        self._staged.pop(session_id, None)

    async def materialize(self, session_id: str) -> bool:
        """Write the staged record; ``False`` when the session was never staged."""
        async with self._materialize_lock:
            record = self._staged.get(session_id)
            if record is None:
                return False
            await self.backend.create(record)
            self._staged.pop(session_id, None)
            return True

    def set_fence(self, session_id: str, fence: int | None) -> None:
        """Bind this session's writes to the lease fence the actor holds."""
        previous = self._fences.get(session_id)
        if fence is None:
            self._fences.pop(session_id, None)
        else:
            self._fences[session_id] = fence
        if previous != fence:
            self._revision_blocks.pop(session_id, None)

    def fence(self, session_id: str) -> int | None:
        return self._fences.get(session_id)

    async def save(self, session_id: str, session: StoredSession) -> None:
        if not (session.messages or session.updates) and self.staged(session_id) is not None:
            # ``session/new`` persists an empty session so a client could reopen it before the
            # first turn. That convenience is what filled history with unused sessions, so a
            # staged session waits for real content before it gets a row.
            return
        # The first content save is also the moment a staged session becomes durable.
        await self.materialize(session_id)
        await self.backend.save_scoped(
            self.principal,
            self.deployment_id,
            session_id,
            session,
            fence=self.fence(session_id),
        )

    async def load(self, session_id: str) -> StoredSession | None:
        return await self.backend.load_scoped(self.principal, self.deployment_id, session_id)

    async def reserve_update_revisions(self, session_id: str, count: int = 1) -> range:
        """Reserve non-reusable revisions, amortizing durable reservations in blocks."""
        if count < 1:
            raise ValueError("update revision reservation count must be positive")
        async with self._revision_lock:
            fence = self.fence(session_id)
            block = self._revision_blocks.get(session_id)
            if block is not None:
                next_revision, end_revision, block_fence = block
                if block_fence == fence and end_revision - next_revision >= count:
                    self._revision_blocks[session_id] = (
                        next_revision + count,
                        end_revision,
                        fence,
                    )
                    return range(next_revision, next_revision + count)

            reservation_size = max(128, count)
            first = await self.backend.reserve_update_revisions(
                self.principal,
                self.deployment_id,
                session_id,
                reservation_size,
                fence=fence,
            )
            self._revision_blocks[session_id] = (
                first + count,
                first + reservation_size,
                fence,
            )
            return range(first, first + count)

    async def get(self, session_id: str) -> SessionRecord | None:
        return await self.backend.get(self.principal, self.deployment_id, session_id)

    async def list(
        self,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SessionPage:
        return await self.backend.list(
            self.principal, self.deployment_id, cwd=cwd, cursor=cursor, limit=limit
        )

    async def delete(self, session_id: str) -> bool:
        return await self.backend.delete(self.principal, self.deployment_id, session_id)

    async def get_visible(self, session_id: str, principal: str) -> SessionRecord | None:
        return await self.backend.get_visible(self.deployment_id, session_id, principal)

    async def list_visible(
        self,
        principal: str,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SessionPage:
        return await self.backend.list_visible(
            self.deployment_id,
            principal,
            cwd=cwd,
            cursor=cursor,
            limit=limit,
        )

    async def set_access(
        self,
        session_id: str,
        *,
        access: SessionAccess,
        owner: str | None = None,
        collaborators: frozenset[str] | None = None,
        principal: str | None = None,
    ) -> bool:
        return await self.backend.set_access(
            principal or self.principal,
            self.deployment_id,
            session_id,
            access=access,
            owner=owner,
            collaborators=collaborators,
        )

    async def set_client_capabilities(
        self, session_id: str, accepts: bool | None, *, principal: str | None = None
    ) -> bool:
        return await self.backend.set_client_capabilities(
            principal or self.principal, self.deployment_id, session_id, accepts
        )

    async def acquire_lease(
        self, session_id: str, owner_id: str, ttl_ms: int
    ) -> SessionLease | None:
        return await self.backend.acquire_lease(
            self.principal, self.deployment_id, session_id, owner_id, ttl_ms
        )

    async def renew_lease(self, session_id: str, owner_id: str, ttl_ms: int) -> SessionLease | None:
        return await self.backend.renew_lease(
            self.principal, self.deployment_id, session_id, owner_id, ttl_ms
        )

    async def release_lease(self, session_id: str, owner_id: str) -> bool:
        return await self.backend.release_lease(
            self.principal, self.deployment_id, session_id, owner_id
        )

    async def get_lease(self, session_id: str) -> SessionLease | None:
        return await self.backend.get_lease(self.deployment_id, session_id)

    async def enqueue_prompt(self, prompt: QueuedPrompt) -> QueuedPrompt:
        return await self.backend.enqueue_prompt(prompt)

    async def enqueue_prompt_mutation(
        self,
        prompt: QueuedPrompt,
        *,
        mutation_id: str,
        request_hash: str,
    ) -> QueueMutationResult:
        return await self.backend.enqueue_prompt_mutation(
            prompt,
            mutation_id=mutation_id,
            request_hash=request_hash,
        )

    async def edit_prompt(
        self,
        session_id: str,
        prompt_id: str,
        *,
        expected_revision: int,
        payload: str,
        mutation_id: str,
        request_hash: str,
        update_id: str,
    ) -> QueueMutationResult:
        return await self.backend.edit_prompt(
            self.deployment_id,
            session_id,
            prompt_id,
            expected_revision=expected_revision,
            payload=payload,
            mutation_id=mutation_id,
            request_hash=request_hash,
            update_id=update_id,
        )

    async def remove_prompt(
        self,
        session_id: str,
        prompt_id: str,
        *,
        expected_revision: int,
        mutation_id: str,
        request_hash: str,
        update_id: str,
    ) -> QueueMutationResult:
        return await self.backend.remove_prompt(
            self.deployment_id,
            session_id,
            prompt_id,
            expected_revision=expected_revision,
            mutation_id=mutation_id,
            request_hash=request_hash,
            update_id=update_id,
        )

    async def claim_prompt(self, prompt_id: str, owner_id: str, fence: int) -> QueuedPrompt | None:
        return await self.backend.claim_prompt(prompt_id, owner_id, fence)

    async def claim_next_prompt(
        self, session_id: str, owner_id: str, fence: int, ttl_ms: int
    ) -> QueuedPrompt | None:
        return await self.backend.claim_next_prompt(
            self.deployment_id, session_id, owner_id, fence, ttl_ms
        )

    async def finish_prompt(
        self, prompt_id: str, state: PromptState, *, owner_id: str, fence: int
    ) -> QueuedPrompt | None:
        return await self.backend.finish_prompt(prompt_id, state, owner_id=owner_id, fence=fence)

    async def get_prompt(self, prompt_id: str) -> QueuedPrompt | None:
        return await self.backend.get_prompt(prompt_id)

    async def list_prompts(
        self,
        session_id: str,
        *,
        states: Sequence[PromptState] | None = None,
        limit: int = 100,
    ) -> _PromptList:
        return await self.backend.list_prompts(
            self.deployment_id, session_id, states=states, limit=limit
        )

    async def queue_snapshot(self, session_id: str) -> PromptQueueSnapshot:
        return await self.backend.queue_snapshot(self.deployment_id, session_id)

    async def mark_uncertain_prompts(
        self, owner_id: str, *, session_id: str | None = None
    ) -> _PromptList:
        return await self.backend.mark_uncertain_prompts(
            owner_id,
            deployment_id=self.deployment_id,
            session_id=session_id,
        )

    async def retry_prompt(
        self,
        prompt_id: str,
        *,
        session_id: str,
        new_prompt_id: str,
        owner_id: str,
        fence: int,
        mutation_id: str,
        request_hash: str,
    ) -> QueueMutationResult | None:
        return await self.backend.retry_prompt(
            prompt_id,
            session_id=session_id,
            new_prompt_id=new_prompt_id,
            owner_id=owner_id,
            fence=fence,
            mutation_id=mutation_id,
            request_hash=request_hash,
        )

    async def begin_lifecycle_operation(
        self,
        operation: QueuedPrompt,
        *,
        reuse_completed: bool,
    ) -> QueuedPrompt:
        return await self.backend.begin_lifecycle_operation(
            operation,
            reuse_completed=reuse_completed,
        )

    async def get_lifecycle_operation(
        self,
        session_id: str,
        kind: OperationKind,
        *,
        principal: str | None = None,
    ) -> QueuedPrompt | None:
        return await self.backend.get_lifecycle_operation(
            self.deployment_id,
            session_id,
            kind,
            principal=principal,
        )

    async def list_pending_lifecycle_operations(self) -> _PromptList:
        return [
            operation
            for operation in await self.backend.list_pending_lifecycle_operations()
            if operation.deployment_id == self.deployment_id
        ]

    async def claim_lifecycle_operation(
        self,
        prompt_id: str,
        owner_id: str,
        fence: int,
    ) -> QueuedPrompt | None:
        return await self.backend.claim_lifecycle_operation(prompt_id, owner_id, fence)

    async def cancel_queued_operations(
        self,
        session_id: str,
        *,
        exclude_prompt_id: str,
    ) -> _PromptList:
        return await self.backend.cancel_queued_operations(
            self.deployment_id,
            session_id,
            exclude_prompt_id=exclude_prompt_id,
        )

    async def settle_lifecycle_operation(
        self,
        prompt_id: str,
        state: PromptState,
        *,
        owner_id: str,
        fence: int,
        delete_session: bool,
    ) -> bool:
        return await self.backend.settle_lifecycle_operation(
            prompt_id,
            state,
            owner_id=owner_id,
            fence=fence,
            delete_session=delete_session,
        )

    async def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        effort: str | None = None,
        bindings: Mapping[str, Mapping[str, Any]] | None = None,
        usage: Mapping[str, Any] | None = None,
        touch: bool = False,
    ) -> None:
        await self.backend.update(
            self.principal,
            self.deployment_id,
            session_id,
            title=title,
            effort=effort,
            bindings=bindings,
            usage=usage,
            touch=touch,
        )


def merge_usage(current: Mapping[str, Any], increment: Mapping[str, Any]) -> dict[str, Any]:
    """Add two usage totals without dropping unknown provider counters or the price."""
    # `cost_status` is derived on emit, and an earlier build also wrote an
    # `unpriced_requests` counter that no longer means anything; drop both.
    total = dict(current)
    total.pop(USAGE_STATUS, None)
    total.pop(_LEGACY_UNPRICED, None)
    for key, value in increment.items():
        name = key
        if name in (USAGE_STATUS, _LEGACY_UNPRICED):
            continue
        if name == USAGE_COST:
            cost = _add_costs(total.get(USAGE_COST), value)
            if cost is None:
                total.pop(USAGE_COST, None)
            else:
                total[USAGE_COST] = cost
            continue
        try:
            total[name] = int(total.get(name, 0)) + int(value)
        except (TypeError, ValueError):
            # Older stores kept non-counter payloads (e.g. per-tool details) alongside
            # counters; carry them through untouched instead of failing the turn.
            total[name] = value
    return total


def serialize_bindings(bindings: Mapping[str, Mapping[str, Any]]) -> str:
    return json.dumps(dict(bindings), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def deserialize_bindings(value: str | None) -> dict[str, dict[str, Any]]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("stored bindings must be an object")
    return {
        str(key): dict(item) if isinstance(item, Mapping) else {} for key, item in parsed.items()
    }


def usage_payload(usage: Any) -> dict[str, Any]:
    """Serialize an upstream ``RunUsage``/ACP usage object into plain counters and its price.

    ``cost`` is kept as a decimal string, and only when the provider path priced the response.
    A missing ``cost`` means "not priced", never "free", which is what
    :func:`finalize_usage` turns into the status the client renders.
    """
    if usage is None:
        return {}
    result: dict[str, Any] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "requests",
        "tool_calls",
    ):
        value = getattr(usage, name, None)
        if name in {"cache_read_tokens", "cache_write_tokens"} and value is None:
            value = getattr(usage, name.replace("cache_", "cached_"), None)
        if isinstance(value, int) and not isinstance(value, bool):
            result[name] = value
    cost = _cost_text(getattr(usage, USAGE_COST, None))
    if cost is not None:
        result[USAGE_COST] = cost
    return result


def finalize_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Add the price status the client renders next to the amount."""
    payload = dict(usage)
    payload.pop(_LEGACY_UNPRICED, None)
    cost = _cost_text(payload.pop(USAGE_COST, None))
    if cost is not None:
        payload[USAGE_COST] = cost
    payload[USAGE_STATUS] = "estimated" if cost is not None else "unavailable"
    return payload


def _cost_text(value: Any) -> str | None:
    """Render a priced amount as a stable decimal string, rejecting NaN/inf and junk."""
    if value is None:
        return None
    try:
        cost = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    if not cost.is_finite():
        return None
    text = format(cost, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _add_costs(left: Any, right: Any) -> str | None:
    """Add two priced amounts exactly, keeping the decimal string form."""
    total = Decimal(0)
    priced = False
    for value in (left, right):
        text = _cost_text(value)
        if text is not None:
            total += Decimal(text)
            priced = True
    return _cost_text(total) if priced else None
