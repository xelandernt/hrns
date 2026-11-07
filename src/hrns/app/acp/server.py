"""Entry points for serving a Pydantic AI agent over ACP."""

from __future__ import annotations

import asyncio

import acp
from acp import schema
from pydantic_ai.agent import AbstractAgent

from .adapter import PydanticAIACPAgent
from .models import ModelResolver
from .session import SessionConfigFunc
from .store import InMemorySessionStore, SessionStore

__all__ = ["create_acp_agent", "run_acp_stdio"]


def create_acp_agent(
    agent: AbstractAgent[None, str],
    *,
    session_store: SessionStore | None = None,
    model_resolver: ModelResolver | None = None,
    session_config: SessionConfigFunc[None] | None = None,
    mcp_capabilities: schema.McpCapabilities | None = None,
) -> PydanticAIACPAgent[None, str]:
    """Wrap an agent in the ACP adapter.

    ``session_store`` defaults to process-local memory. Pass a durable store if sessions
    should survive a restart; the adapter only needs ``save`` and ``load``, plus
    ``remember``, ``list_sessions``, and ``delete`` to advertise ``session/list`` and
    ``session/delete``.

    Pass ``model_resolver`` to advertise ACP's ``model`` session config option and the
    ``providers/*`` methods. It supplies the advertised models, resolves a selection for
    a run, advertises any setting options a model accepts, and owns the provider routing
    a client may change, so a configured route is what the next run uses.

    Pass ``session_config`` to turn the client's MCP declarations into toolsets and
    ``mcp_capabilities`` to advertise which MCP transports the agent accepts.
    """
    return PydanticAIACPAgent(
        agent,
        name=agent.name or "hrns",
        session_store=session_store or InMemorySessionStore(),
        model_resolver=model_resolver,
        session_config=session_config,
        mcp_capabilities=mcp_capabilities,
    )


def run_acp_stdio(
    agent: AbstractAgent[None, str],
    *,
    session_store: SessionStore | None = None,
    model_resolver: ModelResolver | None = None,
    session_config: SessionConfigFunc[None] | None = None,
    mcp_capabilities: schema.McpCapabilities | None = None,
) -> None:
    """Serve the agent over ACP stdio until the client disconnects."""
    adapter = create_acp_agent(
        agent,
        session_store=session_store,
        model_resolver=model_resolver,
        session_config=session_config,
        mcp_capabilities=mcp_capabilities,
    )
    asyncio.run(acp.run_agent(adapter, use_unstable_protocol=True))
