"""Editable configuration for the bundled coding capability."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ...registry import EditableField, NonEditableField

__all__ = ["CoderConfig"]


class CoderConfig(BaseModel):
    """Settings for :class:`hrns.runtime.capabilities.coder.Coder`."""

    model_config = ConfigDict(extra="forbid")

    instructions: str | None = Field(
        default=None,
        description="Extra guidance appended to the built-in coding instructions.",
    )
    repo_context: EditableField[bool] = Field(
        default=True,
        description="Load AGENTS.md / CLAUDE.md from the workspace into the system prompt.",
    )
    caching: EditableField[bool] = Field(
        default=True,
        description="Ask the provider to cache the prompt prefix when it supports caching.",
    )
    shell_timeout: EditableField[float] = Field(
        default=120.0,
        ge=1.0,
        le=3600.0,
        description="Default timeout in seconds for a foreground shell command.",
    )
    unrestricted_filesystem: NonEditableField[bool] = Field(
        default=False,
        description=(
            "Let the file tools reach the whole workspace filesystem instead of the "
            "working directory. Locked: a host must opt in explicitly."
        ),
    )
