"""Embed native agents and network ACP in an existing FastAPI application."""

from ._version import __version__
from .app import AgentServer
from .auth import BearerAuth
from .errors import ACP_SUBPROTOCOL, MAX_MESSAGE_BYTES, ApiError
from .models import AuthenticationDependency, Principal

__all__ = [
    "__version__",
    "ACP_SUBPROTOCOL",
    "MAX_MESSAGE_BYTES",
    "AgentServer",
    "AuthenticationDependency",
    "BearerAuth",
    "ApiError",
    "Principal",
]
