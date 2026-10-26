"""Runtime configuration aliases for the MCP capability.

The models live in :mod:`hrns.config.mcp` so ``hrns mcp`` can import them without
pulling in the runtime. This module keeps the capability package's imports conventional.
"""

from __future__ import annotations

from hrns.config.mcp import McpServerConfig

__all__ = ["McpServerConfig"]
