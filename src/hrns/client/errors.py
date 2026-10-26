"""Errors raised by the client. Every message is written to be shown to a user."""

from __future__ import annotations

from typing import Any

from acp import RequestError


class HrnsClientError(Exception):
    """Base class for expected (recoverable, explainable) client failures."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}


class TransportError(HrnsClientError):
    """The agent process or WebSocket connection could not be established or was closed."""


class ConnectionLost(TransportError):
    """The transport was already closed when an operation was attempted."""


class ProtocolError(HrnsClientError):
    """The peer sent something the client cannot interpret."""


class ExtensionUnavailable(HrnsClientError):
    """The agent does not advertise (or does not accept) a requested project extension method."""

    def __init__(self, method: str) -> None:
        super().__init__(
            f"The agent does not support the extension method '{method}'.",
            details={"method": method},
        )
        self.method = method


class SecretMissing(HrnsClientError):
    """A referenced secret (``env:`` or ``keyring:``) could not be resolved."""

    def __init__(self, reference: str, reason: str) -> None:
        super().__init__(
            f"Secret reference '{reference}' could not be resolved: {reason}",
            details={"reference": reference},
        )
        self.reference = reference


class InteractionRequired(HrnsClientError):
    """A tool approval or structured question arrived with no configured handler."""

    def __init__(self, kind: str, summary: str, *, session_id: str | None = None) -> None:
        message = (
            f"Interaction required ({kind}: {summary}). Configure a handler "
            f"(for example --allow-all or --deny-all) or use the interactive client. "
            f"The session is retained."
        )
        super().__init__(message, details={"kind": kind, "sessionId": session_id})
        self.kind = kind
        self.session_id = session_id


class InteractionRejected(HrnsClientError):
    """The configured policy rejected an interaction; used when an explicit deny is chosen."""


class CatalogError(HrnsClientError):
    """The HTTP deployment catalog rejected a request."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            f"[{status}] {code}: {message}",
            details={"status": status, "code": code, **(details or {})},
        )
        self.status = status
        self.code = code


def describe_rpc_error(method: str, exc: RequestError) -> str:
    """Format an ACP request failure for display, including hrns error details."""
    data = exc.data if isinstance(exc.data, dict) else {}
    code = data.get("code")
    details = data.get("details") or {}
    detail_text = ""
    if isinstance(details, dict):
        for key in ("bindingId", "bindingIds", "currentRevision", "message", "reason"):
            if key in details:
                detail_text += f" {key}={details[key]}"
    elif details:
        detail_text = f" {details}"
    suffix = f" [{code}]" if code else ""
    return f"{method} failed: {exc}{suffix}{detail_text}".strip()
