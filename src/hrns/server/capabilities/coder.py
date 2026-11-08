"""The bundled coding capability: the harness `Coder` plus the `apply_changes` batch tool."""

from __future__ import annotations

from pydantic_ai.tools import AgentDepsT
from pydantic_ai_harness.coder import Coder as _Coder

from .changes import ApplyChanges

__all__ = ["Coder"]


class Coder(_Coder[AgentDepsT]):
    """The harness `Coder` with one extra tool: `apply_changes`.

    Every harness tool, instruction, guardrail, and context-management capability stays as
    it is. The extra tool batches writes and edits for many files into one approved call,
    validates the whole set before writing anything, and reports each file individually.
    Bind it exactly like the harness `Coder`, next to `LocalWorkspace(...)` or a sandbox.
    """

    def __post_init__(self) -> None:
        # Runs inside `CombinedCapability.__init__`, before the composition is normalized, so
        # the extra capability is flattened in like any other child.
        self.capabilities = [*self.capabilities, ApplyChanges()]
        super().__post_init__()
