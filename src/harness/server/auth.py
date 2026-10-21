"""Ready-to-use bearer authentication for HTTP and WebSocket routes."""

import hashlib
import secrets
from collections.abc import Mapping

from fastapi import HTTPException
from starlette.requests import HTTPConnection

from .models import Principal


class BearerAuth:
    """A FastAPI dependency mapping static bearer tokens to stable identities.

    Args:
        tokens: Mapping from complete bearer tokens to authenticated identities,
            for example ``{os.environ["HARNESS_TOKEN"]: Principal("alice")}``.
            Multiple tokens can map to the same user to support token rotation.
            The mapping is captured on construction and tokens are hashed;
            this dependency does not retain plaintext tokens or load users.

    Pass the instance as ``AgentServer(authenticate=BearerAuth(...), store=...)``.
    It validates Authorization headers on HTTP and WebSocket requests and raises
    HTTPException(401) with a WWW-Authenticate challenge for invalid credentials.
    For database lookups, OAuth or JWT, supply a custom authentication dependency
    returning Principal instead; that callback needs no static user inventory.
    """

    def __init__(self, tokens: Mapping[str, Principal]) -> None:
        if not tokens:
            raise ValueError("At least one bearer token is required")
        credentials: list[tuple[bytes, Principal]] = []
        for token, principal in tokens.items():
            if not isinstance(token, str) or not token or any(c.isspace() for c in token):
                raise ValueError("Bearer tokens must be non-empty strings without whitespace")
            if not isinstance(principal, Principal):
                raise TypeError("Each bearer token must map to a Principal")
            credentials.append((hashlib.sha256(token.encode("utf-8")).digest(), principal))
        self._credentials = tuple(credentials)

    async def __call__(self, connection: HTTPConnection) -> Principal:
        """Return the token's identity or raise HTTPException(401)."""
        authorization = connection.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
            raise self._unauthorized()
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        matched: Principal | None = None
        for expected, principal in self._credentials:
            if secrets.compare_digest(digest, expected):
                matched = principal
        if matched is None:
            raise self._unauthorized()
        return matched

    @staticmethod
    def _unauthorized() -> HTTPException:
        return HTTPException(
            status_code=401,
            detail="A valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
