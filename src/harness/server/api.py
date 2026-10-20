"""Authenticated HTTP catalog endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse

from ._version import __version__
from .auth import PrincipalConfig, enforce_transport
from .catalog import agent_descriptor, paginate_deployments, parse_limit
from .errors import ApiError
from .state import HarnessState, get_state

__all__ = ["router", "healthz", "list_agents", "get_agent"]

router = APIRouter()

_CATALOG_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Malformed query parameter or invalid cursor."},
    401: {"description": "Missing or unknown bearer token."},
    403: {"description": "TLS is required for this connection."},
    500: {"description": "Unexpected server failure."},
}


async def require_principal(
    request: Request,
    state: Annotated[HarnessState, Depends(get_state)],
) -> PrincipalConfig:
    """Enforce transport policy and authenticate the request's bearer token."""

    enforce_transport(request.scope, state.settings)
    return state.principals.authenticate(request.headers)


@router.get(
    "/healthz",
    tags=["health"],
    summary="Liveness probe",
    responses={
        403: {"description": "TLS is required for this connection."},
        500: {"description": "Unexpected server failure."},
    },
)
async def healthz(
    request: Request,
    state: Annotated[HarnessState, Depends(get_state)],
) -> JSONResponse:
    """Unauthenticated liveness probe (TLS policy still applies)."""

    enforce_transport(request.scope, state.settings)
    return JSONResponse(
        {"status": "ok", "service": "harness-server", "version": __version__}
    )


@router.get(
    "/v1/agents",
    tags=["catalog"],
    summary="List available agent deployments",
    responses=_CATALOG_RESPONSES,
)
async def list_agents(
    principal: Annotated[PrincipalConfig, Depends(require_principal)],
    state: Annotated[HarnessState, Depends(get_state)],
    limit: Annotated[
        str | None,
        Query(
            description="Maximum items per page; an integer from 1 to 100 (default 50).",
            examples=["50"],
        ),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(
            description="Opaque cursor returned by the previous page.",
            examples=["eyJvIjoxLCJ2IjoxfQ.signature"],
        ),
    ] = None,
) -> JSONResponse:
    """Paginated catalog of deployment revisions available to the caller."""

    parsed_limit = parse_limit(limit)
    items, next_cursor = paginate_deployments(
        state.allowed_deployments(principal),
        limit=parsed_limit,
        cursor=cursor or None,
        codec=state.cursor_codec,
    )
    return JSONResponse({"items": items, "nextCursor": next_cursor})


@router.get(
    "/v1/agents/{agent_id}",
    tags=["catalog"],
    summary="Get an agent deployment",
    responses={
        401: {"description": "Missing or unknown bearer token."},
        403: {"description": "TLS is required for this connection."},
        404: {"description": "Unknown agent or agent not allowed for this principal."},
        500: {"description": "Unexpected server failure."},
    },
)
async def get_agent(
    agent_id: Annotated[
        str,
        Path(description="Immutable deployment revision id."),
    ],
    principal: Annotated[PrincipalConfig, Depends(require_principal)],
    state: Annotated[HarnessState, Depends(get_state)],
) -> JSONResponse:
    """Descriptor for one immutable deployment revision."""

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
