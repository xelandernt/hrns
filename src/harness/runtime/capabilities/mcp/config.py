"""Runtime configuration aliases for the MCP capability.

The models live in :mod:`harness.config.mcp` so ``hrns mcp`` can import them without the
runtime extras. This module keeps the capability package's imports conventional.
"""

from __future__ import annotations

from harness.config.mcp import McpServerConfig

__all__ = ["McpServerConfig"]
