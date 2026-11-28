"""Tool approval policy kept outside the protocol adapters."""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai import RunContext, ToolKind
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness.tool_output_limits import GREP_TOOL_RESULT_KIND, READ_TOOL_RESULT_KIND

__all__ = ["READ_ONLY_TOOLS", "require_approval"]

#: Tools that may run without asking. Everything else is marked ``unapproved``
#: so the ACP adapter can turn it into a permission request. ``ask_user_question``
#: is read-only: the question itself is the interaction, not a side effect.
#: ``load_capability``, ``search_tools``, and ``read_skill_resource`` only read
#: instructions and bundled text, so they never prompt; ``run_skill_script`` does.
READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "grep",
        "read_todos",
        "write_todos",
        "ask_user_question",
        "load_capability",
        "search_tools",
        "read_skill_resource",
    }
)

# These operations read spill files or update the current run's own context.
_TOOL_RESULT_KINDS = frozenset(
    {
        READ_TOOL_RESULT_KIND.id,
        GREP_TOOL_RESULT_KIND.id,
        ToolKind("compaction", "prune_tool_result").id,
    }
)


def require_approval(read_only: frozenset[str] = READ_ONLY_TOOLS) -> PrepareTools[None]:
    """Require approval for tools outside the read-only and context-management policy."""

    async def prepare(_ctx: RunContext[None], tools: list[ToolDefinition]) -> list[ToolDefinition]:
        return [
            tool
            if tool.name in read_only or tool.tool_kind in _TOOL_RESULT_KINDS
            else replace(tool, kind="unapproved")
            for tool in tools
        ]

    return PrepareTools(prepare, id="hrns.approval")
