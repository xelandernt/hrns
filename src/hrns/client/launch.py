"""Turn a profile into a stdio ACP transport."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .constants import CONNECT_TIMEOUT
from .errors import HrnsClientError
from .profiles import Profile
from .transport import (
    AgentTransport,
    StdioAgentTransport,
    WebSocketAgentTransport,
    check_websocket_url,
)

__all__ = [
    "ConnectionTarget",
    "local_cwd",
    "remote_cwd",
    "remote_target",
    "transport_for",
]


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """A fully resolved connection: what to launch, where, and how to display it."""

    name: str
    kind: str  # "local" | "remote"
    profile: Profile
    cwd: str

    def location(self) -> dict[str, str]:
        if self.kind == "remote":
            return {
                "kind": "remote",
                "name": self.name,
                "server": self.profile.url,
                "cwd": self.cwd,
            }
        return {"kind": "local", "name": self.name, "cwd": self.cwd}

    def location_text(self) -> str:
        if self.kind == "remote":
            return f"remote · {self.profile.url} · {self.cwd}"
        return f"local · {self.name} · {self.cwd}"


def local_cwd(profile: Profile, override: str | None = None) -> str:
    """The local working directory for a stdio session."""
    candidate = override or profile.cwd or os.getcwd()
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise HrnsClientError(f"The working directory must be absolute: {candidate}")
    return str(path)


def remote_cwd(profile: Profile, override: str | None = None) -> str:
    """The workspace path sent to a remote agent; never rewritten locally."""
    candidate = override or profile.remote_cwd or os.getcwd()
    if not Path(candidate).is_absolute():
        raise HrnsClientError(f"The remote working directory must be absolute: {candidate}")
    return candidate


def remote_target(
    url: str,
    *,
    name: str = "remote",
    token: str = "",
    cwd: str = "",
) -> ConnectionTarget:
    """Build a remote target from a URL and optional token reference."""
    profile = Profile(
        name=name,
        transport="server",
        url=check_websocket_url(url),
        token=token,
        remote_cwd=cwd,
    )
    return ConnectionTarget(
        name=name,
        kind="remote",
        profile=profile,
        cwd=remote_cwd(profile),
    )


def transport_for(target: ConnectionTarget) -> AgentTransport:
    """Build the transport for a resolved target. A new instance every reconnect."""
    profile = target.profile
    if target.kind == "remote":
        return WebSocketAgentTransport(
            check_websocket_url(profile.url),
            token_ref=profile.token,
            name=profile.name,
            connect_timeout=CONNECT_TIMEOUT,
        )
    return StdioAgentTransport(
        profile.command,
        profile.args,
        cwd=target.cwd,
        env_refs=dict(profile.env),
        name=profile.name,
        connect_timeout=CONNECT_TIMEOUT,
    )
