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
from hrns.runtime.creation import (
    InitialSessionPolicy,
    SessionCreateRequest,
    SessionCreationOperation,
    SessionCreationResult,
    SessionCreationState,
    SessionNewInput,
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
from .models import (
    AuthenticationDependency,
    InitialSessionPolicyFactory,
    Principal,
    SessionLifecycleResult,
)

__all__ = [
    "__version__",
    "ACP_SUBPROTOCOL",
    "MAX_MESSAGE_BYTES",
    "AgentServer",
    "InitialSessionPolicy",
    "InitialSessionPolicyFactory",
    "SessionCreateRequest",
    "SessionCreationOperation",
    "SessionCreationResult",
    "SessionCreationState",
    "SessionNewInput",
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
    "SessionLifecycleResult",
    "SessionPolicy",
]
