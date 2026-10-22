"""Scoped persistence for the upstream ACP session model.

The upstream adapter persists a :class:`StoredSession` (native model messages, the
client-visible update transcript, and the selected model). A store instance is bound to one
authenticated principal and one registered deployment, so a session id from another scope can
never be read or overwritten. Host-owned metadata (cwd, title, effort, selected bindings,
usage totals) lives beside the upstream payload.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from acp import schema
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage

from harness.protocol import HarnessError

if TYPE_CHECKING:
    from pydantic_ai_harness.experimental.acp import StoredSession

__all__ = [
    "SessionRecord",
    "SessionStore",
    "ScopedSessionStore",
    "decode_session",
    "encode_session",
    "now_ms",
]

_MESSAGES = TypeAdapter(list[ModelMessage])

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
    usage: dict[str, int] = field(default_factory=dict)
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
    async def list(self, principal: str, deployment_id: str) -> list[SessionRecord]: ...

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
        usage: Mapping[str, int] | None = None,
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
    """The upstream ``SessionStore`` protocol for exactly one principal/deployment scope."""

    def __init__(self, backend: SessionStore, principal: str, deployment_id: str) -> None:
        self.backend = backend
        self.principal = principal
        self.deployment_id = deployment_id

    async def save(self, session_id: str, session: StoredSession) -> None:
        await self.backend.save_scoped(self.principal, self.deployment_id, session_id, session)

    async def load(self, session_id: str) -> StoredSession | None:
        return await self.backend.load_scoped(self.principal, self.deployment_id, session_id)

    async def create(self, record: SessionRecord) -> None:
        if record.principal != self.principal or record.deployment_id != self.deployment_id:
            raise HarnessError(
                "POLICY_DENIED",
                "A scoped store can only create sessions in its own scope.",
                details={"session_id": record.session_id},
            )
        await self.backend.create(record)

    async def get(self, session_id: str) -> SessionRecord | None:
        return await self.backend.get(self.principal, self.deployment_id, session_id)

    async def list(self) -> list[SessionRecord]:
        return await self.backend.list(self.principal, self.deployment_id)

    async def delete(self, session_id: str) -> bool:
        return await self.backend.delete(self.principal, self.deployment_id, session_id)

    async def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        effort: str | None = None,
        bindings: Mapping[str, Mapping[str, Any]] | None = None,
        usage: Mapping[str, int] | None = None,
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


def record_from_mapping(data: Mapping[str, Any]) -> SessionRecord:
    """Build a record from untrusted JSON (used by stored bindings)."""
    return SessionRecord(
        session_id=str(data["session_id"]),
        principal=str(data["principal"]),
        deployment_id=str(data["deployment_id"]),
        agent_id=str(data["agent_id"]),
        definition_version=str(data["definition_version"]),
        cwd=str(data["cwd"]),
        title=data.get("title") if isinstance(data.get("title"), str) else None,
        effort=data.get("effort") if isinstance(data.get("effort"), str) else None,
        bindings=dict(data.get("bindings") or {}),
        usage=dict(data.get("usage") or {}),
    )


def merge_usage(current: Mapping[str, int], increment: Mapping[str, int]) -> dict[str, int]:
    """Add two usage totals without dropping unknown provider counters."""
    total = {str(key): int(value) for key, value in current.items()}
    for key, value in increment.items():
        total[str(key)] = total.get(str(key), 0) + int(value)
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


def usage_payload(usage: Any) -> dict[str, int]:
    """Serialize an upstream ``RunUsage``/ACP usage object into plain counters."""
    if usage is None:
        return {}
    result: dict[str, int] = {}
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
        if isinstance(value, int) and not isinstance(value, bool):
            result[name] = value
    return result


def list_result(
    records: Sequence[SessionRecord], *, limit: int, offset: int
) -> tuple[list[SessionRecord], int | None]:
    page = list(records[offset : offset + limit])
    next_offset = offset + len(page)
    return page, next_offset if next_offset < len(records) else None
