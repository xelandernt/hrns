"""Loro-backed session documents and synchronization.

Importing this package requires the optional server SDK. Client-only installs
never import it.
"""

from .document import DisplaySlot, SessionDocument
from .models import (
    SCHEMA_VERSION,
    Author,
    Consumption,
    Contribution,
    DisplayRecord,
    DocumentSettings,
    InteractionRecord,
    Lifecycle,
    SchemaVersionError,
    SessionMetadata,
)

__all__ = [
    "SCHEMA_VERSION",
    "Author",
    "Consumption",
    "Contribution",
    "DisplayRecord",
    "DisplaySlot",
    "DocumentSettings",
    "InteractionRecord",
    "Lifecycle",
    "SchemaVersionError",
    "SessionDocument",
    "SessionMetadata",
]
