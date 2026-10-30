"""Embed native agents and network ACP in an existing FastAPI application."""

from hrns.runtime import (
    DefaultSessionPolicy,
    DeploymentRecord,
    FanoutClient,
    LifecycleEvent,
    NoopLifecycleHooks,
    PolicyAction,
    PolicyDecision,
    PromptState,
    QueuedPrompt,
    RequestContext,
    SessionAccess,
    SessionContext,
    SessionLease,
    SessionLifecycleContext,
    SessionLifecycleHooks,
    SessionPolicy,
)

from ._version import __version__
from .app import AgentServer
from .auth import BearerAuth
from .connections import ConnectionInfo, ConnectionRegistry
from .deployments import (
    DeploymentConflict,
    DeploymentRegistry,
    DeploymentRevision,
    MissingDeployment,
)
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
    "ConnectionInfo",
    "ConnectionRegistry",
    "DefaultSessionPolicy",
    "DeploymentConflict",
    "DeploymentRecord",
    "DeploymentRegistry",
    "DeploymentRevision",
    "FanoutClient",
    "LifecycleEvent",
    "MissingDeployment",
    "NoopLifecycleHooks",
    "PolicyAction",
    "PolicyDecision",
    "Principal",
    "PromptState",
    "QueuedPrompt",
    "RequestContext",
    "SessionAccess",
    "SessionContext",
    "SessionLease",
    "SessionLifecycleContext",
    "SessionLifecycleHooks",
    "SessionPolicy",
]
