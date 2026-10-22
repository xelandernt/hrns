"""Editable configuration for the bundled Ask User capability."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AskUserConfig"]


class AskUserConfig(BaseModel):
    """Settings for :class:`harness.runtime.capabilities.ask_user.AskUser`."""

    model_config = ConfigDict(extra="forbid")

    timeout: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Seconds to wait for an answer before telling the model the user did not "
            "respond. Unset waits indefinitely."
        ),
    )
