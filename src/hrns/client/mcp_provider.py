"""Client-side MCP-over-ACP provider backed by FastMCP.

The client owns the MCP server session: it either launches a stdio command or
connects to an HTTP/SSE endpoint, then serves that server to an ACP agent over
the stateless ``mcp/message`` binding. The upstream and downstream sessions are
independent; the proxy translates at the method level, so a legacy upstream is
fine while the ACP side is MCP 2026-07-28.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from acp.schema import AcpMcpServer
from fastmcp import Client
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server as LowLevelServer
from mcp.shared.dispatcher import ProgressFnT
from mcp_types import (
    CallToolRequestParams,
    CompleteRequestParams,
    CompleteResult,
    GetPromptRequestParams,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    ReadResourceRequestParams,
    ReadResourceResult,
)
from mcp_types import (
    CallToolResult as McpCallToolResult,
)

from hrns.acp_mcp import (
    MCP_PROTOCOL_VERSION,
    AcpChannel,
    McpOverAcpProvider,
    McpServerOptions,
    McpServerRegistration,
    bind_provider,
    from_mcp_server,
)
from hrns.config.mcp import (
    McpServerSettings,
    McpSettings,
    build_client,
    build_token_store,
)

logger = logging.getLogger(__name__)

__all__ = ["ClientMcpProvider"]


@dataclass
class _Offering:
    registration: McpServerRegistration
    stack: AsyncExitStack


@dataclass
class ClientMcpProvider:
    """Serve configured ``[mcp]`` servers to an ACP agent over ``mcp/message``."""

    settings: McpSettings
    _provider: McpOverAcpProvider = field(default_factory=McpOverAcpProvider)
    _channel: AcpChannel | None = None
    _offerings: list[_Offering] = field(default_factory=list)
    _declarations: list[AcpMcpServer] = field(default_factory=list)
    _token_store: object | None = None

    @property
    def declarations(self) -> tuple[AcpMcpServer, ...]:
        """The ACP declarations to include in ``session/new`` or ``session/load``."""
        return tuple(self._declarations)

    async def start(self, connection: object, initialized: object) -> None:
        """Bind the provider and start every exported server on this connection."""
        await self.close()
        self._provider = McpOverAcpProvider()
        if not _agent_supports_acp(initialized):
            return
        servers = [
            (name, server)
            for name, server in self.settings.servers.items()
            if server.enabled and server.export
        ]
        if not servers:
            return
        self._channel = bind_provider(connection, self._provider)
        for name, server in servers:
            try:
                offering = await self._start_offering(name, server)
            except Exception:
                logger.warning("cannot export MCP server %r over ACP", name, exc_info=True)
                continue
            self._offerings.append(offering)
            self._declarations.append(offering.registration.declaration)

    async def close(self) -> None:
        """Cancel provider operations and stop every upstream connection."""
        for offering in self._offerings:
            try:
                await offering.registration.dispose()
            except Exception:
                logger.debug("disposing MCP registration failed", exc_info=True)
            try:
                await offering.stack.aclose()
            except Exception:
                logger.debug("closing MCP upstream failed", exc_info=True)
        self._offerings.clear()
        self._declarations.clear()
        self._channel = None

    async def _start_offering(self, name: str, server: McpServerSettings) -> _Offering:
        stack = AsyncExitStack()
        try:
            store = self._store_for(server)
            upstream = build_client(server, store=store, interactive=False)
            await stack.enter_async_context(upstream)
            _log_era(name, upstream)
            adapter = from_mcp_server(lambda: _proxy_server(upstream, name))
            assert self._channel is not None
            registration = self._provider.register_server(
                self._channel,
                McpServerOptions(name=name, handler=adapter.handler),
            )
        except BaseException:
            await stack.aclose()
            raise
        return _Offering(registration=registration, stack=stack)

    def _store_for(self, server: McpServerSettings) -> object | None:
        if not server.is_http or server.oauth_config() is None:
            return None
        if self._token_store is None:
            self._token_store = build_token_store(
                self.settings.token_store, self.settings.token_store_path
            )
        return self._token_store


def _agent_supports_acp(initialized: object) -> bool:
    capabilities = getattr(initialized, "agent_capabilities", None)
    mcp = getattr(capabilities, "mcp_capabilities", None)
    return bool(getattr(mcp, "acp", False))


def _log_era(name: str, upstream: Client) -> None:
    """Report the upstream revision so a legacy server is visible, not silent."""
    version = upstream.protocol_version
    if version == MCP_PROTOCOL_VERSION:
        logger.info("MCP server %r negotiated %s", name, version)
        return
    logger.warning(
        "MCP server %r negotiated %s, not %s; exporting it best-effort. "
        "Reverse requests (sampling, elicitation, roots) are not supported over "
        "MCP-over-ACP and fail in the server.",
        name,
        version or "an unknown revision",
        MCP_PROTOCOL_VERSION,
    )


def _proxy_server(upstream: Client, name: str) -> LowLevelServer:
    """Proxy the upstream, advertising only the capabilities it actually has."""
    capabilities = upstream.server_capabilities
    if capabilities is None:
        raise RuntimeError(f"MCP client for {name!r} is not connected")

    has_tools = capabilities.tools is not None
    has_resources = capabilities.resources is not None
    has_prompts = capabilities.prompts is not None
    has_completions = capabilities.completions is not None

    async def on_list_tools(
        _ctx: ServerRequestContext[object, object], _params: object
    ) -> ListToolsResult:
        return ListToolsResult(tools=await upstream.list_tools())

    async def on_call_tool(
        ctx: ServerRequestContext[object, object], params: CallToolRequestParams
    ) -> McpCallToolResult:
        result = await upstream.call_tool(
            params.name,
            params.arguments,
            raise_on_error=False,
            progress_handler=_progress_handler(ctx),
        )
        return McpCallToolResult(
            content=result.content,
            structured_content=result.structured_content,
            meta=result.meta,
            is_error=result.is_error,
        )

    async def on_list_resources(
        _ctx: ServerRequestContext[object, object], _params: object
    ) -> ListResourcesResult:
        return ListResourcesResult(resources=await upstream.list_resources())

    async def on_read_resource(
        _ctx: ServerRequestContext[object, object], params: ReadResourceRequestParams
    ) -> ReadResourceResult:
        return ReadResourceResult(contents=await upstream.read_resource(params.uri))

    async def on_list_resource_templates(
        _ctx: ServerRequestContext[object, object], _params: object
    ) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(
            resource_templates=await upstream.list_resource_templates()
        )

    async def on_list_prompts(
        _ctx: ServerRequestContext[object, object], _params: object
    ) -> ListPromptsResult:
        return ListPromptsResult(prompts=await upstream.list_prompts())

    async def on_get_prompt(
        ctx: ServerRequestContext[object, object], params: GetPromptRequestParams
    ) -> GetPromptResult:
        return await upstream.get_prompt(params.name, params.arguments)

    async def on_completion(
        ctx: ServerRequestContext[object, object], params: CompleteRequestParams
    ) -> CompleteResult:
        completion = await upstream.complete(
            params.ref,
            {"name": params.argument.name, "value": params.argument.value},
        )
        return CompleteResult(completion=completion)

    return LowLevelServer(
        name,
        on_list_tools=on_list_tools if has_tools else None,
        on_call_tool=on_call_tool if has_tools else None,
        on_list_resources=on_list_resources if has_resources else None,
        on_read_resource=on_read_resource if has_resources else None,
        on_list_resource_templates=(on_list_resource_templates if has_resources else None),
        on_list_prompts=on_list_prompts if has_prompts else None,
        on_get_prompt=on_get_prompt if has_prompts else None,
        on_completion=on_completion if has_completions else None,
    )


def _progress_handler(ctx: ServerRequestContext[object, object]) -> ProgressFnT | None:
    meta = ctx.meta
    request_id = ctx.request_id
    token = None
    if isinstance(meta, dict):
        token = meta.get("progressToken", meta.get("progress_token"))
    if not isinstance(token, (str, int)) or isinstance(token, bool):
        return None

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        related = str(request_id) if request_id is not None else None
        await ctx.session.send_progress_notification(
            token, progress, total, message, related_request_id=related
        )

    return on_progress
