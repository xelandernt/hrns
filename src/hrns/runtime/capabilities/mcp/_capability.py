"""Turn one configured MCP server into a Pydantic AI capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.capabilities import MCP, AgentCapability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset

from hrns.config.mcp import McpServerConfig, build_token_store, build_transport, server_name

from ...registry import BindingContext

__all__ = ["GuardedMcpToolset", "build_mcp_server"]


@dataclass
class GuardedMcpToolset(WrapperToolset[AgentDepsT]):
    """Keep one unreachable server from failing the whole turn.

    A missing binary or an offline endpoint makes the MCP toolset's ``__aenter__`` raise,
    which would otherwise abort the run before the model is asked anything. The guard
    records the failure, contributes no tools for that turn, and reports the server error
    only if a tool call is somehow attempted.
    """

    server: str = ""
    entered: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def id(self) -> str | None:
        return self.wrapped.id

    @property
    def label(self) -> str:
        return f"MCP {self.server!r}"

    async def __aenter__(self) -> GuardedMcpToolset[AgentDepsT]:
        try:
            await self.wrapped.__aenter__()
        except Exception:
            self.entered = False
            return self
        self.entered = True
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        if not self.entered:
            return None
        self.entered = False
        try:
            return await self.wrapped.__aexit__(*args)
        except Exception:
            return None

    async def get_tools(self, ctx: RunContext[AgentDepsT]) -> dict[str, Any]:
        try:
            return await self.wrapped.get_tools(ctx)
        except Exception:
            return {}

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[AgentDepsT],
        tool: Any,
    ) -> Any:
        try:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        except Exception as exc:
            raise ToolFailed(f"MCP server {self.server!r} failed: {exc}") from exc


def build_mcp_server(config: McpServerConfig, binding: BindingContext) -> AgentCapability:
    """Compose the transport, guard, and ``MCP`` capability for one server."""
    from pydantic_ai.capabilities import Capability
    from pydantic_ai.mcp import MCPToolset

    name = server_name(str(getattr(binding, "binding_id", "mcp.server")))
    try:
        store = build_token_store(config.token_store, config.token_store_path)
        transport = build_transport(config, store=store, interactive=False)
    except (ValueError, OSError) as exc:
        # A missing ``{env:NAME}`` or an unwritable token directory must not make the
        # whole agent uncomposable. The server reports the reason through /mcp instead.
        return Capability[Any](
            id=f"mcp.{name}",
            instructions=[f"MCP server {name} is unavailable: {exc}"],
        )
    toolset: AbstractToolset[object] = MCPToolset(
        transport,
        id=f"mcp.{name}",
        include_instructions=True,
        init_timeout=config.timeout,
    )
    if config.exclude_tools:
        blocked = set(config.exclude_tools)
        toolset = toolset.filtered(lambda _ctx, tool: tool.name not in blocked)
    guarded = GuardedMcpToolset(wrapped=toolset, server=name)
    return MCP(
        native=False,
        local=guarded,
        id=f"mcp.{name}",
        description=config.description or None,
        allowed_tools=config.include_tools or None,
        defer_loading=config.defer_loading,
    )
