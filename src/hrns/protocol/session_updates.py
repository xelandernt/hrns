"""Stable identity and ordering for durable session updates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from .models import WireModel

_UpdateT = TypeVar("_UpdateT")
_UpdateModelT = TypeVar("_UpdateModelT", bound=BaseModel)


class SessionUpdatePosition(WireModel):
    """Server-assigned identity and durable order for one session update."""

    schema_version: int = Field(default=1, ge=1)
    update_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)


class SessionUpdateProtocolError(ValueError):
    """An update is missing or conflicts with its server-assigned position."""

    code = "SESSION_UPDATE_PROTOCOL_ERROR"


def maybe_session_update_position(update: object) -> SessionUpdatePosition | None:
    """Read ``_meta.hrns.update`` from an ACP session update, when present."""
    meta = getattr(update, "field_meta", None)
    if meta is None:
        return None
    if not isinstance(meta, Mapping):
        raise SessionUpdateProtocolError("session update _meta must be an object")
    hrns = meta.get("hrns")
    if hrns is None:
        return None
    if not isinstance(hrns, Mapping):
        raise SessionUpdateProtocolError("session update _meta.hrns must be an object")
    raw = hrns.get("update")
    if raw is None:
        return None
    try:
        return SessionUpdatePosition.model_validate(raw)
    except ValueError as exc:
        raise SessionUpdateProtocolError("session update _meta.hrns.update is invalid") from exc


def session_update_position(update: object) -> SessionUpdatePosition:
    """Return the required server-assigned position for a session update."""
    position = maybe_session_update_position(update)
    if position is None:
        raise SessionUpdateProtocolError(
            "session update is missing _meta.hrns.update identity and revision"
        )
    return position


def with_session_update_position(
    update: _UpdateModelT,
    position: SessionUpdatePosition,
) -> _UpdateModelT:
    """Attach a position without replacing unrelated ACP metadata."""
    meta = getattr(update, "field_meta", None)
    if meta is None:
        merged_meta: dict[str, object] = {}
    elif isinstance(meta, Mapping):
        merged_meta = dict(meta)
    else:
        raise SessionUpdateProtocolError("session update _meta must be an object")

    raw_hrns = merged_meta.get("hrns")
    if raw_hrns is None:
        hrns: dict[str, object] = {}
    elif isinstance(raw_hrns, Mapping):
        hrns = dict(raw_hrns)
    else:
        raise SessionUpdateProtocolError("session update _meta.hrns must be an object")

    existing = hrns.get("update")
    if existing is not None:
        current = SessionUpdatePosition.model_validate(existing)
        if current != position:
            raise SessionUpdateProtocolError(
                "session update identity or revision changed after assignment"
            )
        return update

    hrns["update"] = position.to_wire()
    merged_meta["hrns"] = hrns
    return update.model_copy(update={"field_meta": merged_meta})


def order_session_updates(updates: Iterable[_UpdateT]) -> list[_UpdateT]:
    """Validate, deduplicate, and sort updates by their server-assigned revision."""
    by_id: dict[str, tuple[SessionUpdatePosition, _UpdateT]] = {}
    by_revision: dict[int, str] = {}
    for update in updates:
        position = session_update_position(update)
        known_id = by_revision.get(position.revision)
        if known_id is not None and known_id != position.update_id:
            raise SessionUpdateProtocolError(
                f"session revision {position.revision} is assigned to multiple update identities"
            )
        prior = by_id.get(position.update_id)
        if prior is not None:
            prior_position, prior_update = prior
            if prior_position.revision != position.revision:
                raise SessionUpdateProtocolError(
                    f"update identity {position.update_id!r} has conflicting revisions"
                )
            if prior_update != update:
                raise SessionUpdateProtocolError(
                    f"update identity {position.update_id!r} has conflicting payloads"
                )
            continue
        by_id[position.update_id] = (position, update)
        by_revision[position.revision] = position.update_id
    return [
        update
        for position, update in sorted(
            by_id.values(),
            key=lambda item: item[0].revision,
        )
    ]


class SessionUpdateLedger(Generic[_UpdateT]):
    """Track one session's updates, rejecting conflicts and dropping duplicates."""

    def __init__(self) -> None:
        self._by_id: dict[str, tuple[SessionUpdatePosition, _UpdateT]] = {}
        self._by_revision: dict[int, str] = {}

    def accept(self, update: _UpdateT) -> bool:
        """Record an update; return ``False`` only for an identical duplicate."""
        position = session_update_position(update)
        known_id = self._by_revision.get(position.revision)
        if known_id is not None and known_id != position.update_id:
            raise SessionUpdateProtocolError(
                f"session revision {position.revision} is assigned to multiple update identities"
            )
        prior = self._by_id.get(position.update_id)
        if prior is not None:
            prior_position, prior_update = prior
            if prior_position.revision != position.revision:
                raise SessionUpdateProtocolError(
                    f"update identity {position.update_id!r} has conflicting revisions"
                )
            if prior_update != update:
                raise SessionUpdateProtocolError(
                    f"update identity {position.update_id!r} has conflicting payloads"
                )
            return False
        self._by_id[position.update_id] = (position, update)
        self._by_revision[position.revision] = position.update_id
        return True

    def ordered(self) -> tuple[_UpdateT, ...]:
        """Return every accepted update in authoritative session order."""
        return tuple(
            update
            for position, update in sorted(
                self._by_id.values(),
                key=lambda item: item[0].revision,
            )
        )
