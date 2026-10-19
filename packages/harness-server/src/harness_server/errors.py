"""HTTP/JSON error envelopes and WebSocket close-code constants.

Every error response served by ``harness-server`` uses one predictable
envelope shape::

    {"error": {"code": "<machine-readable-key>", "message": "<human-readable>",
               "details": {...}}}

WebSocket runtime failures reuse the same vocabulary encoded into close
frames (code + short reason).  Handshake failures are delivered as HTTP
status responses (see :mod:`harness_server.proxy`); the WS close codes
below are fallbacks for servers that do not support the denial-response
extension.
"""

from __future__ import annotations

from typing import Any

from harness_protocol import MAX_MESSAGE_BYTES, SUBPROTOCOL

__all__ = [
    "MAX_MESSAGE_BYTES",
    "ACP_SUBPROTOCOL",
    "CLOSE_PROTOCOL_ERROR",
    "CLOSE_UNSUPPORTED_DATA",
    "CLOSE_INVALID_PAYLOAD",
    "CLOSE_MESSAGE_TOO_BIG",
    "CLOSE_UNAUTHORIZED",
    "CLOSE_TLS_REQUIRED",
    "CLOSE_AGENT_NOT_FOUND",
    "CLOSE_SUBPROTOCOL_REQUIRED",
    "CLOSE_SERVER_BUSY",
    "CLOSE_WORKER_EXITED",
    "CLOSE_OUTBOUND_STALLED",
    "ERROR_STATUS",
    "ApiError",
    "envelope",
    "http_error_codes",
]

# Transport contract, shared with clients and workers.
ACP_SUBPROTOCOL = SUBPROTOCOL
#: A single WS message may not exceed this many bytes (16 MiB, per the
#: project transport contract).  One complete UTF-8 JSON-RPC object per
#: text message; no batches, no binary.

# WebSocket close codes used by the ACP transport.
CLOSE_PROTOCOL_ERROR = 1002          # malformed / non-object / batch frame
CLOSE_UNSUPPORTED_DATA = 1003        # binary frame
CLOSE_INVALID_PAYLOAD = 1007         # invalid UTF-8
CLOSE_MESSAGE_TOO_BIG = 1009         # oversize text frame
CLOSE_UNAUTHORIZED = 4401            # fallback for 401 denial
CLOSE_TLS_REQUIRED = 4403            # fallback for TLS enforcement denial
CLOSE_AGENT_NOT_FOUND = 4404         # fallback for unknown/forbidden agent
CLOSE_SUBPROTOCOL_REQUIRED = 4455    # fallback for missing subprotocol
CLOSE_SERVER_BUSY = 4503             # fallback for capacity denial
CLOSE_WORKER_EXITED = 4511           # worker process died or was killed
CLOSE_OUTBOUND_STALLED = 4512        # consumer stopped draining frames

_STABLE_ERROR_CODES: dict[str, int] = {
    "INVALID_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "TLS_REQUIRED": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "CONFLICT": 409,
    "SERVER_BUSY": 503,
    "WORKER_SPAWN_FAILED": 503,
    "INTERNAL_ERROR": 500,
}

#: Stable machine-readable error codes mapped to HTTP status codes.
ERROR_STATUS = dict(_STABLE_ERROR_CODES)


class ApiError(Exception):
    """Raised inside request handling to produce a predictable envelope.

    ``code`` must be one of :data:`ERROR_STATUS` keys unless an explicit
    ``status_code`` is supplied.  Details are always structured so clients
    can key on them; ``message`` is always human-readable.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        status = status_code if status_code is not None else _STABLE_ERROR_CODES.get(code)
        if status is None:  # pragma: no cover - programmer error
            raise ValueError(f"unknown error code without status: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.status_code = int(status)
        self.headers: dict[str, str] = dict(headers or {})

    def envelope(self) -> dict[str, Any]:
        return envelope(self.code, self.message, self.details)


def envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the canonical error response body for ``code``.

    ``details`` is always present (an empty object when the failure has no
    structured context), matching the documented ``{error: {code, message,
    details}}`` shape used by HTTP catalog clients and WebSocket denials.
    """

    return {
        "error": {
            "code": code,
            "message": message,
            "details": dict(details or {}),
        }
    }


def http_error_codes() -> tuple[str, ...]:
    """Return the stable error-code keys (introspection/testing helper)."""

    return tuple(_STABLE_ERROR_CODES)
