"""Tool-approval permission types: the call under review and the policy that scopes decisions."""

from __future__ import annotations

import json
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass

from acp import schema

from hrns.shared.config import PERMISSION_MODES, PermissionMode

from .serialize import jsonable

#: Tool kinds auto-approved in ``reads`` permission mode (matches client ``READ_ONLY_KINDS``).
READ_ONLY_TOOL_KINDS: frozenset[schema.ToolKind] = frozenset({"read", "search", "think"})

#: Default permission modes advertised as the ``permissions`` session config option.
DEFAULT_PERMISSION_MODES: tuple[PermissionMode, ...] = PERMISSION_MODES


def validate_permission_modes(modes: Sequence[PermissionMode]) -> tuple[PermissionMode, ...]:
    """Validate and deduplicate configured session permission modes.

    An empty sequence disables the server-owned ``permissions`` config option (the server
    always asks the client unless remembered via ``always_allow`` / ``always_reject``).
    A non-empty sequence must contain ``"ask"`` so new sessions always start in ``"ask"``.
    """
    unique: list[PermissionMode] = []
    for mode in modes:
        if mode not in PERMISSION_MODES:
            raise ValueError(
                f"Unknown permission mode {mode!r}; choose from {', '.join(PERMISSION_MODES)}"
            )
        if mode not in unique:
            unique.append(mode)
    if unique and "ask" not in unique:
        raise ValueError("Configured permission_modes must include 'ask'")
    return tuple(unique)


@dataclass(frozen=True, kw_only=True)
class ToolCallPermission:
    """A tool call awaiting the client's approval, passed to a ``permission_policy``."""

    tool_name: str
    tool_call_id: str
    args: Mapping[str, object]


# Maps a tool call to the scope key under which an "always allow"/"always reject" decision is
# remembered for the session. Two calls with the same key share a remembered decision.
PermissionPolicy = Callable[[ToolCallPermission], Hashable]


def default_permission_scope(call: ToolCallPermission) -> Hashable:
    """Scope an "always" decision to the exact call: the same tool with the same arguments."""
    return (call.tool_name, json.dumps(jsonable(call.args), sort_keys=True))
