"""MCP servers an ACP client declared, exposed as Pydantic AI toolsets.

Both standard ACP declarations (stdio/HTTP/SSE) and client-provided
MCP-over-ACP servers become ordinary ``MCPToolset`` instances. The adapter only
passes the session's declarations and, for ``type: "acp"``, the connection's
caller and channel.
"""

from __future__ import annotations

from acp.schema import AcpMcpServer, HttpMcpServer, McpServerStdio, SseMcpServer
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

from hrns.shared.acp_mcp import AcpMcpClientTransport
from hrns.shared.config.mcp import McpServerSettings, build_transport

from .session import AcpSession

__all__ = ["acp_mcp_toolsets"]


def acp_mcp_toolsets(session: AcpSession) -> tuple[AbstractToolset[object], ...]:
    """Build one ``MCPToolset`` per server the client declared for this session."""
    toolsets: list[AbstractToolset[object]] = []
    for server in session.mcp_servers:
        if isinstance(server, AcpMcpServer):
            if session.mcp_caller is None or session.mcp_channel is None:
                raise ValueError(
                    "the ACP session has no MCP caller/channel for a client-provided server"
                )
            transport = AcpMcpClientTransport(
                session.mcp_caller,
                session.mcp_channel,
                server.server_id,
                name=server.name,
            )
            toolsets.append(MCPToolset(transport, id=server.name))
        elif isinstance(server, McpServerStdio):
            settings = McpServerSettings(
                command=server.command,
                args=list(server.args),
                env={item.name: item.value for item in server.env or []},
                cwd=session.cwd,
                oauth=False,
            )
            toolsets.append(MCPToolset(build_transport(settings), id=server.name))
        elif isinstance(server, HttpMcpServer):
            transport = StreamableHttpTransport(
                url=server.url,
                headers={item.name: item.value for item in server.headers or []},
            )
            toolsets.append(MCPToolset(transport, id=server.name))
        elif isinstance(server, SseMcpServer):
            transport = SSETransport(
                url=server.url,
                headers={item.name: item.value for item in server.headers or []},
            )
            toolsets.append(MCPToolset(transport, id=server.name))
    return tuple(toolsets)
