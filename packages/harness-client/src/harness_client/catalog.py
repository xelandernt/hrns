"""Authenticated HTTP deployment catalog.

Endpoints (CONCEPT §11.2):

* ``GET /v1/agents?cursor=...&limit=...`` → ``{items, nextCursor}``
* ``GET /v1/agents/{agentId}`` → deployment descriptor
* ``GET /v1/agents/{agentId}/acp`` → WebSocket upgrade (handled by the transport)

Errors are ``{error: {code, message, details}}`` and become
:class:`~harness_client.errors.CatalogError` with the HTTP status preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from .constants import DEFAULT_CATALOG_LIMIT
from .errors import CatalogError
from .profiles import secret_value

__all__ = ["AgentDeployment", "AgentPage", "CatalogClient"]


@dataclass(frozen=True, slots=True)
class AgentDeployment:
    """A catalog entry: one immutable deployed revision."""

    agent_id: str
    name: str = ""
    description: str = ""
    version: str = ""
    recommended: bool = False
    default_working_directory: str = ""
    workspaces: tuple[str, ...] = ()
    connection: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.agent_id

    def location_text(self) -> str:
        where = self.default_working_directory or (self.workspaces[0] if self.workspaces else "")
        return f"{self.label} {self.version}".strip() + (f" · {where}" if where else "")

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> AgentDeployment:
        if not isinstance(payload, dict):
            raise CatalogError(500, "MALFORMED_DESCRIPTOR", "Catalog returned a non-object item")
        agent_id = str(
            payload.get("agentId")
            or payload.get("agent_id")
            or payload.get("deploymentId")
            or payload.get("id")
            or ""
        )
        if not agent_id:
            raise CatalogError(500, "MALFORMED_DESCRIPTOR", "Catalog item is missing its agent id")
        workspaces = (
            payload.get("workspaces")
            or payload.get("allowedWorkspaces")
            or payload.get("remoteWorkspaces")
            or payload.get("workingDirectories")
            or []
        )
        if isinstance(workspaces, str):
            workspaces = [workspaces]
        connection = payload.get("connection") or {}
        default_cwd = str(
            payload.get("defaultWorkingDirectory")
            or payload.get("defaultWorkspace")
            or payload.get("default_working_directory")
            or payload.get("cwd")
            or ""
        )
        return cls(
            agent_id=agent_id,
            name=str(payload.get("name") or payload.get("title") or agent_id),
            description=str(payload.get("description") or ""),
            version=str(payload.get("version") or ""),
            recommended=bool(payload.get("recommended", False)),
            default_working_directory=default_cwd,
            workspaces=tuple(str(item) for item in workspaces if isinstance(item, str)),
            connection=connection if isinstance(connection, dict) else {},
            raw=payload,
        )


@dataclass(frozen=True, slots=True)
class AgentPage:
    items: tuple[AgentDeployment, ...] = ()
    next_cursor: str | None = None


class CatalogClient:
    """Small async client for the deployment catalog; token stays in memory only."""

    def __init__(
        self,
        base_url: str,
        *,
        token_ref: str = "",
        token: str | None = None,
        timeout: float = 20.0,
        http_transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("A server profile needs a base URL")
        self.base_url = base_url.rstrip("/")
        self.token_ref = token_ref
        self._token = token
        self.timeout = timeout
        self._http_transport = http_transport
        self.headers = dict(headers or {})
        self._client: httpx.AsyncClient | None = None

    def _resolved_token(self) -> str | None:
        if self._token is not None:
            return self._token
        if self.token_ref:
            return secret_value(self.token_ref)
        return None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **self.headers}
        token = self._resolved_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _url(self, path: str) -> str:
        return self.base_url + path

    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._http_transport,
                headers=self._headers(),
                follow_redirects=False,
            )
        return self._client

    async def __aenter__(self) -> CatalogClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        client = await self._client_or_create()
        try:
            response = await client.request(method, self._url(path), params=params)
        except httpx.HTTPError as exc:
            raise CatalogError(0, "CATALOG_UNREACHABLE", f"Cannot reach the catalog at {self.base_url}: {exc}") from exc
        payload: Any = None
        try:
            payload = response.json() if response.content else None
        except Exception:
            payload = None
        if response.status_code >= 400:
            self._raise_for_error(response.status_code, payload)
        if not isinstance(payload, dict):
            raise CatalogError(response.status_code, "MALFORMED_RESPONSE", "The catalog returned a non-object response")
        return payload

    @staticmethod
    def _raise_for_error(status: int, payload: Any) -> None:
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            raise CatalogError(
                status,
                str(error.get("code") or "HTTP_ERROR"),
                str(error.get("message") or f"HTTP {status}"),
                error.get("details") if isinstance(error.get("details"), dict) else None,
            )
        if status == 401:
            raise CatalogError(status, "UNAUTHORIZED", "The server rejected the bearer token.")
        if status == 403:
            raise CatalogError(status, "FORBIDDEN", "This token cannot access the requested agent.")
        if status == 404:
            raise CatalogError(status, "NOT_FOUND", "The server has no such agent deployment.")
        raise CatalogError(status, "HTTP_ERROR", f"The catalog request failed with HTTP {status}.")

    async def list_agents(self, *, cursor: str | None = None, limit: int = DEFAULT_CATALOG_LIMIT) -> AgentPage:
        if not 1 <= int(limit) <= 100:
            raise ValueError("Catalog limit must be between 1 and 100")
        params: dict[str, Any] = {"limit": int(limit)}
        if cursor:
            params["cursor"] = cursor
        payload = await self._request("GET", "/v1/agents", params=params)
        raw_items = payload.get("items")
        if raw_items is None:
            raw_items = payload.get("agents") or []
        if not isinstance(raw_items, list):
            raise CatalogError(200, "MALFORMED_RESPONSE", "Catalog items must be a list")
        items = tuple(AgentDeployment.from_wire(item) for item in raw_items)
        cursor_out = payload.get("nextCursor", payload.get("next_cursor"))
        return AgentPage(items=items, next_cursor=cursor_out)

    async def get_agent(self, agent_id: str) -> AgentDeployment:
        if not agent_id:
            raise ValueError("agent_id cannot be empty")
        payload = await self._request("GET", f"/v1/agents/{quote(agent_id, safe='')}")
        item = payload.get("agent") if isinstance(payload.get("agent"), dict) else payload
        return AgentDeployment.from_wire(item)

    def websocket_url(self, agent_id: str) -> str:
        """Derive the ACP endpoint URL, preserving test/HTTP schemes."""
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/v1/agents/{quote(agent_id, safe='')}/acp"

    def resolve(self, reference: str) -> str:
        """Join a possibly relative URL (useful for profiles that store a path)."""
        return urljoin(self.base_url + "/", reference)
