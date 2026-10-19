"""Application assembly: ``create_app``, shared state, lifespan and errors."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from ._version import __version__
from .api import get_agent, healthz, list_agents
from .auth import PrincipalIndex
from .catalog import CursorCodec
from .config import (
    ConfigError,
    DeploymentConfig,
    HarnessServerConfig,
    PrincipalConfig,
    ServerSettings,
    finalize_config,
    load_config,
)
from .errors import ApiError, envelope
from .proxy import acp_websocket
from .worker import WorkerRegistry, WorkerSlots

__all__ = ["HarnessState", "create_app"]

logger = logging.getLogger("harness_server")

_HTTP_CODES: dict[int, tuple[str, str]] = {
    400: ("INVALID_REQUEST", "the request is malformed"),
    401: ("UNAUTHORIZED", "authentication is required"),
    403: ("FORBIDDEN", "the request is not permitted"),
    404: ("NOT_FOUND", "the requested resource was not found"),
    405: ("METHOD_NOT_ALLOWED", "the method is not allowed for this resource"),
    409: ("CONFLICT", "the request conflicts with the current state"),
    422: ("INVALID_REQUEST", "the request body could not be validated"),
    500: ("INTERNAL_ERROR", "internal server error"),
}


@dataclass
class HarnessState:
    """Per-application immutable catalog plus mutable capacity accounting."""

    config: HarnessServerConfig
    settings: ServerSettings
    store_path: str
    deployments: dict[str, DeploymentConfig]
    deployment_order: list[DeploymentConfig]
    principals: PrincipalIndex
    cursor_codec: CursorCodec
    slots: WorkerSlots
    registry: WorkerRegistry

    @classmethod
    def from_config(cls, config: HarnessServerConfig) -> "HarnessState":
        deployments = {deployment.id: deployment for deployment in config.deployments}
        return cls(
            config=config,
            settings=config.server,
            store_path=config.server.store_path,
            deployments=deployments,
            deployment_order=list(config.deployments),
            principals=PrincipalIndex(config.principals),
            cursor_codec=CursorCodec(),
            slots=WorkerSlots(
                config.server.max_worker_count,
                config.server.max_connections_per_principal,
            ),
            registry=WorkerRegistry(),
        )

    def deployment(self, agent_id: str) -> DeploymentConfig | None:
        return self.deployments.get(agent_id)

    def worker_store_path(self, agent_id: str) -> str:
        """Value handed to workers as ``HARNESS_STORE``.

        By default this is one SQLite database per immutable deployment revision:
        workers of the same revision (all connections) share it under SQLite WAL,
        while different runtime versions never contend for one schema. With
        ``server.store_url`` the same PostgreSQL DSN is supplied to every worker;
        rows remain scoped to ``(principal, deploymentId)`` inside the database.
        """

        if self.settings.store_url:
            return self.settings.store_url
        return str(Path(self.store_path) / f"{agent_id}.sqlite3")

    def allowed_deployments(self, principal: PrincipalConfig) -> list[DeploymentConfig]:
        allowed = set(principal.allowed_agent_ids)
        return [
            deployment
            for deployment in self.deployment_order
            if deployment.id in allowed
        ]


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    state: HarnessState = app.state.harness
    if not state.settings.store_url:
        store = Path(state.store_path)
        try:
            store.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - environment problem
            raise RuntimeError(
                f"HARNESS_STORE path {store} is not a usable directory: {exc}"
            ) from exc
    logger.info(
        "harness-server %s ready: %d deployment(s), %d principal(s)",
        __version__,
        len(state.deployments),
        len(state.principals),
    )
    try:
        yield
    finally:
        await state.registry.shutdown_all(state.settings)
        logger.info("harness-server stopped")


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    if request.scope.get("type") != "http":  # pragma: no cover - WS path handled inline
        raise exc
    return JSONResponse(exc.envelope(), status_code=exc.status_code, headers=exc.headers)


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if request.scope.get("type") != "http":  # pragma: no cover - defensive
        raise exc
    code, message = _HTTP_CODES.get(exc.status_code, ("HTTP_ERROR", "request failed"))
    body = envelope(code, message, {"status": exc.status_code})
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if request.scope.get("type") != "http":  # pragma: no cover - defensive
        raise exc
    logger.exception("unhandled error serving %s", request.url.path)
    return JSONResponse(
        envelope("INTERNAL_ERROR", "internal server error"),
        status_code=500,
    )


def create_app(
    config: HarnessServerConfig | Mapping[str, Any] | str | os.PathLike[str],
) -> Starlette:
    """Build the Starlette application for a validated deployment config.

    ``config`` accepts an already validated :class:`HarnessServerConfig`, a
    path to a TOML file, or an equivalent mapping (handy for tests).
    """

    if isinstance(config, (str, os.PathLike)):
        config = load_config(config)
    elif isinstance(config, Mapping):
        try:
            config = HarnessServerConfig.model_validate(config)
        except ValueError as exc:
            raise ValueError(f"invalid harness-server configuration: {exc}") from exc
        # Mapping input is the in-memory equivalent of a TOML file; resolve
        # filesystem fields against the current working directory and run the
        # same load-time checks.
        try:
            config = finalize_config(config, Path.cwd())
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
    if not isinstance(config, HarnessServerConfig):  # pragma: no cover - misuse
        raise TypeError(
            "create_app expected HarnessServerConfig, a mapping, or a config path"
        )

    state = HarnessState.from_config(config)
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/v1/agents", list_agents, methods=["GET"]),
        Route("/v1/agents/{agent_id}", get_agent, methods=["GET"]),
        WebSocketRoute("/v1/agents/{agent_id}/acp", acp_websocket),
    ]
    app = Starlette(
        routes=routes,
        lifespan=_lifespan,
        exception_handlers={
            ApiError: _api_error_handler,
            HTTPException: _http_exception_handler,
            Exception: _unhandled_exception_handler,
        },
    )
    app.state.harness = state
    return app
