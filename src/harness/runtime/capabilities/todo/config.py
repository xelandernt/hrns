"""Editable configuration for the bundled Todo capability."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ...registry import EditableField

__all__ = ["TodoConfig"]


class TodoConfig(BaseModel):
    """Settings for :class:`harness.runtime.capabilities.todo.Todo`."""

    model_config = ConfigDict(extra="forbid")

    enable_subtasks: EditableField[bool] = Field(
        default=False,
        description=(
            "Allow parent steps, dependencies, and the `blocked` status in the full plan. "
            "Subtasks appear flat in the terminal panel."
        ),
    )
