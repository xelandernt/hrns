"""ACP stdio transport for the same registered session runtime as network hosting."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

import acp
from acp import schema
from pydantic_ai.agent import AbstractAgent

from hrns.shared.config import PermissionMode

from .._types import PrincipalT
from .host import AcpServer
from .models import ModelResolver
from .permission import PermissionPolicy
from .session import AttachmentConfigFunc, SessionConfigFunc, SessionContext
from .store import SessionStore


def run_acp_stdio(
    factory: Callable[
        [SessionContext], AbstractAgent[None, str] | Awaitable[AbstractAgent[None, str]]
    ],
    *,
    principal: PrincipalT | None = None,
    session_store: SessionStore[PrincipalT] | None = None,
    model_resolver: ModelResolver | None = None,
    session_config: SessionConfigFunc[None] | None = None,
    attachment_config: AttachmentConfigFunc[None] | None = None,
    permission_policy: PermissionPolicy | None = None,
    permission_modes: Sequence[PermissionMode] | None = None,
    mcp_capabilities: schema.McpCapabilities | None = None,
    _provider_editor: Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]
    | None = None,
    _remember_model: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    server = AcpServer[PrincipalT]()
    registration = server.add_agent(
        "agent",
        factory=factory,
        session_store=session_store,
        model_resolver=model_resolver,
        session_config=session_config,
        attachment_config=attachment_config,
        permission_policy=permission_policy,
        permission_modes=permission_modes,
        mcp_capabilities=mcp_capabilities,
    )

    async def serve() -> None:
        async with server:
            adapter = registration.connection(principal=principal)
            adapter._provider_editor = _provider_editor
            adapter._remember_model = _remember_model
            try:
                await acp.run_agent(adapter, use_unstable_protocol=True)
            finally:
                adapter.detach()

    asyncio.run(serve())
