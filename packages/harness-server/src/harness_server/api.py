"""Authenticated HTTP catalog endpoints."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._version import __version__
from .auth import enforce_transport
from .catalog import agent_descriptor, paginate_deployments, parse_limit
from .errors import ApiError

__all__ = ["healthz", "list_agents", "get_agent"]


def _state(request: Request) -> Any:
    return request.app.state.harness


async def healthz(request: Request) -> JSONResponse:
    """Unauthenticated liveness probe (TLS policy still applies)."""

    state = _state(request)
    enforce_transport(request.scope, state.settings)
    return JSONResponse(
        {"status": "ok", "service": "harness-server", "version": __version__}
    )


async def list_agents(request: Request) -> JSONResponse:
    """Paginated catalog of deployment revisions available to the caller."""

    state = _state(request)
    enforce_transport(request.scope, state.settings)
    principal = state.principals.authenticate(request.headers)

    limit = parse_limit(request.query_params.get("limit"))
    cursor = request.query_params.get("cursor") or None
    items, next_cursor = paginate_deployments(
        state.allowed_deployments(principal),
        limit=limit,
        cursor=cursor,
        codec=state.cursor_codec,
    )
    return JSONResponse({"items": items, "nextCursor": next_cursor})


async def get_agent(request: Request) -> JSONResponse:
    """Descriptor for one immutable deployment revision."""

    state = _state(request)
    enforce_transport(request.scope, state.settings)
    principal = state.principals.authenticate(request.headers)

    agent_id = str(request.path_params.get("agent_id", ""))
    deployment = state.deployment(agent_id)
    if deployment is None or not state.principals.may_use_agent(principal, agent_id):
        # Unknown and unauthorised deployments share one shape so a caller
        # cannot probe the catalog for agents it may not use.
        raise ApiError(
            "NOT_FOUND",
            "no such agent is available to this principal",
            details={"agentId": agent_id},
        )
    return JSONResponse(agent_descriptor(deployment))
