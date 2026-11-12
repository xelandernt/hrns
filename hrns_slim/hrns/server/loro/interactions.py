"""Session-owned questions and approvals resolved by any interface.

The document stores the pending request and the first accepted answer. A Loro
participant can answer while an ACP request is outstanding, and vice versa. A
last-writer-wins field would not be enough: acceptance is a server-side
check-and-write against the pending state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from .document import SessionDocument
from .models import InteractionKind, InteractionRecord

Validator = Callable[[Mapping[str, object]], dict[str, object]]
_logger = logging.getLogger(__name__)

APPROVAL_OPTIONS = frozenset(
    {"allow_once", "allow_always", "reject_once", "reject_always", "cancel"}
)
_APPROVAL_OPTIONS = APPROVAL_OPTIONS
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
        if self._document.poisoned:
            raise RuntimeError("cannot create an interaction on a poisoned document")
        record = self._document.create_interaction(kind=kind, request=request)
        self._validators[record.id] = validator
        # Acceptance is durable before any transport is asked; a failure aborts the turn.
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
        await self._save(interaction_id)
        self._signal(interaction_id)
        self._on_change()
        return True

    async def ingest(self) -> list[str]:
        """Accept answers written directly into the document by Loro clients.

        A client writes a ``response`` object and leaves ``state`` pending; the
        server validates it, applies the first-answer rule, and sets ``resolved``.
        Answers that arrived while the server was not waiting are accepted too.
        """
        accepted: list[str] = []
        for record in self._document.interactions():
            if record.state != "pending" or record.response is None:
                continue
            validator = self._validators.get(record.id)
            if validator is None:
                continue
            try:
                validated = validator(record.response)
            except ValueError:
                continue
            actor = record.resolved_by or "loro"
            if not self._document.resolve_interaction(record.id, validated, resolved_by=actor):
                continue
            accepted.append(record.id)
        if accepted:
            await self._save(*accepted)
            for interaction_id in accepted:
                self._signal(interaction_id)
            self._on_change()
        return accepted

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
            await self._save(interaction_id)
            self._signal(interaction_id)
            self._on_change()
        return cancelled

    async def cancel_pending(self, *, detail: str, state: str = "cancelled") -> list[str]:
        cancelled = self._document.cancel_pending_interactions(state=state, detail=detail)
        if cancelled:
            await self._save(*cancelled)
            for interaction_id in cancelled:
                self._signal(interaction_id)
            self._on_change()
        return cancelled

    async def _save(self, *interaction_ids: str) -> None:
        """Persist an acceptance; a poisoned document is intentionally not written."""
        if self._document.poisoned:
            return
        try:
            await self._persist()
        except Exception:
            _logger.exception(
                "failed to persist interaction state %s; waking waiters anyway",
                ", ".join(interaction_ids) or "-",
            )

    def _signal(self, interaction_id: str) -> None:
        waiter = self._waiters.pop(interaction_id, None)
        if waiter is not None:
            waiter.event.set()


__all__ = [
    "APPROVAL_OPTIONS",
    "InteractionService",
    "validate_approval",
    "validate_question",
]
