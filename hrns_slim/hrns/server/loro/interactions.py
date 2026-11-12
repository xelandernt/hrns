"""Session-owned questions and approvals resolved by any interface.

The document stores the pending request and the first accepted answer. A Loro
participant can answer while an ACP request is outstanding, and vice versa. A
last-writer-wins field would not be enough: acceptance is a server-side
check-and-write against the pending state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from .document import SessionDocument
from .models import InteractionKind, InteractionRecord

Validator = Callable[[Mapping[str, object]], dict[str, object]]

_APPROVAL_OPTIONS = {"allow_once", "allow_always", "reject_once", "reject_always", "cancel"}
_QUESTION_ACTIONS = {"accept", "decline", "cancel"}


def validate_approval(response: Mapping[str, object]) -> dict[str, object]:
    option = response.get("option")
    if not isinstance(option, str) or option not in _APPROVAL_OPTIONS:
        raise ValueError(
            "approval answers use option=allow_once|allow_always|reject_once|reject_always|cancel"
        )
    return {"option": option}


def validate_question(response: Mapping[str, object]) -> dict[str, object]:
    action = response.get("action", "accept")
    if not isinstance(action, str) or action not in _QUESTION_ACTIONS:
        raise ValueError("question answers use action=accept|decline|cancel")
    content = response.get("content", {})
    if not isinstance(content, Mapping):
        raise ValueError("question content must be an object")
    normalized: dict[str, object] = {}
    for key, value in content.items():
        if isinstance(key, str):
            normalized[key] = value
    return {"action": action, "content": normalized}


@dataclass
class _Waiter:
    event: asyncio.Event = field(default_factory=asyncio.Event)


class InteractionService:
    """Create, answer, and wait for one session's interactions."""

    def __init__(
        self,
        document: SessionDocument,
        *,
        persist: Callable[[], Awaitable[None]],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._document = document
        self._persist = persist
        self._on_change = on_change or (lambda: None)
        self._validators: dict[str, Validator] = {}
        self._waiters: dict[str, _Waiter] = {}

    @property
    def waiting(self) -> bool:
        return any(record.state == "pending" for record in self._document.interactions())

    def pending(self) -> list[InteractionRecord]:
        return [record for record in self._document.interactions() if record.state == "pending"]

    async def create(
        self,
        *,
        kind: InteractionKind,
        request: dict[str, object],
        validator: Validator,
    ) -> InteractionRecord:
        record = self._document.create_interaction(kind=kind, request=request)
        self._validators[record.id] = validator
        await self._persist()
        self._on_change()
        return record

    async def resolve(
        self,
        interaction_id: str,
        response: Mapping[str, object],
        *,
        actor: str,
    ) -> bool:
        """Accept the first valid answer for a pending request."""
        record = self._document.interaction(interaction_id)
        validator = self._validators.get(interaction_id)
        if record is None or record.state != "pending" or validator is None:
            return False
        try:
            validated = validator(response)
        except ValueError:
            return False
        if not self._document.resolve_interaction(interaction_id, validated, resolved_by=actor):
            return False
        await self._persist()
        self._signal(interaction_id)
        self._on_change()
        return True

    async def wait(self, interaction_id: str) -> InteractionRecord:
        while True:
            record = self._document.interaction(interaction_id)
            if record is None:
                raise KeyError(interaction_id)
            if record.state != "pending":
                return record
            waiter = self._waiters.get(interaction_id)
            if waiter is None:
                waiter = self._waiters[interaction_id] = _Waiter()
            await waiter.event.wait()

    async def cancel(self, interaction_id: str, *, detail: str) -> bool:
        cancelled = self._document.cancel_interaction(
            interaction_id, state="cancelled", detail=detail
        )
        if cancelled:
            await self._persist()
            self._signal(interaction_id)
            self._on_change()
        return cancelled

    async def cancel_pending(self, *, detail: str, state: str = "cancelled") -> list[str]:
        cancelled = self._document.cancel_pending_interactions(state=state, detail=detail)
        if cancelled:
            await self._persist()
            for interaction_id in cancelled:
                self._signal(interaction_id)
            self._on_change()
        return cancelled

    def _signal(self, interaction_id: str) -> None:
        waiter = self._waiters.pop(interaction_id, None)
        if waiter is not None:
            waiter.event.set()

    def close(self) -> None:
        for waiter in self._waiters.values():
            waiter.event.set()
        self._waiters.clear()


__all__ = [
    "InteractionService",
    "validate_approval",
    "validate_question",
]
