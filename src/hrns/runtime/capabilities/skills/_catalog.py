"""The skill catalog shared by the `/skills` command and `$name` resolution."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic_ai_harness import Skills
from pydantic_ai_harness.skills import SkillCatalog
from pydantic_ai_skills import SkillsCapability

from hrns.protocol import HrnsError, PresentationPayload
from hrns.protocol.extensions import CommandResult

from ...extensions import CommandContext, TextArguments
from ...resources import WORKSPACE, ResourceSet
from ...workspaces import local_workspace
from .config import SkillsConfig, resolved_directories

__all__ = [
    "CATALOG_RENDERER",
    "build_capability",
    "build_skills",
    "catalog_command",
    "catalog_data",
    "workspace_base",
]

CATALOG_RENDERER = "hrns.skills.catalog"


def workspace_base(resources: ResourceSet | None) -> Path:
    """The directory relative libraries resolve against.

    A local workspace path is what the agent's tools act on, so it is the right base for
    `.agents/skills`. A remote or sandboxed workspace is not readable on this host; fall
    back to the runtime's working directory, matching how `SkillsCapability` reads.
    """
    value = resources.get(WORKSPACE) if resources is not None else None
    if isinstance(value, str | Path):
        path = Path(value).expanduser()
        if path.is_dir():
            return path
    return Path.cwd()


def _existing_libraries(directories: Sequence[str]) -> list[str]:
    """Libraries that exist on the runtime host and hold packages, not a `SKILL.md`.

    `SkillsCapability` refuses a path that does not exist, so the conventional defaults
    are filtered here instead of failing every turn in a project without `.agents/skills`.
    """
    libraries: list[str] = []
    for entry in directories:
        path = Path(entry)
        if not path.is_dir() or (path / "SKILL.md").is_file():
            continue
        libraries.append(entry)
    return libraries


def build_skills(directories: Sequence[str]) -> Skills:
    """The catalog reader for these libraries, outside a run.

    Reads from the runtime host, as `SkillsCapability` does: a library path names a
    directory on the machine running the agent, not a path inside a sandboxed workspace.
    """
    return Skills(
        list(directories),
        missing_directories="skip",
        duplicate_names="keep_first",
        workspace=local_workspace(Path.cwd()),
    )


class _WindowsSkillsCapability(SkillsCapability):
    """`SkillsCapability` that reads libraries through the Windows workspace.

    `SkillsCapability` builds ``LocalWorkspaceBackend(Path.cwd())`` internally to hand harness
    the libraries on this machine, and Pydantic AI's backend refuses non-POSIX hosts. The
    libraries are host paths in both cases, so only the workspace wrapper changes.
    """

    def _build_harness_skills(
        self,
        libraries: Sequence[str | Path],
        directory_names: frozenset[str],
        shadowed: frozenset[str],
    ) -> Skills[Any] | None:
        if not libraries:
            return None
        workspace = local_workspace(Path.cwd())
        if self.include is not None:
            return Skills(
                libraries,
                include=sorted((self.include & directory_names) - shadowed),
                workspace=workspace,
            )
        return Skills(
            libraries,
            exclude=sorted((self.exclude | shadowed) & directory_names),
            workspace=workspace,
        )


def build_capability(directories: Sequence[str]) -> SkillsCapability | None:
    """The on-demand catalog plus bundled-file tools, or None with no existing library."""
    libraries = _existing_libraries(directories)
    if not libraries:
        return None
    capability_type = _WindowsSkillsCapability if os.name == "nt" else SkillsCapability
    return capability_type(
        libraries,
        id="hrns.skills.files",
        # `$name` injects the skill body, so a loaded skill and an explicitly invoked one
        # are equivalent for the turn; refuse-file reads would add a redundant
        # `load_capability` round-trip to every reference.
        require_loaded=False,
    )


def catalog_data(catalog: SkillCatalog, directories: Sequence[str]) -> dict[str, Any]:
    """Serialize one catalog snapshot for a client picker."""
    return {
        "directories": list(directories),
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "directory": skill.directory,
                "model_invocable": skill.model_invocable,
                "user_invocable": skill.user_invocable,
            }
            for skill in catalog.skills
        ],
        "skipped": [
            {
                "path": skipped.path,
                "reason": skipped.reason,
                "message": skipped.message,
            }
            for skipped in catalog.skipped
        ],
    }


def _fallback(data: dict[str, Any]) -> str:
    skills = data["skills"]
    if not skills:
        return "No skills discovered in the configured libraries."
    return "\n".join(f"${skill['name']} — {skill['description']}" for skill in skills)


async def catalog_command(context: CommandContext, _arguments: TextArguments) -> CommandResult:
    """Return the discovered skills so a client can offer them before the first turn."""
    config = context.config
    if not isinstance(config, SkillsConfig):
        raise HrnsError("COMMAND_FAILED", "Agent Skills received an invalid configuration.")
    if context.resources is not None and context.resources.get(WORKSPACE) is None:
        raise HrnsError(
            "SKILLS_UNAVAILABLE",
            "Agent Skills needs a workspace resource to discover its libraries.",
            details={"binding_id": context.binding_id},
        )
    directories = resolved_directories(config, workspace_base(context.resources))
    catalog = await build_skills(directories).load()
    data = catalog_data(catalog, directories)
    return CommandResult(
        presentation=PresentationPayload(
            renderer_id=CATALOG_RENDERER,
            data=data,
            fallback=_fallback(data),
        )
    )
