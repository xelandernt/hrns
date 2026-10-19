"""``harness-server`` command line entry point.

The transport contract constants are baked into the runner: a 16 MiB maximum
WebSocket message, a 30 second ping interval and a 30 second pong timeout,
matching docs/SERVER.md and the ACP-over-WebSocket deployment.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

import uvicorn

from ._version import __version__
from .app import create_app
from .config import ConfigError, load_config
from .errors import MAX_MESSAGE_BYTES

__all__ = [
    "build_parser",
    "install_log_filters",
    "main",
    "PING_INTERVAL_SECONDS",
    "PING_TIMEOUT_SECONDS",
]

PING_INTERVAL_SECONDS = 30.0
PING_TIMEOUT_SECONDS = 30.0

#: Uvicorn treats a completed ASGI WebSocket *denial response* as an
#: incomplete handshake and logs an ERROR even though the HTTP response was
#: delivered correctly.  The server uses denial responses deliberately for
#: authentication, TLS and subprotocol failures, so the runner filters that
#: one exact upstream message instead of hiding real handshake bugs.
_UVICORN_DENIAL_NOISE = "ASGI callable returned without completing handshake."


class _DenialResponseNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401 - logging API
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        return message != _UVICORN_DENIAL_NOISE


def install_log_filters() -> None:
    """Idempotently silence the Uvicorn denial-response log quirk."""

    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(item, _DenialResponseNoiseFilter) for item in logger.filters):
        logger.addFilter(_DenialResponseNoiseFilter())



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-server",
        description=(
            "Serve the authenticated harness agent catalog and the "
            "ACP-over-WebSocket transport."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="deployments.toml",
        help="deployment configuration file (see docs/SERVER.md)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="bind address (overrides server.host in the config)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="bind port (overrides server.port in the config)",
    )
    parser.add_argument(
        "--root-path",
        default=None,
        help="ASGI root path when served under a subpath by a proxy",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default=None,
        help="uvicorn log level (overrides server.log_level)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the configuration and exit without serving",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"harness-server: {exc}", file=sys.stderr)
        return 2

    settings = config.server
    host = args.host if args.host is not None else settings.host
    port = args.port if args.port is not None else settings.port
    if not 1 <= port <= 65535:
        print("harness-server: --port must be between 1 and 65535", file=sys.stderr)
        return 2
    log_level = args.log_level or settings.log_level
    root_path = args.root_path if args.root_path is not None else settings.root_path

    if args.check:
        print(
            f"harness-server: configuration ok "
            f"({len(config.deployments)} deployment(s), "
            f"{len(config.principals)} principal(s))"
        )
        return 0

    install_log_filters()
    app = create_app(config)
    uvicorn.run(
        app,
        host=host,
        port=port,
        root_path=root_path,
        log_level=log_level,
        ws_max_size=MAX_MESSAGE_BYTES,
        ws_ping_interval=PING_INTERVAL_SECONDS,
        ws_ping_timeout=PING_TIMEOUT_SECONDS,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        ssl_certfile=settings.ssl_certfile,
        ssl_keyfile=settings.ssl_keyfile,
    )
    return 0
