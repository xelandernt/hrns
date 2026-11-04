"""AG-UI adapter surface built on pydantic-ai's AG-UI integration."""

from __future__ import annotations

from pydantic_ai.agent import AbstractAgent
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["PydanticAIAGUIAgent"]


class PydanticAIAGUIAgent:
    """Wrap an agent in pydantic-ai's AG-UI HTTP adapter.

    This is a per-request adapter: one ``POST`` carrying ``RunAgentInput``
    produces one AG-UI event stream. It does not own threads, subscriber fan-out,
    or replay. Multi-client thread management belongs above this class.

    Install the protocol dependency with::

        pydantic-ai-slim[ag-ui]
    """

    def __init__(self, agent: AbstractAgent[None, str]) -> None:
        self.agent = agent

    async def dispatch_request(self, request: Request) -> Response:
        """Run the agent for one AG-UI request and stream its events."""
        try:
            from pydantic_ai.ui.ag_ui import AGUIAdapter
        except ImportError as exc:  # pragma: no cover - dependency error path
            raise RuntimeError(
                "AG-UI support requires the ag-ui extra: pip install 'pydantic-ai-slim[ag-ui]'"
            ) from exc

        return await AGUIAdapter.dispatch_request(request, agent=self.agent)
