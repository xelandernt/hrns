"""Build the one agent every protocol adapter wraps."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from hrns.server.capabilities import AskUser, Coder, Skills, Todo
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

    Approval is a capability, not an adapter concern. By default every tool
    outside :data:`hrns.server.approvals.READ_ONLY_TOOLS` is marked ``unapproved``,
    so ACP can turn it into a permission request. Pass ``approval=False`` to
    disable that policy.
    """
    selected = model or os.environ.get("HRNS_MODEL")
    if selected is None:
        raise ValueError("Pass model= or set HRNS_MODEL to a Pydantic AI model id.")

    resolved_skills = Skills.from_settings(skills or SkillsSettings(), cwd=cwd)
    return Agent[None, str](
        selected,
        deps_type=type(None),
        name="hrns",
        model_settings=model_settings,
        capabilities=[
            LocalWorkspace(cwd),
            coder or Coder(),
            Todo(),
            AskUser(),
            *([resolved_skills] if resolved_skills is not None else []),
            *([require_approval()] if approval else []),
        ],
    )
