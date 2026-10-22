"""Editable configuration for the Agent Skills capability."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...registry import EditableField

__all__ = ["DEFAULT_SKILL_DIRECTORIES", "SkillsConfig", "resolved_directories"]

#: Codex-style defaults: a library in the workspace and the operator's personal one.
DEFAULT_SKILL_DIRECTORIES = (".agents/skills", "~/.agents/skills")


class SkillsConfig(BaseModel):
    """Settings for :class:`harness.runtime.capabilities.skills.AgentSkills`."""

    model_config = ConfigDict(extra="forbid")

    directories: EditableField[list[str]] = Field(
        default_factory=lambda: list(DEFAULT_SKILL_DIRECTORIES),
        min_length=1,
        description=(
            "Agent Skill library directories: the parent of the skill packages, not a skill "
            "package itself. A relative path resolves against the session workspace; `~` "
            "expands on the runtime host."
        ),
    )

    @field_validator("directories")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        cleaned = [entry.strip() for entry in value]
        if any(not entry for entry in cleaned):
            raise ValueError("directories must not contain blank entries")
        return cleaned


def resolved_directories(config: SkillsConfig, base: str | Path | None = None) -> list[str]:
    """Absolute library paths: `~` on the runtime host, relative against `base`.

    `base` is the session workspace when it is a path on this host, else the runtime's
    working directory. A sandboxed workspace is not a host path: its libraries are not
    readable here, and `~` still names the runtime operator's own library.
    """
    root = Path(base).expanduser() if base is not None else Path.cwd()
    resolved: list[str] = []
    for entry in config.directories:
        path = Path(entry).expanduser()
        if not path.is_absolute():
            path = root / path
        absolute = str(path.resolve())
        if absolute not in resolved:
            resolved.append(absolute)
    return resolved
