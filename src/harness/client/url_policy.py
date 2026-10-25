"""Transport URL policy shared by the HTTP catalog and WebSocket transport.

Bearer credentials must never travel over plaintext to a remote host. Loopback development
is the one explicit exception: ``http://`` and ``ws://`` are accepted there because the
packets never leave the machine.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from .errors import TransportError

__all__ = ["is_loopback_host", "require_secure_http_url", "require_secure_ws_url"]

_HTTP_SCHEMES = frozenset({"http", "https"})
_WS_SCHEMES = frozenset({"ws", "wss"})


def is_loopback_host(host: str | None) -> bool:
    """Whether *host* names this machine (``localhost`` or a loopback IP literal)."""
    if not host:
        return False
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback


def _require_secure_url(url: str, *, schemes: frozenset[str], kind: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in schemes or not parsed.hostname:
        allowed = " or ".join(f"{scheme}://" for scheme in sorted(schemes))
        raise TransportError(f"{kind} URL must start with {allowed}, got '{url}'")
    if parsed.scheme in {"https", "wss"}:
        return
    if is_loopback_host(parsed.hostname):
        return
    secure_scheme = "wss" if "ws" in schemes else "https"
    raise TransportError(
        f"Refusing to send credentials over plaintext {parsed.scheme}:// to a remote host; "
        f"use {secure_scheme}:// (plain {parsed.scheme} is allowed only for loopback development)."
    )


def require_secure_http_url(url: str) -> None:
    """Reject a plaintext remote HTTP URL before any authenticated catalog request."""
    _require_secure_url(url, schemes=_HTTP_SCHEMES, kind="Catalog")


def require_secure_ws_url(url: str) -> None:
    """Reject a plaintext remote WebSocket URL before it can carry a bearer token."""
    _require_secure_url(url, schemes=_WS_SCHEMES, kind="WebSocket")
