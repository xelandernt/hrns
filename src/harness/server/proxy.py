"""WebSocket ACP endpoint: handshake policy, worker spawn and relay wiring.

Handshake failures are answered at the HTTP layer with the standard error
envelope (the ASGI WebSocket Denial Response extension, supported by Uvicorn
and the Starlette/FastAPI test client).  Servers without that extension fall
back to an immediate WebSocket close with the documented application close
code.

After a successful upgrade the proxy is intentionally content-neutral: every
text frame carries exactly one JSON-RPC object and is forwarded unchanged in
either direction.  Framing, size and buffering rules are enforced here; ACP
semantics (and session ownership) remain the worker's responsibility.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, WebSocket
from fastapi.responses import JSONResponse

from .auth import enforce_transport
from .errors import (
    ACP_SUBPROTOCOL,
    CLOSE_AGENT_NOT_FOUND,
    CLOSE_SERVER_BUSY,
    CLOSE_SUBPROTOCOL_REQUIRED,
    CLOSE_TLS_REQUIRED,
    CLOSE_UNAUTHORIZED,
    ApiError,
)
from .state import HarnessState, get_websocket_state
from .worker import WorkerSession, WorkerSpawnError

__all__ = ["router", "acp_websocket", "denial_fallback_code"]

logger = logging.getLogger("harness.server.acp")

router = APIRouter()


_FALLBACK_CODES = {
    "UNAUTHORIZED": CLOSE_UNAUTHORIZED,
    "TLS_REQUIRED": CLOSE_TLS_REQUIRED,
    "NOT_FOUND": CLOSE_AGENT_NOT_FOUND,
    "SERVER_BUSY": CLOSE_SERVER_BUSY,
    "WORKER_SPAWN_FAILED": CLOSE_SERVER_BUSY,
}


def denial_fallback_code(exc: ApiError) -> int:
    """WS close code used when the server cannot answer with an HTTP denial."""

    if exc.code == "INVALID_REQUEST" and exc.details.get("param") == "subprotocol":
        return CLOSE_SUBPROTOCOL_REQUIRED
    return _FALLBACK_CODES.get(exc.code, 1011)


async def _deny(ws: WebSocket, exc: ApiError) -> None:
    """Reject an upgrade with an HTTP error envelope, or close if unsupported."""

    response = JSONResponse(exc.envelope(), status_code=exc.status_code, headers=exc.headers)
    try:
        await ws.send_denial_response(response)
        return
    except RuntimeError:
        # ASGI server lacks the websocket.http.response extension.
        pass
    except Exception:  # pragma: no cover - socket teardown race
        pass
    try:
        await ws.close(code=denial_fallback_code(exc), reason=exc.code)
    except Exception:  # pragma: no cover - socket already gone
        pass


def _offered_subprotocols(scope: Mapping[str, Any]) -> list[str]:
    offered = scope.get("subprotocols") or []
    return [value for value in offered if isinstance(value, str)]


async def _close_session(session: WorkerSession) -> None:
    """Bounded worker teardown that survives ASGI task cancellation.

    Once a connection has been accepted, cancellation means "the client is
    gone"; the worker is still reaped before the handler returns.  Returning
    normally (rather than re-raising) lets ASGI servers complete their
    connection teardown without logging a spurious cancellation.
    """

    try:
        await session.aclose()
    except asyncio.CancelledError:
        try:
            await session.aclose_uninterruptible()
        except asyncio.CancelledError:
            pass


@router.websocket("/v1/agents/{agent_id}/acp")
async def acp_websocket(
    ws: WebSocket,
    agent_id: Annotated[str, Path(description="Immutable deployment revision id.")],
    state: Annotated[HarnessState, Depends(get_websocket_state)],
) -> None:
    """Serve one authenticated ACP connection for ``/v1/agents/{id}/acp``."""

    settings = state.settings
    scope = ws.scope

    # -- handshake policy (all checks happen before the upgrade completes) --
    try:
        enforce_transport(scope, settings)
        principal = state.principals.authenticate(ws.headers)
        deployment = state.deployment(agent_id)
        if deployment is None or not state.principals.may_use_agent(principal, agent_id):
            # Unknown and unauthorised agents are deliberately indistinguishable.
            raise ApiError(
                "NOT_FOUND",
                "no such agent is available to this principal",
                details={"agentId": agent_id},
            )
        if ACP_SUBPROTOCOL not in _offered_subprotocols(scope):
            raise ApiError(
                "INVALID_REQUEST",
                f"the {ACP_SUBPROTOCOL} WebSocket subprotocol is required",
                details={"param": "subprotocol", "required": ACP_SUBPROTOCOL},
            )
        state.slots.acquire(principal.id)
    except ApiError as exc:
        await _deny(ws, exc)
        return

    session: WorkerSession | None = None
    try:
        session = WorkerSession(
            deployment=deployment,
            principal_id=principal.id,
            store_path=state.worker_store_path(deployment.id),
            settings=settings,
        )
        try:
            await session.start()
        except WorkerSpawnError as exc:
            await _deny(
                ws,
                ApiError(
                    "WORKER_SPAWN_FAILED",
                    "the deployment worker could not be started",
                    details={"agentId": agent_id, **exc.details},
                ),
            )
            return

        state.registry.add(session)
        await ws.accept(subprotocol=ACP_SUBPROTOCOL)
        try:
            await session.relay(ws)
        except asyncio.CancelledError:
            # The socket is gone; worker cleanup runs in ``finally``.
            pass
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ACP connection failed for deployment %s", agent_id)
        try:
            await ws.close(code=1011, reason="INTERNAL_ERROR")
        except Exception:  # pragma: no cover - socket already gone
            pass
    finally:
        try:
            if session is not None:
                state.registry.discard(session)
                await _close_session(session)
        finally:
            # Release the capacity slot even when teardown is cancelled.
            state.slots.release(principal.id)
