"""ACP-facing persistence names over the canonical document store.

The payload is defined in :mod:`hrns.server.loro.store`; ACP adds only the
transcript-derived title helper it already used.
"""

from __future__ import annotations

from collections.abc import Sequence

from acp import schema

from ..loro.store import (
    DeletableSessionStore,
    DocumentCheckpoint,
    InMemoryDocumentStore,
    ListableSessionStore,
    SessionAccess,
    SessionStore,
    SessionSummary,
)


def title_from_updates(updates: Sequence[object]) -> str | None:
    """Derive a session title from the first user text in its transcript."""
    for update in updates:
        if not isinstance(update, schema.UserMessageChunk):
            continue
        content = update.content
        if isinstance(content, schema.TextContentBlock):
            title = " ".join(content.text.split())
            if title:
                return title[:80]
    return None


__all__ = [
    "DeletableSessionStore",
    "DocumentCheckpoint",
    "InMemoryDocumentStore",
    "ListableSessionStore",
    "SessionAccess",
    "SessionStore",
    "SessionSummary",
    "title_from_updates",
]
