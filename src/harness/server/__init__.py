"""Harness server: authenticated catalog and ACP WebSocket hosting.

The server process owns deployments, bearer-token authentication, worker
launch policy and bounded ACP framing.  It intentionally has no dependency
on the harness runtime or Textual: agent execution happens in each
deployment's dedicated environment behind a ``harness-host`` stdio worker.
"""

from ._version import __version__
from .app import HarnessState, create_app
from .config import (
    ConfigError,
    DeploymentConfig,
    HarnessServerConfig,
    PrincipalConfig,
    ServerSettings,
    load_config,
    sha256_hex,
)
from .errors import ACP_SUBPROTOCOL, MAX_MESSAGE_BYTES, ApiError, envelope

__all__ = [
    "__version__",
    "ACP_SUBPROTOCOL",
    "MAX_MESSAGE_BYTES",
    "ApiError",
    "ConfigError",
    "DeploymentConfig",
    "HarnessServerConfig",
    "HarnessState",
    "PrincipalConfig",
    "ServerSettings",
    "create_app",
    "envelope",
    "load_config",
    "sha256_hex",
]
