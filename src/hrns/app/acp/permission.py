"""Tool-approval permission types: the call under review and the policy that scopes decisions."""

from __future__ import annotations

import json
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass

from .serialize import jsonable


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
