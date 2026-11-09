"""URL validation shared by the CLI, profiles, and transports."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from .errors import TransportError


def check_websocket_url(url: str) -> str:
    """Return *url* when it is a usable ACP WebSocket endpoint.

    A bearer token must never cross the network in plaintext, so ``ws://`` is
    accepted only for loopback development; everything else needs ``wss://``.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise TransportError(f"WebSocket URL must start with ws:// or wss://, got '{url}'")
    if parsed.scheme == "ws" and not _is_loopback_host(parsed.hostname):
        raise TransportError(
            "Refusing plaintext ws:// to a remote host; use wss://. "
            "Plain ws:// is allowed only for loopback development."
        )
    return url


def _is_loopback_host(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback
