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
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from acp import schema
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage

from hrns.protocol import HrnsError

if TYPE_CHECKING:
    from pydantic_ai_harness.experimental.acp import StoredSession

__all__ = [
    "SESSION_COLUMNS",
    "SessionPage",
    "SessionRecord",
    "SessionStore",
    "ScopedSessionStore",
    "decode_session",
    "decode_session_cursor",
    "encode_session",
    "encode_session_cursor",
    "now_ms",
]

#: Metadata projection shared by both stores. Session bodies are only read by ``load_scoped``.
SESSION_COLUMNS = (
    "session_id, principal, deployment_id, agent_id, definition_version, cwd, title, effort, "
    "bindings, usage, model, created_at, last_active_at"
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
    updates = json.dumps(
        [
            {
                "type": update.session_update,
                "data": update.model_dump(mode="json", by_alias=True, exclude_none=True),
            }
            for update in session.updates
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
    return StoredSession(messages=parsed_messages, updates=parsed_updates, model=model)


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
    created_at: int = 0
    last_active_at: int = 0


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
    async def save_scoped(
        self, principal: str, deployment_id: str, session_id: str, session: StoredSession
    ) -> None: ...

    @abstractmethod
    async def load_scoped(
        self, principal: str, deployment_id: str, session_id: str
    ) -> StoredSession | None: ...

    def scoped(self, principal: str, deployment_id: str) -> ScopedSessionStore:
        return ScopedSessionStore(self, principal, deployment_id)


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

    def stage(self, record: SessionRecord) -> None:
        """Hold a composed session's metadata in memory until the session is first used."""
        if record.principal != self.principal or record.deployment_id != self.deployment_id:
            raise HrnsError(
                "POLICY_DENIED",
                "A scoped store can only stage sessions in its own scope.",
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

    async def save(self, session_id: str, session: StoredSession) -> None:
        if not (session.messages or session.updates) and self.staged(session_id) is not None:
            # ``session/new`` persists an empty session so a client could reopen it before the
            # first turn. That convenience is what filled history with unused sessions, so a
            # staged session waits for real content before it gets a row.
            return
        # The first content save is also the moment a staged session becomes durable.
        await self.materialize(session_id)
        await self.backend.save_scoped(self.principal, self.deployment_id, session_id, session)

    async def load(self, session_id: str) -> StoredSession | None:
        return await self.backend.load_scoped(self.principal, self.deployment_id, session_id)

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
