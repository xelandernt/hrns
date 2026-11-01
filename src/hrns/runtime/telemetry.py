"""Safe, aggregate runtime telemetry contracts for embedding applications."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeAlias, runtime_checkable

__all__ = [
    "ConnectionTelemetryEvent",
    "ConnectionTransition",
    "QueueTelemetryEvent",
    "QueueTransition",
    "RuntimeTelemetry",
    "RuntimeTelemetryEvent",
    "TurnTelemetryEvent",
    "TurnTransition",
]

_LOG = logging.getLogger(__name__)


class ConnectionTransition(StrEnum):
    """A connection lifecycle outcome without connection or principal identity."""

    CONNECTED = "connected"
    RECONNECTED = "reconnected"
    REJECTED = "rejected"
    DISCONNECTED = "disconnected"


class QueueTransition(StrEnum):
    """The durable state reached by a queued prompt."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class TurnTransition(StrEnum):
    """The durable state reached by an embedding-submitted turn."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ConnectionTelemetryEvent:
    """One aggregate connection lifecycle outcome."""

    transition: ConnectionTransition
    active_connections: int

    def __post_init__(self) -> None:
        if self.active_connections < 0:
            raise ValueError("active_connections must not be negative")


@dataclass(frozen=True, slots=True)
class QueueTelemetryEvent:
    """One authoritative prompt-queue transition and its aggregate state."""

    transition: QueueTransition
    depth: int
    oldest_queued_age_ms: int | None

    def __post_init__(self) -> None:
        if self.depth < 0:
            raise ValueError("depth must not be negative")
        if self.oldest_queued_age_ms is not None and self.oldest_queued_age_ms < 0:
            raise ValueError("oldest_queued_age_ms must not be negative")


@dataclass(frozen=True, slots=True)
class TurnTelemetryEvent:
    """One durable transition for an embedding-submitted turn."""

    transition: TurnTransition


RuntimeTelemetryEvent: TypeAlias = (
    ConnectionTelemetryEvent | QueueTelemetryEvent | TurnTelemetryEvent
)


@runtime_checkable
class RuntimeTelemetry(Protocol):
    """Receive safe aggregate runtime events synchronously.

    Implementations must return promptly. HRNS catches and logs observer failures
    without recording the observer exception, which may contain application data.
    """

    def emit(self, event: RuntimeTelemetryEvent, /) -> None:
        """Record one safe aggregate event."""


def emit_telemetry(observer: RuntimeTelemetry | None, event: RuntimeTelemetryEvent) -> None:
    """Notify an observer without allowing it to affect runtime execution."""
    if observer is None:
        return
    try:
        observer.emit(event)
    except Exception:
        _LOG.warning(
            "HRNS telemetry observer failed while emitting %s; continuing",
            type(event).__name__,
        )
