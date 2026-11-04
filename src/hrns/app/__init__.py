"""Thin Pydantic AI application surface.

The backend is deliberately small:

- :mod:`hrns.app.agent` builds one Pydantic AI ``Agent`` from capabilities.
- :mod:`hrns.app.acp` exposes that agent over ACP.
- :mod:`hrns.app.agui` exposes that agent over AG-UI.
- :mod:`hrns.app.fastapi` mounts either protocol on a FastAPI app.

There is no session server, plugin registry, policy catalog, or custom protocol
in this package. ACP sessions and AG-UI threads belong to the adapters.
"""

from .acp import PydanticAIACPAgent, create_acp_agent, run_acp_stdio
from .agent import build_agent
from .agui import PydanticAIAGUIAgent

__all__ = [
    "PydanticAIACPAgent",
    "PydanticAIAGUIAgent",
    "build_agent",
    "create_acp_agent",
    "run_acp_stdio",
]
