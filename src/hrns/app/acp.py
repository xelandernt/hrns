"""ACP adapter entry points built on pydantic-ai-harness."""

from __future__ import annotations

import warnings

from pydantic_ai.agent import AbstractAgent
from pydantic_ai_harness.experimental import HarnessExperimentalWarning

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

from pydantic_ai_harness.experimental.acp import (  # noqa: E402
    InMemorySessionStore,
    PydanticAIACPAgent,
    SessionStore,
    run_acp_stdio_sync,
)

__all__ = ["PydanticAIACPAgent", "create_acp_agent", "run_acp_stdio"]


def create_acp_agent(
    agent: AbstractAgent[None, str],
    *,
    session_store: SessionStore | None = None,
) -> PydanticAIACPAgent[None, str]:
    """Wrap an agent in the upstream ACP adapter.

    ``session_store`` defaults to process-local memory. Pass a durable store if
    sessions should survive a restart; the adapter only needs ``save`` and
    ``load``.
    """
    return PydanticAIACPAgent(
        agent,
        name=agent.name or "hrns",
        session_store=session_store or InMemorySessionStore(),
    )


def run_acp_stdio(
    agent: AbstractAgent[None, str],
    *,
    session_store: SessionStore | None = None,
) -> None:
    """Serve the agent over ACP stdio until the client disconnects."""
    run_acp_stdio_sync(
        agent,
        name=agent.name or "hrns",
        session_store=session_store or InMemorySessionStore(),
    )
