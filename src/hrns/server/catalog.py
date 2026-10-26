"""Agent catalog DTOs, opaque cursors and pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any, Iterable

from .errors import ACP_SUBPROTOCOL, MAX_MESSAGE_BYTES, ApiError
from .models import RegisteredAgent

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "CursorCodec",
    "agent_descriptor",
    "paginate_agents",
    "parse_limit",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MIN_LIMIT = 1

_CURSOR_VERSION = 1


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("cursor segments use unpadded base64url")
    padding = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + padding)
    # Reject non-canonical encodings whose unused trailing bits differ: otherwise
    # two distinct cursor strings could carry the same accepted signature bytes.
    if _b64encode(raw) != value:
        raise ValueError("non-canonical cursor encoding")
    return raw


class CursorCodec:
    """Opaque, tamper-evident pagination cursors.

    Cursors are an internal offset plus an HMAC signature using a per-app
    secret.  They intentionally stay opaque to clients: tampering, truncation,
    or cursors issued by a different server instance are rejected with a
    structured ``INVALID_REQUEST`` error rather than a confusing page.
    """

    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret or secrets.token_bytes(32)

    def encode(self, offset: int) -> str:
        if offset < 0:  # pragma: no cover - programmer error
            raise ValueError("cursor offset must be non-negative")
        payload = json.dumps(
            {"o": offset, "v": _CURSOR_VERSION}, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_b64encode(payload)}.{_b64encode(signature)}"

    def decode(self, cursor: str) -> int:
        try:
            encoded_payload, encoded_signature = cursor.split(".", 1)
            payload = _b64decode(encoded_payload)
            signature = _b64decode(encoded_signature)
        except Exception as exc:
            raise _invalid_cursor("the cursor is malformed") from exc

        expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise _invalid_cursor("the cursor is invalid for this server")

        try:
            data = json.loads(payload)
        except ValueError as exc:  # pragma: no cover - signature already proves origin
            raise _invalid_cursor("the cursor payload is unreadable") from exc
        if not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION:
            raise _invalid_cursor("the cursor version is not supported")
        offset = data.get("o")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise _invalid_cursor("the cursor offset is invalid")
        return offset


def _invalid_cursor(message: str) -> ApiError:
    return ApiError(
        "INVALID_REQUEST",
        message,
        details={"param": "cursor"},
    )


def parse_limit(raw: str | None) -> int:
    """Parse and bound ``limit`` (1..100, default 50)."""

    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise ApiError(
            "INVALID_REQUEST",
            "limit must be an integer",
            details={"param": "limit", "value": raw},
        ) from exc
    if value < MIN_LIMIT or value > MAX_LIMIT:
        raise ApiError(
            "INVALID_REQUEST",
            f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}",
            details={"param": "limit", "value": raw, "min": MIN_LIMIT, "max": MAX_LIMIT},
        )
    return value


def agent_descriptor(agent: RegisteredAgent, *, prefix: str = "") -> dict[str, Any]:
    """Catalog descriptor compatible with the hrns network client."""
    return {
        "agentId": agent.id,
        "name": agent.definition.name,
        "version": agent.definition.definition_version,
        "description": agent.definition.description,
        "recommended": True,
        "defaultWorkspace": agent.default_cwd,
        "allowedWorkspaces": list(agent.allowed_cwds),
        "connection": {
            "url": f"{prefix}/v1/agents/{agent.id}/acp",
            "subprotocol": ACP_SUBPROTOCOL,
            "maxMessageBytes": MAX_MESSAGE_BYTES,
        },
    }


def paginate_agents(
    agents: Iterable[RegisteredAgent],
    *,
    limit: int,
    cursor: str | None,
    codec: CursorCodec,
    prefix: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    """Return one descriptor page plus the next opaque cursor (if any)."""

    ordered = list(agents)
    offset = codec.decode(cursor) if cursor else 0
    if offset > len(ordered):
        raise ApiError(
            "INVALID_REQUEST",
            "the cursor points past the end of the catalog",
            details={"param": "cursor"},
        )
    page = ordered[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = codec.encode(next_offset) if next_offset < len(ordered) else None
    return [agent_descriptor(item, prefix=prefix) for item in page], next_cursor
