"""Agent Skills: an on-demand catalog plus deterministic `$name` invocation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from pydantic_ai.capabilities import AbstractCapability, Capability, CombinedCapability
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai_skills import Skill, SkillPackage, SkillsCapability

from ...registry import BindingContext
from ...resources import ResourceSlot
from ._catalog import build_capability, build_skills, workspace_base
from ._invocation import explicit_skill_names, latest_user_text
from ._needs import NEEDS
from .config import SkillsConfig, resolved_directories

__all__ = ["AgentSkills", "build_agent_skills"]

_HEADER = (
    "# Explicitly invoked skills\n\n"
    "The user's message names these skills with `$name`. Treat them as loaded for this "
    "turn and follow their instructions."
)


class _ExplicitSkills(AbstractCapability[AgentDepsT]):
    """Put the body of every `$name` the user invoked into the turn's instructions.

    Resolution happens in the runtime, from the turn's prompt, so a hand-typed mention,
    a pasted one, a headless run, and an ACP client all behave like the picker.
    """

    def __init__(self, directories: Sequence[str], packages: Mapping[str, SkillPackage]) -> None:
        self._directories = tuple(directories)
        self._packages = packages

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> AbstractCapability[AgentDepsT]:
        text = latest_user_text(ctx.prompt, ctx.messages)
        if "$" not in text:
            return self
        catalog = await build_skills(self._directories).load()
        by_name = {skill.name: skill for skill in catalog.skills if skill.user_invocable}
        names = explicit_skill_names(text, by_name)
        if not names:
            return self
        skills: list[Skill] = []
        for name in names:
            definition = by_name[name]
            package = self._packages.get(name)
            # Programmatic skills use the same bundled-file rendering as disk-backed
            # skills, including user-only skills absent from the model's catalog.
            body = definition.body.replace("${SKILL_DIR}", definition.directory).replace(
                "${CLAUDE_SKILL_DIR}", definition.directory
            )
            skills.append(
                Skill(
                    name=name,
                    description=definition.description,
                    content=body,
                    resources=list(package.resources) if package is not None else [],
                    scripts=list(package.scripts) if package is not None else [],
                )
            )
        rendered: dict[str, str] = {}

        def collect(capability: AbstractCapability[AgentDepsT]) -> None:
            if capability.id in names:
                instructions = capability.get_instructions()
                assert isinstance(instructions, list)
                parts: list[str] = []
                for part in instructions:
                    assert isinstance(part, str)
                    parts.append(part)
                rendered[capability.id] = "\n\n".join(parts)

        SkillsCapability[AgentDepsT](skills=skills).apply(collect)
        body = "\n\n".join(rendered[name] for name in names)
        return Capability[AgentDepsT](
            id="hrns.skills.explicit",
            instructions=[f"{_HEADER}\n\n{body}"],
        )


class AgentSkills(CombinedCapability[AgentDepsT]):
    """Agent Skills from local libraries, with progressive disclosure and `$name`.

    Each skill becomes a deferred capability the model loads with `load_capability`, so the
    always-on prompt only carries names and descriptions. Bundled resources and scripts are
    reachable through `read_skill_resource` and `run_skill_script`.

    A user who writes `$skill-name` invokes a skill explicitly: that skill's instructions
    are part of the turn without waiting for the model to choose, exactly as the `/skills`
    picker suggests. Names the catalog does not know are left as ordinary text.
    """

    needs: ClassVar[tuple[ResourceSlot[object], ...]] = NEEDS

    def __init__(self, config: SkillsConfig | None = None, *, base: str | None = None) -> None:
        config = config if config is not None else SkillsConfig()
        directories = resolved_directories(config, base)
        files = build_capability(directories)
        capabilities: list[AbstractCapability[AgentDepsT]] = [
            _ExplicitSkills(directories, files.packages if files is not None else {})
        ]
        if files is not None:
            capabilities.append(files)
        super().__init__(
            capabilities,
            id="hrns.skills",
            description="Agent Skills, loaded on demand, and `$name` explicit invocation.",
        )


def build_agent_skills(config: SkillsConfig, binding: BindingContext) -> AgentSkills:
    """Plugin factory: turn a validated configuration into the native capability."""
    resources = binding.resources
    base = str(workspace_base(resources)) if resources is not None else None
    return AgentSkills(config, base=base)
