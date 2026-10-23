"""The bundled coding capability: tools plus the context management around them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pydantic_ai.capabilities import AbstractCapability, Caching, Capability, CombinedCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai_harness.compaction import ClearToolResults
from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments
from pydantic_ai_harness.repo_context import RepoContext
from pydantic_ai_harness.tool_output_limits import Band, ToolOutputLimits, Truncate

from ...registry import BindingContext
from ...resources import WORKSPACE, ResourceSlot
from ._instructions import INSTRUCTIONS
from ._needs import NEEDS
from ._tools import MAX_MODEL_TEXT, CoderTools
from .config import CoderConfig

__all__ = ["Coder"]

_WORKSPACE_HELP = (
    "Coder needs a workspace, but none is attached to this run. Bind a workspace resource "
    "for the session (the local agent binds its working directory), or pass `workspace=` "
    "to the run."
)


@dataclass(kw_only=True)
class _RequireWorkspace(AbstractCapability[AgentDepsT]):
    """Fail a run at its start when no workspace is attached."""

    id: str | None = "harness.coder.workspace"

    async def before_run(self, ctx: RunContext[AgentDepsT]) -> None:
        if not ctx.workspace.attached:
            raise UserError(_WORKSPACE_HELP)


class Coder(CombinedCapability[AgentDepsT]):
    """A coding agent's tools, instructions, and context management in the run workspace.

    Six tools run through `ctx.workspace`: `read_file`, `write_file`, `edit_file`,
    `list_files`, `grep`, and `shell`. Writes and edits are hash-checked, credentials and
    version-control internals are read-only, and a run without a workspace fails at its
    start.

    The capability also composes the context management a long coding session needs:
    clearing old tool results, bounded tool output, argument repair, repository instructions,
    and prompt caching when the provider supports it. Delegation is not included.
    """

    needs: ClassVar[tuple[ResourceSlot[object], ...]] = NEEDS

    def __init__(self, config: CoderConfig | None = None) -> None:
        config = config if config is not None else CoderConfig()
        instructions = INSTRUCTIONS
        if config.instructions:
            instructions = f"{INSTRUCTIONS}\n{config.instructions.strip()}\n"
        capabilities: list[AbstractCapability[AgentDepsT]] = [
            *([_RequireWorkspace()] if WORKSPACE in NEEDS else []),
            Capability(id="harness.coder.instructions", instructions=[instructions]),
            CoderTools(config=config),
            ClearToolResults(max_fraction=0.7, id="harness.coder.clear_tool_results"),
            ToolOutputLimits(
                id="harness.coder.tool_output_limits",
                bands=[
                    Band(over=MAX_MODEL_TEXT, action=Truncate(max_chars=MAX_MODEL_TEXT)),
                ],
            ),
            RepairToolArguments(),
        ]
        if config.repo_context:
            capabilities.append(
                RepoContext(id="harness.coder.repo_context", expose_inventory_tool=False)
            )
        if config.caching:
            capabilities.append(Caching(id="harness.coder.caching"))
        super().__init__(
            capabilities,
            id="harness.coder",
            description="Coding tools, repository context, and context management.",
        )


def build_coder(config: CoderConfig, _binding: BindingContext) -> Coder:
    """Plugin factory: turn a validated configuration into the native capability."""
    return Coder(config)
