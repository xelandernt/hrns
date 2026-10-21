"""Catalog and ACP routes attached to the embedding FastAPI application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from fastapi.responses import JSONResponse

from .catalog import agent_descriptor, paginate_agents, parse_limit
from .errors import ApiError
from .models import Principal
from .proxy import acp_websocket

if TYPE_CHECKING:
    from .app import AgentServer


def build_router(server: AgentServer, *, prefix: str = "") -> APIRouter:
    router = APIRouter(lifespan=server.lifespan)

    async def require_principal(value: object = Depends(server.authenticate)) -> Principal:
        if not isinstance(value, Principal):
            raise TypeError("The authentication dependency must return harness.server.Principal")
        return value

    @router.get("/v1/agents", tags=["agents"])
    async def list_agents(
        request: Request,
        principal: Principal = Depends(require_principal),
        limit: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
    ) -> JSONResponse:
        try:
            items, next_cursor = paginate_agents(
                server.available_agents(principal),
                limit=parse_limit(limit),
                cursor=cursor or None,
                codec=server._cursor_codec,
                prefix=request.scope.get("root_path", "") + prefix,
            )
            return JSONResponse({"items": items, "nextCursor": next_cursor})
        except ApiError as exc:
            return JSONResponse(exc.envelope(), status_code=exc.status_code)

    @router.get("/v1/agents/{agent_id}", tags=["agents"])
    async def get_agent(
        request: Request, agent_id: str, principal: Principal = Depends(require_principal)
    ) -> JSONResponse:
        try:
            return JSONResponse(
                agent_descriptor(
                    server.get_agent(agent_id, principal),
                    prefix=request.scope.get("root_path", "") + prefix,
                )
            )
        except ApiError as exc:
            return JSONResponse(exc.envelope(), status_code=exc.status_code)

    @router.websocket("/v1/agents/{agent_id}/acp")
    async def connect_agent(
        websocket: WebSocket, agent_id: str, principal: Principal = Depends(require_principal)
    ) -> None:
        await acp_websocket(server, websocket, agent_id, principal)

    return router
