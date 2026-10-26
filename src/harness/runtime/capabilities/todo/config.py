"""Editable configuration for the bundled Todo capability."""

from __future__ import annotations

from typing import Literal

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
    inject: EditableField[bool] = Field(
        default=True,
        description=(
            "Show the current plan back to the model each turn as a cache-safe reminder. "
            "Disable it to keep the plan visible only to the user."
        ),
    )
    cache_ttl: EditableField[Literal["5m", "1h"]] = Field(
        default="5m",
        description="Lifetime of the prompt-cache breakpoint that precedes the plan reminder.",
    )
