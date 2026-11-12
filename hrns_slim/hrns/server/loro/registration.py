"""The transport-neutral surface a Loro route needs from a registration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, Protocol

from hrns.server._types import PrincipalT

from .models import InteractionRecord
from .store import SessionAccess, SessionSummary
from .sync import SyncTransport


class LoroSession(Protocol):
    """Session operations that do not depend on the agent's deps/output types."""

    session_id: str
    cwd: str

    def status(self) -> dict[str, object]: ...

    def snapshot(self) -> bytes: ...

    async def sync(self, transport: SyncTransport) -> None: ...

    def document_changed(self) -> None: ...

    async def answer_interaction(
        self, interaction_id: str, response: Mapping[str, object], *, actor: str = "loro"
    ) -> bool: ...

    async def pending_interactions(self) -> tuple[InteractionRecord, ...]: ...


@dataclass(frozen=True)
class LoroRegistration(Generic[PrincipalT]):
    """Closures that erase per-agent dependency types at the HTTP boundary."""

    create: Callable[[str, PrincipalT | None], Awaitable[LoroSession]]
    get: Callable[[str, PrincipalT | None, SessionAccess], Awaitable[LoroSession]]
    list: (
        Callable[
            [PrincipalT | None, str | None, str | None],
            Awaitable[tuple[Sequence[SessionSummary], str | None]],
        ]
        | None
    )
    delete: Callable[[str, PrincipalT | None], Awaitable[None]] | None


__all__ = ["LoroRegistration", "LoroSession"]
