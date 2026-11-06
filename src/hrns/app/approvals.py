"""Tool approval policy kept outside the protocol adapters."""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai import RunContext
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.tools import ToolDefinition

__all__ = ["READ_ONLY_TOOLS", "require_approval"]

#: Tools that may run without asking. Everything else is marked ``unapproved``
#: so the ACP adapter can turn it into a permission request.
READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "grep", "read_todos", "write_todos"})


def require_approval(read_only: frozenset[str] = READ_ONLY_TOOLS) -> PrepareTools[None]:
    """Mark every non-read-only function tool as requiring approval."""

    async def prepare(_ctx: RunContext[None], tools: list[ToolDefinition]) -> list[ToolDefinition]:
        return [
            tool if tool.name in read_only else replace(tool, kind="unapproved") for tool in tools
        ]

    return PrepareTools(prepare, id="hrns.approval")
