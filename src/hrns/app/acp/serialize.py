"""Coerce tool input/output into JSON-able payloads sized for ACP session updates."""

from __future__ import annotations

import json
from collections.abc import Iterator

from pydantic_core import to_jsonable_python

# ACP stdio is newline-delimited JSON and clients read it with a bounded buffer (asyncio's
# default StreamReader limit is 64 KiB). A single oversized notification overruns that buffer
# and drops the connection, so long text is split across several updates.
#
# The SDK serializes outbound text with json.dumps(..., ensure_ascii=True), so a non-ASCII code
# point expands inside the JSON string: a BMP char to \uXXXX (6 bytes) and an astral char to a
# surrogate pair (12 bytes). A character-count cap therefore cannot bound the wire size, so we
# chunk by escaped byte length instead.
MAX_TEXT_UPDATE_BYTES = 48 * 1024

# Tool input/output is sent whole in one notification (it cannot be chunked across updates like
# streamed text), so an oversized payload is truncated to keep the notification under the buffer.
MAX_RAW_FIELD_CHARS = 16 * 1024


def _escaped_len(char: str) -> int:
    """Bytes ``char`` occupies inside a ``json.dumps(..., ensure_ascii=True)`` string."""
    if char in '"\\\b\f\n\r\t':
        return 2
    code = ord(char)
    if code < 0x20:
        return 6
    if code < 0x80:
        return 1
    if code < 0x10000:
        return 6
    return 12


def chunk_text(text: str, budget: int = MAX_TEXT_UPDATE_BYTES) -> Iterator[str]:
    """Split ``text`` so each chunk's JSON-escaped byte length stays within ``budget``."""
    chunk: list[str] = []
    size = 0
    for char in text:
        char_size = _escaped_len(char)
        if chunk and size + char_size > budget:
            yield "".join(chunk)
            chunk = []
            size = 0
        chunk.append(char)
        size += char_size
    if chunk:
        yield "".join(chunk)


def jsonable(value: object) -> object:
    """Coerce arbitrary tool input/output into something an ACP session update can serialize."""
    return to_jsonable_python(value, fallback=str, bytes_mode="base64")


def bounded_jsonable(value: object) -> object:
    """``jsonable``, but replace an oversized payload with a truncated marker."""
    payload = jsonable(value)
    serialized = json.dumps(payload)
    if len(serialized) <= MAX_RAW_FIELD_CHARS:
        return payload
    return {
        "truncated": True,
        "original_length": len(serialized),
        "preview": serialized[:MAX_RAW_FIELD_CHARS],
    }
