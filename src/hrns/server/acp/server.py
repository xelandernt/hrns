"""ACP stdio transport for the same registered session runtime as network hosting."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import acp
from acp import schema
from pydantic_ai.agent import AbstractAgent

from .host import AcpServer
from .models import ModelResolver
from .session import AttachmentConfigFunc, SessionConfigFunc
from .store import SessionStore


def run_acp_stdio(
    factory: Callable[[], AbstractAgent[None, str]],
    *,
    session_store: SessionStore | None = None,
    model_resolver: ModelResolver | None = None,
    session_config: SessionConfigFunc[None] | None = None,
    attachment_config: AttachmentConfigFunc[None] | None = None,
    mcp_capabilities: schema.McpCapabilities | None = None,
) -> None:
    server = AcpServer()
    registration = server.add_agent(
        "agent",
        factory=factory,
        session_store=session_store,
        model_resolver=model_resolver,
        session_config=session_config,
        attachment_config=attachment_config,
        mcp_capabilities=mcp_capabilities,
    )

    async def serve() -> None:
        async with server:
            adapter = registration.connection()
            try:
                await acp.run_agent(adapter, use_unstable_protocol=True)
            finally:
                adapter.detach()

    asyncio.run(serve())
