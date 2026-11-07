"""Client-side MCP-over-ACP provider backed by FastMCP.

The client owns the MCP server session: it either launches a stdio command or
connects to an HTTP/SSE endpoint, then serves that server to an ACP agent over
the stateless ``mcp/message`` binding. The upstream and downstream sessions are
independent; the proxy translates at the method level, so a legacy upstream is
fine while the ACP side is MCP 2026-07-28.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import StrEnum

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

__all__ = ["ClientMcpProvider", "ConsentDecision"]


@dataclass
class _Offering:
    registration: McpServerRegistration
    stack: AsyncExitStack


class ConsentDecision(StrEnum):
    """What the client's user decided about exporting one server."""

    ALLOW_ONCE = "once"
    """Declare it for this session only."""
    ALLOW_SESSION = "session"
    """Declare it for every session until this connection closes."""
    ALWAYS_ALLOW = "always"
    """Declare it now and persist ``always_allow = true`` in the config."""
    DENY = "deny"
    """Do not declare it for this session."""


ConsentHandler = Callable[[str, McpServerSettings], Awaitable[ConsentDecision]]
"""Ask the client's user whether an exported server may be offered to the agent."""


@dataclass
class ClientMcpProvider:
    """Serve configured ``[mcp]`` servers to an ACP agent over ``mcp/message``."""

    settings: McpSettings
    consent: ConsentHandler | None = None
    """Session-start consent prompt; ``None`` denies servers not pre-approved."""

    persist: Callable[[], object] | None = None
    """Write the config document after an ``always allow`` decision."""

    _provider: McpOverAcpProvider = field(default_factory=McpOverAcpProvider)
    _channel: AcpChannel | None = None
    _exported: dict[str, McpServerSettings] = field(default_factory=dict)
    _offerings: list[_Offering] = field(default_factory=list)
    _started: dict[str, McpServerRegistration] = field(default_factory=dict)
    _granted: set[str] = field(default_factory=set)
    _denied: set[str] = field(default_factory=set)
    _prepared: tuple[AcpMcpServer, ...] | None = None
    _token_store: object | None = None

    async def prepare(self) -> None:
        """Resolve consent for the next session before it is timed out.

        The TUI calls this before ``session/new`` so the user's thinking time is
        not charged against the connection timeout.
        """
        self._prepared = await self._resolve()

    async def declarations(self) -> tuple[AcpMcpServer, ...]:
        """The ACP declarations for one session, asking consent for each server."""
        prepared, self._prepared = self._prepared, None
        if prepared is not None:
            return prepared
        return await self._resolve()

    async def _resolve(self) -> tuple[AcpMcpServer, ...]:
        declarations: list[AcpMcpServer] = []
        for name, server in self._exported.items():
            if not await self._approve(name, server):
                continue
            registration = self._started.get(name)
            if registration is None:
                await self._offer(name, server)
                registration = self._started.get(name)
            if registration is not None:
                declarations.append(registration.declaration)
        return tuple(declarations)

    async def start(self, connection: object, initialized: object) -> None:
        """Bind the provider and start the servers that do not need consent."""
        await self.close()
        self._provider = McpOverAcpProvider()
        if not _agent_supports_acp(initialized):
            return
        self._exported = {
            name: server
            for name, server in self.settings.servers.items()
            if server.enabled and server.export
        }
        if not self._exported:
            return
        self._channel = bind_provider(connection, self._provider)
        for name, server in self._exported.items():
            if server.always_allow:
                await self._offer(name, server)

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
        self._started.clear()
        self._granted.clear()
        self._denied.clear()
        self._prepared = None
        self._exported.clear()
        self._channel = None

    async def _approve(self, name: str, server: McpServerSettings) -> bool:
        if server.always_allow or name in self._granted:
            return True
        if name in self._denied:
            return False
        if self.consent is None:
            logger.warning(
                "MCP server %r is exported but not approved; set "
                "[mcp.servers.%s] always_allow = true to export it without a prompt",
                name,
                name,
            )
            self._denied.add(name)
            return False
        decision = await self.consent(name, server)
        if decision is ConsentDecision.DENY:
            logger.info("MCP server %r not exported: consent denied", name)
            self._denied.add(name)
            return False
        if decision is ConsentDecision.ALLOW_SESSION:
            self._granted.add(name)
        elif decision is ConsentDecision.ALWAYS_ALLOW:
            self._granted.add(name)
            server.always_allow = True
            if self.persist is not None:
                try:
                    self.persist()
                except Exception:
                    logger.warning(
                        "could not save always_allow for MCP server %r", name, exc_info=True
                    )
        return True

    async def _offer(self, name: str, server: McpServerSettings) -> None:
        try:
            offering = await self._start_offering(name, server)
        except Exception:
            logger.warning("cannot export MCP server %r over ACP", name, exc_info=True)
            return
        self._offerings.append(offering)
        self._started[name] = offering.registration

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
