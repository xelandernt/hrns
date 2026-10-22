"""Versioned wire contracts for application commands and tool presentation.

These models are data only. They never carry executable callbacks, widget code, import
references, or host/session internals; the client resolves presentation identifiers against
locally installed adapters.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from .models import BindingId, PresentationPayload, WireModel

CommandName = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")]
"""Local command name; a namespace separator is supplied by the binding id."""

#: Conversation operations this protocol version can actually perform.
SUPPORTED_CONVERSATION_OPERATIONS = ("read_history", "start_turn")


class CommandDescriptor(WireModel):
    """One command an enabled binding contributes to the current session."""

    schema_version: Literal[1] = 1
    binding_id: BindingId
    command_name: CommandName
    description: str = ""
    takes_arguments: bool = False
    arguments_hint: str = ""
    required_operations: list[str] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)


class CommandListRequest(WireModel):
    session_id: str = Field(min_length=1)
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=200)


class CommandListResponse(WireModel):
    schema_version: Literal[1] = 1
    commands: list[CommandDescriptor] = Field(default_factory=list)
    selected_revision: int = Field(default=0, ge=0)
    active_revision: int | None = Field(default=None, ge=0)
    supported_operations: list[str] = Field(default_factory=list)
    next_cursor: str | None = None


class StartTurn(WireModel):
    """An explicit request to run one normal agent turn with this exact prompt."""

    type: Literal["start_turn"] = "start_turn"
    prompt: str = Field(min_length=1, max_length=200_000)
    display_text: str | None = Field(default=None, max_length=200_000)


ConversationAction = Annotated[StartTurn, Field(discriminator="type")]


class CommandResult(WireModel):
    """User-only output plus at most one explicit conversation action."""

    presentation: PresentationPayload | None = None
    conversation: ConversationAction | None = None


class CommandInvokeRequest(WireModel):
    session_id: str = Field(min_length=1)
    binding_id: BindingId
    command_name: CommandName
    request_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)
    text: str | None = Field(default=None, max_length=200_000)
    arguments: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _one_argument_form(self) -> CommandInvokeRequest:
        if self.text is not None and self.arguments is not None:
            raise ValueError("provide either raw text or structured arguments, not both")
        return self


class CommandInvokeResponse(WireModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=200)
    binding_id: BindingId
    command_name: CommandName
    result: CommandResult


class CommandCancelRequest(WireModel):
    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1, max_length=200)


class CommandCancelResponse(WireModel):
    schema_version: Literal[1] = 1
    cancelled: bool = False
    finished: bool = False


class CommandCatalogChanged(WireModel):
    """Notification that the current session's command catalog changed."""

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    selected_revision: int = Field(default=0, ge=0)
    commands: list[CommandDescriptor] = Field(default_factory=list)
    supported_operations: list[str] = Field(default_factory=list)


class ToolPresentationSnapshot(WireModel):
    """Supplemental presentation state for one ACP tool call."""

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(default=1, ge=1)
    binding_id: str | None = Field(default=None, max_length=200)
    run_id: str | None = Field(default=None, max_length=200)
    input: PresentationPayload | None = None
    output: PresentationPayload | None = None
    progress: PresentationPayload | None = None
    fallback: str = ""


class PresentationListRequest(WireModel):
    session_id: str = Field(min_length=1)
    tool_call_ids: list[str] = Field(min_length=1, max_length=200)


class PresentationListResponse(WireModel):
    schema_version: Literal[1] = 1
    snapshots: list[ToolPresentationSnapshot] = Field(default_factory=list)


class PresentationUpdated(WireModel):
    snapshots: list[ToolPresentationSnapshot] = Field(min_length=1, max_length=100)


__all__ = [
    "CommandCancelRequest",
    "CommandCancelResponse",
    "CommandCatalogChanged",
    "CommandDescriptor",
    "CommandInvokeRequest",
    "CommandInvokeResponse",
    "CommandListRequest",
    "CommandListResponse",
    "CommandName",
    "CommandResult",
    "ConversationAction",
    "PresentationListRequest",
    "PresentationListResponse",
    "PresentationUpdated",
    "SUPPORTED_CONVERSATION_OPERATIONS",
    "StartTurn",
    "ToolPresentationSnapshot",
]
