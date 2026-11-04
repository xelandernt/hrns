"""Turn a profile into a stdio ACP transport."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .constants import CONNECT_TIMEOUT
from .errors import HrnsClientError
from .profiles import Profile
from .transport import AgentTransport, StdioAgentTransport

__all__ = ["ConnectionTarget", "local_cwd", "transport_for"]


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """A fully resolved stdio connection."""

    name: str
    kind: str
    profile: Profile
    cwd: str

    def location(self) -> dict[str, str]:
        return {"kind": "local", "name": self.name, "cwd": self.cwd}

    def location_text(self) -> str:
        return f"local · {self.name} · {self.cwd}"


def local_cwd(profile: Profile, override: str | None = None) -> str:
    """The local working directory for a stdio session."""
    candidate = override or profile.cwd or os.getcwd()
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise HrnsClientError(f"The working directory must be absolute: {candidate}")
    return str(path)


def transport_for(target: ConnectionTarget) -> AgentTransport:
    """Build the stdio transport for a resolved target."""
    profile = target.profile
    return StdioAgentTransport(
        profile.command,
        profile.args,
        cwd=target.cwd,
        env_refs=dict(profile.env),
        name=profile.name,
        connect_timeout=CONNECT_TIMEOUT,
    )
