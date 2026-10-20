"""Bearer authentication, principal lookup and transport security policy."""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from typing import Any, Mapping

from .config import ConfigError, PrincipalConfig, ServerSettings
from .errors import ApiError

__all__ = [
    "PrincipalIndex",
    "bearer_token_from_headers",
    "authenticate_headers",
    "tls_indicated",
    "client_is_loopback",
    "enforce_transport",
]


def _header_get(headers: Mapping[str, Any], name: str) -> str | None:
    """Case-insensitive header lookup that also tolerates raw byte maps."""

    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if value is None:
        value = headers.get(name.title())
    if isinstance(value, bytes):  # pragma: no cover - ASGI raw safety
        value = value.decode("latin-1")
    return value if isinstance(value, str) else None


def bearer_token_from_headers(headers: Mapping[str, Any]) -> str | None:
    """Extract a bearer token from an ``Authorization`` header.

    Returns ``None`` when the header is absent or not a well-formed bearer
    credential.  Scheme matching is case-insensitive per RFC 7235.
    """

    raw = _header_get(headers, "authorization")
    if not raw:
        return None
    scheme, _, credential = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    credential = credential.strip()
    return credential or None


class PrincipalIndex:
    """Immutable lookup of principals by id and by token hash."""

    def __init__(self, principals: list[PrincipalConfig]) -> None:
        self._by_id: dict[str, PrincipalConfig] = {}
        self._by_token_hash: dict[str, str] = {}
        for principal in principals:
            if principal.id in self._by_id:
                raise ConfigError(f"duplicate principal id: {principal.id}")
            self._by_id[principal.id] = principal
            for digest in principal.token_hashes:
                owner = self._by_token_hash.get(digest)
                if owner is not None and owner != principal.id:
                    raise ConfigError(
                        f"token hash shared by principals {owner!r} and {principal.id!r}"
                    )
                self._by_token_hash[digest] = principal.id

    def __len__(self) -> int:
        return len(self._by_id)

    def by_id(self, principal_id: str) -> PrincipalConfig | None:
        return self._by_id.get(principal_id)

    def principal_for_token(self, token: str) -> PrincipalConfig | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        # Compare every configured digest with a constant-time primitive rather
        # than returning on the first dictionary hit.
        matched: str | None = None
        for candidate, principal_id in self._by_token_hash.items():
            if secrets.compare_digest(digest, candidate):
                matched = principal_id
        if matched is None:
            return None
        return self._by_id.get(matched)

    def authenticate(self, headers: Mapping[str, Any]) -> PrincipalConfig:
        """Authenticate request/upgrade headers or raise ``ApiError`` 401."""

        token = bearer_token_from_headers(headers)
        if token is None:
            raise ApiError(
                "UNAUTHORIZED",
                "a bearer token is required",
                details={"scheme": "Bearer"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        principal = self.principal_for_token(token)
        if principal is None:
            raise ApiError(
                "UNAUTHORIZED",
                "the bearer token is not recognised",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    def may_use_agent(self, principal: PrincipalConfig, agent_id: str) -> bool:
        return agent_id in principal.allowed_agent_ids


def authenticate_headers(
    index: PrincipalIndex, headers: Mapping[str, Any]
) -> PrincipalConfig:
    """Module-level convenience wrapper around :meth:`PrincipalIndex.authenticate`."""

    return index.authenticate(headers)


def _client_host(scope: Mapping[str, Any]) -> str | None:
    client = scope.get("client")
    if not client:
        return None
    host = client[0] if isinstance(client, (tuple, list)) else client
    return host if isinstance(host, str) else None


def client_is_loopback(scope: Mapping[str, Any]) -> bool:
    """Whether the direct socket peer is loopback (or unknown/in-process)."""

    host = _client_host(scope)
    if host is None:
        # Embedded ASGI transports (TestClient, in-process mounts) provide no
        # peer address.  Treat as local; real Uvicorn always provides one.
        return True
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _peer_is_trusted_proxy(scope: Mapping[str, Any], allow: list[str]) -> bool:
    if "*" in allow:
        return True
    host = _client_host(scope)
    if host is None:
        return True
    for entry in allow:
        if entry == host:
            return True
        try:
            if ipaddress.ip_address(entry) == ipaddress.ip_address(host):
                return True
        except ValueError:
            continue
    return False


def _forwarded_proto(scope: Mapping[str, Any]) -> str | None:
    headers = scope.get("headers") or ()
    for name, value in headers:
        if name.lower() == b"x-forwarded-proto":
            try:
                raw = value.decode("latin-1")
            except Exception:  # pragma: no cover - defensive
                return None
            return raw.split(",")[0].strip().lower() or None
    return None


def tls_indicated(scope: Mapping[str, Any], settings: ServerSettings) -> bool:
    """Whether the request arrived over TLS (directly or via trusted proxy)."""

    if scope.get("scheme") in {"https", "wss"}:
        return True
    proto = _forwarded_proto(scope)
    if proto in {"https", "wss"} and _peer_is_trusted_proxy(
        scope, settings.forwarded_allow_ips
    ):
        return True
    return False


def enforce_transport(scope: Mapping[str, Any], settings: ServerSettings) -> None:
    """Raise ``ApiError(TLS_REQUIRED)`` when plaintext is not acceptable.

    Plain HTTP/WS stays available to loopback development clients even when
    TLS is required; every other peer must present ``https``/``wss``
    (directly or through a trusted TLS-terminating proxy advertising
    ``X-Forwarded-Proto``).
    """

    if not settings.require_tls:
        return
    if tls_indicated(scope, settings):
        return
    if client_is_loopback(scope):
        return
    raise ApiError(
        "TLS_REQUIRED",
        "this server requires TLS; connect with wss/https through the TLS terminator",
    )
