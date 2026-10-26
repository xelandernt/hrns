"""Small ACP mapping helpers used by the host.

Execution, streaming, transcript replay, and approvals belong to the upstream
``PydanticAIACPAgent``. This module only holds the host policy and configuration helpers
that the adapter's public constructor does not provide: workspace validation, stable error
mapping, the standard effort option, and the initial session configuration payload.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

from acp import schema
from acp.exceptions import RequestError
from pydantic import ValidationError

from hrns.config import EFFORT_DEFAULT, EFFORT_OFF, THINKING_LEVELS
from hrns.protocol import BindingChange, HrnsError, changes_adapter

__all__ = [
    "EFFORT_OPTION_ID",
    "MODEL_OPTION_ID",
    "SessionUpdate",
    "canonical_root",
    "effort_option",
    "hrns_rpc_error",
    "initial_configuration_changes",
    "mcp_forbidden_error",
    "validate_requested_cwd",
]

MODEL_OPTION_ID = "model"
EFFORT_OPTION_ID = "effort"

SessionUpdate: TypeAlias = (
    schema.UserMessageChunk
    | schema.AgentMessageChunk
    | schema.AgentThoughtChunk
    | schema.ToolCallStart
    | schema.ToolCallProgress
    | schema.AgentPlanUpdate
    | schema.AvailableCommandsUpdate
    | schema.CurrentModeUpdate
    | schema.ConfigOptionUpdate
    | schema.SessionInfoUpdate
    | schema.UsageUpdate
)

_EFFORT_LABELS = {
    EFFORT_DEFAULT: ("Default", "Let the provider choose its default reasoning effort."),
    EFFORT_OFF: ("Off", "Disable reasoning when the model allows it."),
    "minimal": ("Minimal", "Fastest responses with the least reasoning."),
    "low": ("Low", "Light reasoning for straightforward work."),
    "medium": ("Medium", "Balanced reasoning for everyday work."),
    "high": ("High", "Deeper reasoning for harder work."),
    "xhigh": ("XHigh", "Maximum reasoning; highest cost and latency."),
}


def canonical_root(path: str | os.PathLike[str]) -> str:
    """Canonical absolute form of an allowed workspace root."""
    return os.path.realpath(os.path.expanduser(os.fspath(path)))


def validate_requested_cwd(cwd: Any, *, allowed: Sequence[str] | None, hosted: bool) -> str:
    """Canonicalize a client-requested working directory and enforce host policy."""
    if not isinstance(cwd, str) or not cwd.strip():
        raise HrnsError("INVALID_CONFIGURATION", "cwd must be a non-empty absolute path.")
    if not os.path.isabs(cwd):
        raise HrnsError(
            "INVALID_CONFIGURATION",
            "cwd must be an absolute path.",
            {"cwd": cwd},
        )
    candidate = os.path.realpath(cwd)
    if allowed is not None:
        roots = [canonical_root(root) for root in allowed]
        for root in roots:
            if candidate == root or root == os.sep or candidate.startswith(root + os.sep):
                return candidate
        raise HrnsError(
            "WORKSPACE_UNAVAILABLE",
            "The requested working directory is outside the workspace roots allowed for this deployment.",
            {"cwd": candidate, "allowedRoots": roots, "hosted": hosted},
        )
    return candidate


def effort_option(
    current: str | None,
    *,
    support: str | None,
) -> schema.SessionConfigOptionSelect | None:
    """The unified ``effort`` select option, or ``None`` when the model cannot reason."""
    if support is None:
        return None
    values = [EFFORT_DEFAULT]
    if support != "always":
        values.append(EFFORT_OFF)
    values.extend(THINKING_LEVELS)
    current_value = current if current in values else EFFORT_DEFAULT
    return schema.SessionConfigOptionSelect(
        id=EFFORT_OPTION_ID,
        name="Reasoning effort",
        type="select",
        current_value=current_value,
        options=[
            schema.SessionConfigSelectOption(
                value=value, name=_EFFORT_LABELS[value][0], description=_EFFORT_LABELS[value][1]
            )
            for value in values
        ],
        description="Reasoning effort used by the next turn.",
        category="model",
    )


def initial_configuration_changes(meta: Any) -> list[BindingChange]:
    """Parse ``session/new`` ``_meta.hrns.initialConfiguration`` into changes."""
    if meta is None:
        return []
    if not isinstance(meta, Mapping):
        raise HrnsError("INVALID_CONFIGURATION", "_meta.hrns must be an object.")
    raw = meta.get("initialConfiguration", meta.get("initial_configuration"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HrnsError(
            "INVALID_CONFIGURATION",
            "initialConfiguration must be a list of binding changes.",
            {"field": "initialConfiguration"},
        )
    try:
        return changes_adapter.validate_python(raw)
    except ValidationError as exc:
        raise HrnsError(
            "INVALID_CONFIGURATION",
            "initialConfiguration is not a valid binding-change transaction.",
            {"errors": exc.errors()[:10]},
        ) from exc


def hrns_rpc_error(error: BaseException) -> RequestError:
    """Translate a :class:`HrnsError` (or generic failure) into an ACP RPC error."""
    if isinstance(error, HrnsError):
        return RequestError(-32000, error.message, {"code": error.code, "details": error.details})
    if isinstance(error, ValidationError):
        return RequestError.invalid_params({"errors": exc_errors(error)})
    if isinstance(error, RequestError):
        return error
    return RequestError.internal_error({"details": str(error) or type(error).__name__})


def exc_errors(error: ValidationError) -> list[Any]:
    return error.errors()[:10]


def mcp_forbidden_error(*, hosted: bool, servers: Sequence[Any]) -> HrnsError:
    """Stable rejection for client-supplied MCP servers the host cannot authorize."""
    if hosted:
        return HrnsError(
            "POLICY_DENIED",
            "Client-supplied MCP servers are prohibited in hosted mode; MCP endpoints are "
            "authorized by the deployment.",
            {"mcpServerCount": len(servers)},
        )
    return HrnsError(
        "INVALID_CONFIGURATION",
        "This agent does not accept client-supplied MCP servers; MCP is integrated through "
        "a declared definition resource.",
        {"mcpServerCount": len(servers)},
    )
