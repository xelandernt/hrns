"""Mount ACP and AG-UI on one FastAPI app.

Run with:

    HRNS_MODEL=openai:gpt-6.1-sol uvicorn examples.fastapi_app:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI, Request, WebSocket
from pydantic_ai.agent import AbstractAgent

from hrns.app.agent import build_agent
from hrns.app.fastapi import mount_acp, mount_agui


async def authenticate(connection: WebSocket | Request) -> str | None:
    """Return the caller's identity, or ``None`` to refuse the connection."""
    token = connection.headers.get("authorization")
    if token == "Bearer example-token":
        return "example-user"
    return None


def agent_factory(principal: str | None) -> AbstractAgent[None, str]:
    # Use ``principal`` to bind identity into the agent's tools or deps.
    return build_agent()


app = FastAPI(title="hrns example")

# One fresh ACP adapter per authenticated WebSocket connection.
mount_acp(app, agent_factory, path="/acp", authenticate=authenticate)

# One fresh agent and AG-UI run per authenticated POST.
mount_agui(app, agent_factory, path="/agui", authenticate=authenticate)
