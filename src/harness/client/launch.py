"""Turning profiles into live transports.

Shared by the CLI, the Textual picker and the bridge so the location shown
to the user is derived from the same resolved connection settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import AgentDeployment, CatalogClient
from .constants import CONNECT_TIMEOUT, WS_SUBPROTOCOL
from .errors import HarnessClientError
from .profiles import Profile
from .transport import AgentTransport, StdioAgentTransport, WebSocketAgentTransport

__all__ = [
    "ConnectionTarget",
    "catalog_for",
    "default_remote_cwd",
    "local_cwd",
    "resolve_agent",
    "transport_for",
]


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """A fully resolved connection: what to launch, where, and how to display it."""

    name: str
    kind: str  # "local" | "remote"
    profile: Profile
    agent: AgentDeployment | None
    cwd: str

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id if self.agent else self.profile.agent

    def location(self) -> dict[str, str]:
        if self.kind == "local":
            return {"kind": "local", "name": self.name, "cwd": self.cwd}
        return {
            "kind": "remote",
            "name": self.name,
            "server": self.profile.url,
            "cwd": self.cwd,
            "agent": self.agent.label if self.agent else self.agent_id,
        }

    def location_text(self) -> str:
        if self.kind == "local":
            return f"local · {self.name} · {self.cwd}"
        label = self.agent.label if self.agent else self.agent_id
        return f"{self.profile.url} · {label} · {self.cwd}"


def local_cwd(profile: Profile, override: str | None = None) -> str:
    """The local working directory for a stdio session."""
    candidate = override or profile.cwd or os.getcwd()
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise HarnessClientError(f"The working directory must be absolute: {candidate}")
    return str(path)


def default_remote_cwd(agent: AgentDeployment, override: str | None = None) -> str:
    """A remote workspace path; never silently substitute a local path."""
    candidate = override or agent.default_working_directory
    if not candidate and agent.workspaces:
        candidate = agent.workspaces[0]
    if not candidate:
        raise HarnessClientError(
            f"The agent '{agent.label}' does not advertise a remote workspace. "
            "Choose one with --cwd from its catalog descriptor."
        )
    return candidate


def catalog_for(profile: Profile, *, http_transport: Any = None) -> CatalogClient:
    if not profile.url:
        raise HarnessClientError(f"Profile '{profile.name}' has no server URL.")
    return CatalogClient(profile.url, token_ref=profile.token, http_transport=http_transport)


async def resolve_agent(
    profile: Profile,
    reference: str | None,
    *,
    http_transport: Any = None,
) -> AgentDeployment:
    """Resolve a stable deployment ID *or* a human-readable catalog name."""
    reference = (reference or profile.agent or "").strip()
    if not reference:
        raise HarnessClientError(
            f"No agent selected for server profile '{profile.name}'. "
            "Pass --agent ID or set `agent` in the profile."
        )
    async with catalog_for(profile, http_transport=http_transport) as catalog:
        direct_error: HarnessClientError | None = None
        try:
            return await catalog.get_agent(reference)
        except HarnessClientError as exc:
            direct_error = exc
        # Fall back to the full paginated catalog for human-readable names.
        matches: list[AgentDeployment] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = await catalog.list_agents(cursor=cursor, limit=100)
            for item in page.items:
                if reference.lower() in {item.agent_id.lower(), item.name.lower()}:
                    matches.append(item)
            cursor = page.next_cursor
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            recommended = [item for item in matches if item.recommended]
            if len(recommended) == 1:
                return recommended[0]
            ids = ", ".join(item.agent_id for item in matches)
            raise HarnessClientError(
                f"Agent name '{reference}' is ambiguous. Use a deployment id: {ids}"
            )
        raise HarnessClientError(
            f"No accessible agent '{reference}' on {profile.url}."
            + (f" ({direct_error.message})" if direct_error else "")
        )


def transport_for(target: ConnectionTarget) -> AgentTransport:
    """Build the transport for a resolved target. New instance every reconnect."""
    profile = target.profile
    if target.kind == "local":
        env_refs = {k: v for k, v in profile.env.items()}
        return StdioAgentTransport(
            profile.command,
            profile.args,
            cwd=target.cwd,
            env_refs=env_refs,
            name=profile.name,
            connect_timeout=CONNECT_TIMEOUT,
        )
    if not target.agent:
        raise HarnessClientError("A remote target needs a resolved agent deployment.")
    catalog = catalog_for(profile)
    return WebSocketAgentTransport(
        catalog.websocket_url(target.agent.agent_id),
        token_ref=profile.token,
        name=profile.name,
        connect_timeout=CONNECT_TIMEOUT,
    )


def remote_handshake_headers() -> dict[str, str]:
    """Documentation hook for the subprotocol used above the catalog auth."""
    return {"Sec-WebSocket-Protocol": WS_SUBPROTOCOL}
