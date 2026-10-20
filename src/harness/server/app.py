"""Application assembly: ``create_app``, lifespan and error envelopes."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ._version import __version__
from .api import router as api_router
from .config import (
    ConfigError,
    HarnessServerConfig,
    finalize_config,
    load_config,
)
from .errors import ApiError, envelope
from .proxy import router as proxy_router
from .state import HarnessState

__all__ = ["HarnessState", "create_app"]

logger = logging.getLogger("harness.server")

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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
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


async def _api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):  # pragma: no cover - defensive
        raise exc
    if request.scope.get("type") != "http":  # pragma: no cover - WS path handled inline
        raise exc
    return JSONResponse(exc.envelope(), status_code=exc.status_code, headers=exc.headers)


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):  # pragma: no cover - defensive
        raise exc
    if request.scope.get("type") != "http":  # pragma: no cover - defensive
        raise exc
    code, message = _HTTP_CODES.get(exc.status_code, ("HTTP_ERROR", "request failed"))
    body = envelope(code, message, {"status": exc.status_code})
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


async def _validation_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - defensive
        raise exc
    if request.scope.get("type") != "http":  # pragma: no cover - defensive
        raise exc
    code, message = _HTTP_CODES[422]
    details = {"errors": jsonable_encoder(exc.errors())}
    return JSONResponse(envelope(code, message, details), status_code=422)


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
) -> FastAPI:
    """Build the FastAPI application for a validated deployment config.

    ``config`` accepts an already validated :class:`HarnessServerConfig`, a
    path to a TOML file, or an equivalent mapping (handy for tests and
    embedding).
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
    app = FastAPI(
        title="harness-server",
        version=__version__,
        description=(
            "Authenticated catalog of immutable agent deployment revisions and "
            "the ACP-over-WebSocket transport."
        ),
        lifespan=_lifespan,
        redirect_slashes=False,
        exception_handlers={
            ApiError: _api_error_handler,
            HTTPException: _http_exception_handler,
            RequestValidationError: _validation_error_handler,
            Exception: _unhandled_exception_handler,
        },
    )
    app.state.harness = state
    app.include_router(api_router)
    app.include_router(proxy_router)
    return app
