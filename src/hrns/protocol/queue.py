"""Authoritative durable queue contracts and client-side convergence."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from .models import WireModel

QueueStateName = Literal[
    "queued",
    "claimed",
    "completed",
    "failed",
    "cancelled",
    "uncertain",
]


class QueueMutationRequest(WireModel):
    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1, strict=True)
    mutation_id: str = Field(min_length=1, max_length=200)

    @field_validator("session_id", "prompt_id", "mutation_id")
    @classmethod
    def reject_blank_ids(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier must not be blank")
        return value


class QueueEditRequest(QueueMutationRequest):
    prompt: list[JsonValue]


class QueueRemoveRequest(QueueMutationRequest):
    pass


class QueueItem(WireModel):
    """One complete authoritative queue item or cancelled tombstone."""

    schema_version: Literal[1] = 1
    update_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1, strict=True)
    prompt_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    principal: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    credential_id: str | None = None
    connection_id: str | None = None
    sequence: int = Field(ge=1, strict=True)
    prompt: list[JsonValue]
    state: QueueStateName
    attempt: int = Field(ge=0, strict=True)
    parent_id: str | None = None
    owner_id: str | None = None
    fence: int | None = None
    created_at: int = Field(ge=0, strict=True)
    updated_at: int = Field(ge=0, strict=True)


class QueueUpdate(WireModel):
    """One positioned queue transition delivered by response or live notification."""

    schema_version: Literal[1] = 1
    update_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1, strict=True)
    item: QueueItem

    @model_validator(mode="after")
    def require_matching_position(self) -> Self:
        if self.item.update_id != self.update_id or self.item.revision != self.revision:
            raise ValueError("queue update and item positions must match")
        return self


class QueueSnapshot(WireModel):
    """A complete ordered queue read at one durable session revision."""

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    snapshot_revision: int = Field(ge=0, strict=True)
    items: list[QueueItem]


class QueueUpdateProtocolError(ValueError):
    """Queue update identities or revisions conflict during convergence."""

    code = "QUEUE_UPDATE_PROTOCOL_ERROR"


class QueueState:
    """Merge list snapshots and live updates by durable identity and revision."""

    def __init__(self, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        self.snapshot_revision = 0
        self._by_id: dict[str, QueueItem] = {}
        self._by_revision: dict[int, str] = {}
        self._items: dict[str, QueueItem] = {}

    def _record(self, item: QueueItem) -> bool:
        if item.session_id != self.session_id:
            raise QueueUpdateProtocolError("queue item belongs to another session")
        known_id = self._by_revision.get(item.revision)
        if known_id is not None and known_id != item.update_id:
            raise QueueUpdateProtocolError(
                f"queue revision {item.revision} has conflicting update identities"
            )
        prior = self._by_id.get(item.update_id)
        if prior is not None:
            if prior != item:
                raise QueueUpdateProtocolError(
                    f"queue update {item.update_id!r} has conflicting payloads"
                )
            return False
        self._by_id[item.update_id] = item
        self._by_revision[item.revision] = item.update_id
        current = self._items.get(item.prompt_id)
        if current is None or item.revision > current.revision:
            self._items[item.prompt_id] = item
        elif item.revision == current.revision and item != current:
            raise QueueUpdateProtocolError(
                f"queue item {item.prompt_id!r} has conflicting revision payloads"
            )
        return True

    def accept(self, update: QueueUpdate) -> bool:
        if update.update_id != update.item.update_id or update.revision != update.item.revision:
            raise QueueUpdateProtocolError("queue update position does not match its item")
        return self._record(update.item)

    def merge_snapshot(self, snapshot: QueueSnapshot) -> None:
        if snapshot.session_id != self.session_id:
            raise QueueUpdateProtocolError("queue snapshot belongs to another session")
        for item in snapshot.items:
            if item.revision > snapshot.snapshot_revision:
                raise QueueUpdateProtocolError(
                    "queue snapshot contains an item newer than its snapshot revision"
                )
            self._record(item)
        if snapshot.snapshot_revision < self.snapshot_revision:
            return
        visible_ids = {item.prompt_id for item in snapshot.items}
        for prompt_id, item in tuple(self._items.items()):
            if prompt_id not in visible_ids and item.revision <= snapshot.snapshot_revision:
                self._items.pop(prompt_id)
        self.snapshot_revision = snapshot.snapshot_revision

    def merge_updates(self, updates: Iterable[QueueUpdate]) -> None:
        for update in updates:
            self.accept(update)

    def ordered(self) -> tuple[QueueItem, ...]:
        return tuple(sorted(self._items.values(), key=lambda item: item.sequence))
