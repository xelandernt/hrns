"""Probe one MCP server for the TUI and report its current connection state."""

from __future__ import annotations

import asyncio
from typing import Any

from hrns.config.mcp import (
    McpServerConfig,
    build_client,
    build_token_store,
    has_tokens,
    server_name,
    server_target,
)
from hrns.protocol import HrnsError
from hrns.protocol.extensions import CommandResult, PresentationPayload

from ...extensions import CommandContext, TextArguments

__all__ = ["status_command"]

_RENDERER = "hrns.mcp.status"


def _looks_like_auth(error: str) -> bool:
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in ("authentication", "unauthorized", "401", "needs auth", "oauth")
    )


async def status_command(context: CommandContext, _arguments: TextArguments) -> CommandResult:
    """Connect briefly, list tools, and report ``connected``/``failed``/``needs auth``."""
    config = context.config
    if not isinstance(config, McpServerConfig):
        raise HrnsError("COMMAND_FAILED", "The MCP plugin received an invalid configuration.")

    name = server_name(context.binding_id)
    data: dict[str, Any] = {
        "name": name,
        "transport": config.transport,
        "target": server_target(config),
        "deferred": config.defer_loading,
        "authenticated": None,
        "state": "connecting",
        "tools": [],
        "error": None,
    }

    try:
        store = build_token_store(config.token_store, config.token_store_path)
        data["authenticated"] = await has_tokens(config, store)
        client = build_client(config, store=store, interactive=False)
        async with asyncio.timeout(config.timeout):
            async with client:
                tools = await client.list_tools()
        allowed = set(config.include_tools)
        blocked = set(config.exclude_tools)
        visible = [
            tool
            for tool in tools
            if (not allowed or getattr(tool, "name", "") in allowed)
            and getattr(tool, "name", "") not in blocked
        ]
        data["tools"] = [
            {
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "") or ""),
            }
            for tool in visible
        ]
        data["state"] = "connected"
    except Exception as exc:  # noqa: BLE001 - surfaced as state, never raised to the client
        message = str(exc) or exc.__class__.__name__
        data["state"] = "needs auth" if _looks_like_auth(message) else "failed"
        data["error"] = message

    tools = data["tools"] if isinstance(data["tools"], list) else []
    if data["state"] == "connected":
        intro = f"MCP server {name}: connected, {len(tools)} tool(s)."
    else:
        intro = f"MCP server {name}: {data['state']}."
        if data["error"]:
            intro += f" {data['error']}"
    names = [str(tool.get("name", "")) for tool in tools if isinstance(tool, dict)]
    fallback = intro + ("\n" + "\n".join(names) if names else "")

    return CommandResult(
        presentation=PresentationPayload(renderer_id=_RENDERER, data=data, fallback=fallback)
    )
