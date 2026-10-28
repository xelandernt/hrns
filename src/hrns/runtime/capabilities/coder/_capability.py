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
from ._instructions import instructions
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

    id: str | None = "hrns.coder.workspace"

    async def before_run(self, ctx: RunContext[AgentDepsT]) -> None:
        if not ctx.workspace.attached:
            raise UserError(_WORKSPACE_HELP)


class Coder(CombinedCapability[AgentDepsT]):
    """A coding agent's tools, instructions, and context management in the run workspace.

    Five tools run through `ctx.workspace`: `read_file`, `apply_changes`,
    `list_files`, `grep`, and `shell`. File changes are preflighted as a batch, credentials and
    version-control internals are read-only, and a run without a workspace fails at its
    start.

    The capability also composes the context management a long coding session needs:
    clearing old tool results, bounded tool output, argument repair, repository instructions,
    and prompt caching when the provider supports it. Delegation is not included.
    """

    needs: ClassVar[tuple[ResourceSlot[object], ...]] = NEEDS

    def __init__(
        self,
        config: CoderConfig | None = None,
        *,
        shadowed: frozenset[str] = frozenset(),
    ) -> None:
        config = config if config is not None else CoderConfig()
        # A client-origin binding can take over a tool this capability would contribute. That
        # is a fact about the composition, so it is applied here instead of being written back
        # into the operator's configuration: disabling the other binding restores this one.
        tool_config = config.model_copy(update={"shell": False}) if "shell" in shadowed else config
        instructions_text = instructions(client_terminal=not tool_config.shell)
        if config.instructions:
            instructions_text = f"{instructions_text}\n{config.instructions.strip()}\n"
        capabilities: list[AbstractCapability[AgentDepsT]] = [
            *([_RequireWorkspace()] if WORKSPACE in NEEDS else []),
            Capability(id="hrns.coder.instructions", instructions=[instructions_text]),
            CoderTools(config=tool_config),
            ClearToolResults(max_fraction=0.7, id="hrns.coder.clear_tool_results"),
            ToolOutputLimits(
                id="hrns.coder.tool_output_limits",
                bands=[
                    Band(over=MAX_MODEL_TEXT, action=Truncate(max_chars=MAX_MODEL_TEXT)),
                ],
            ),
            RepairToolArguments(),
        ]
        if config.repo_context:
            capabilities.append(
                RepoContext(id="hrns.coder.repo_context", expose_inventory_tool=False)
            )
        if config.caching:
            capabilities.append(Caching(id="hrns.coder.caching"))
        super().__init__(
            capabilities,
            id="hrns.coder",
            description="Coding tools, repository context, and context management.",
        )


def build_coder(config: CoderConfig, binding: BindingContext) -> Coder:
    """Plugin factory: turn a validated configuration into the native capability."""
    return Coder(config, shadowed=binding.shadowed_tools)
