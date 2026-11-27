"""Build the one agent every protocol adapter wraps."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace, RetryOnModelErrors
from pydantic_ai.exceptions import ModelConnectionError
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits, WorkspaceOverflowStore

from hrns.server.capabilities import AskUser, Coder, Compaction, Skills, Todo
from hrns.shared.config.settings import SkillsSettings

from .approvals import require_approval

__all__ = ["build_agent"]


def build_agent(
    model: Model | str | None = None,
    *,
    cwd: str | Path = ".",
    coder: Coder | None = None,
    skills: SkillsSettings | None = None,
    model_settings: ModelSettings | None = None,
    approval: bool = True,
    _allow_unconfigured: bool = False,
) -> Agent[None, str]:
    """Build the default coding agent.

    The model is explicit unless ``HRNS_MODEL`` is set. Everything else is a
    Pydantic AI capability: the workspace roots the tools and ``Coder`` supplies
    the coding behavior. Add more capabilities here; protocol adapters never need
    to know about them.

    Skills are on by default: ``<cwd>/.agents/skills`` and ``~/.agents/skills`` are
    picked up when they exist, and no capability is added when neither does. Pass
    :class:`~hrns.shared.config.SkillsSettings` to add libraries, narrow the selection,
    or turn the capability off.

    Large tool results are saved to the workspace for bounded retrieval. Context is
    managed by :class:`~hrns.server.capabilities.Compaction` (automatic tiered compaction
    plus the ``/compact`` ACP slash command).

    Approval is a capability. ACP turns tools marked ``unapproved`` into
    permission requests. Pass ``approval=False`` to disable that policy.
    """
    selected = model if _allow_unconfigured else model or os.environ.get("HRNS_MODEL")
    if selected is None and not _allow_unconfigured:
        raise ValueError("Pass model= or set HRNS_MODEL to a Pydantic AI model id.")

    resolved_skills = Skills.from_settings(skills or SkillsSettings(), cwd=cwd)
    workspace = LocalWorkspace[None](cwd)
    return Agent[None, str](
        selected,
        deps_type=type(None),
        name="hrns",
        model_settings=model_settings,
        capabilities=[
            RetryOnModelErrors[None](retry_on=(ModelConnectionError,)),
            workspace,
            coder or Coder(sub_agents=False),
            # Post-execution hooks run in reverse order, so spills see the
            # result before Coder's truncation. An explicit workspace keeps
            # the dedicated result readers available alongside read_file.
            ToolOutputLimits(
                store=WorkspaceOverflowStore(workspace=LocalWorkspaceBackend(workspace.working_dir))
            ),
            Compaction(),
            Todo(),
            AskUser(),
            *([resolved_skills] if resolved_skills is not None else []),
            *([require_approval()] if approval else []),
        ],
    )
