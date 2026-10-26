"""Bundled Agent Skills capability: local libraries, `/skills`, and `$name` invocation.

The descriptor is metadata only: configuration is a plain Pydantic model, the factory is an
import reference, and the catalog command imports the heavy skill machinery on first use.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AgentCapability

from hrns.protocol import CapabilityPolicy
from hrns.protocol.extensions import CommandResult

from ...extensions import Command, CommandContext, TextArguments
from ...registry import BindingContext, CompatibilityConstraints, PluginDescriptor
from ._needs import NEEDS
from .config import SkillsConfig

__all__ = ["AgentSkills", "SkillsConfig", "build_agent_skills", "descriptor"]

_PLUGIN_ID = "hrns.skills"


def build_agent_skills(config: SkillsConfig, binding: BindingContext) -> AgentCapability:
    """Lazy factory reference; imports the capability module only when composed."""
    from ._capability import build_agent_skills as _build

    return _build(config, binding)


async def _catalog_handler(context: CommandContext, arguments: TextArguments) -> CommandResult:
    """Lazy command handler; the catalog imports `pydantic-ai-skills` on first invocation."""
    from ._catalog import catalog_command

    return await catalog_command(context, arguments)


def descriptor() -> PluginDescriptor[SkillsConfig]:
    """Metadata for the bundled Agent Skills capability."""
    return PluginDescriptor(
        plugin_id=_PLUGIN_ID,
        version="0.1.0",
        description=(
            "Agent Skills from local `.agents/skills` libraries: names and descriptions up "
            "front, instructions and bundled files on demand, and `$name` explicit invocation."
        ),
        config_model=SkillsConfig,
        factory="hrns.runtime.capabilities.skills:build_agent_skills",
        commands=[
            Command(
                name="catalog",
                handler=_catalog_handler,
                description="List the skills discovered in the configured libraries.",
            )
        ],
        state_mode="run",
        compatibility=CompatibilityConstraints(requires_extras=["pydantic_ai_skills"]),
        needs=NEEDS,
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=False,
            client_can_toggle=True,
            change_mode="next_turn",
        ),
    )


def __getattr__(name: str) -> Any:
    """Expose ``AgentSkills`` without importing the skill machinery at discovery time."""
    if name == "AgentSkills":
        from ._capability import AgentSkills

        return AgentSkills
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
