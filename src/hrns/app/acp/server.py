"""Entry points for serving a Pydantic AI agent over ACP."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Literal

import acp
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.models import KnownModelName, Model

from .adapter import PydanticAIACPAgent
from .store import InMemorySessionStore, SessionStore

__all__ = ["ACPModels", "create_acp_agent", "run_acp_stdio"]

ACPModels = Sequence[KnownModelName | str] | Literal["all"]
"""Model ids advertised through ACP's ``model`` session config option."""


def create_acp_agent(
    agent: AbstractAgent[None, str],
    *,
    session_store: SessionStore | None = None,
    models: ACPModels | None = None,
    model_resolver: Callable[[str], Model | str] | None = None,
) -> PydanticAIACPAgent[None, str]:
    """Wrap an agent in the ACP adapter.

    ``session_store`` defaults to process-local memory. Pass a durable store if sessions
    should survive a restart; the adapter only needs ``save`` and ``load``, plus
    ``remember``, ``list_sessions``, and ``delete`` to advertise ``session/list`` and
    ``session/delete``. Pass ``models`` to advertise ACP's ``model`` session config option;
    clients can then switch models without reconnecting. ``model_resolver`` maps an
    advertised id to a model when the id is not one Pydantic AI resolves itself.
    """
    return PydanticAIACPAgent(
        agent,
        name=agent.name or "hrns",
        session_store=session_store or InMemorySessionStore(),
        models=models,
        model_resolver=model_resolver,
    )


def run_acp_stdio(
    agent: AbstractAgent[None, str],
    *,
    session_store: SessionStore | None = None,
    models: ACPModels | None = None,
    model_resolver: Callable[[str], Model | str] | None = None,
) -> None:
    """Serve the agent over ACP stdio until the client disconnects."""
    adapter = create_acp_agent(
        agent,
        session_store=session_store,
        models=models,
        model_resolver=model_resolver,
    )
    asyncio.run(acp.run_agent(adapter, use_unstable_protocol=True))
