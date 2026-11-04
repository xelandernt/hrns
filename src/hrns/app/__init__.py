"""Thin Pydantic AI application surface.

The backend is deliberately small:

- :mod:`hrns.app.agent` builds one Pydantic AI ``Agent`` from capabilities.
- :mod:`hrns.app.acp` exposes that agent over ACP.
- :mod:`hrns.app.fastapi` mounts ACP on a FastAPI app.

There is no session server, plugin registry, policy catalog, or custom protocol
in this package. ACP sessions belong to the adapter.
"""

from .acp import PydanticAIACPAgent, create_acp_agent, run_acp_stdio
from .agent import build_agent

__all__ = [
    "PydanticAIACPAgent",
    "build_agent",
    "create_acp_agent",
    "run_acp_stdio",
]
