"""Shared helpers, protocols, and turn state types for ACP session execution."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias, TypeVar

from acp import schema
from acp.interfaces import Client
from pydantic_ai import UsageLimitExceeded
from pydantic_ai.messages import (
    AgentStreamEvent,
    FinishReason,
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    ToolReturnPart,
    UserContent,
)
from pydantic_ai.models import Model
from pydantic_ai.run import AgentRunResult, AgentRunResultEvent
from pydantic_ai.usage import RunUsage

from .content import PromptContentBlock
from .presentation import ToolCallContent
from .session import SessionUpdate

_MODEL_CONFIG_ID = "model"
_ASK_USER_TOOL = "ask_user_question"

#: The ``session/new`` and ``session/load`` response ``_meta`` key carrying the skill catalog.
SKILLS_META_KEY = "hrns/skills"

#: Tool-call id prefix for the transcript marker of a skill the person invoked.
_SKILL_TOOL_PREFIX = "hrns-skill:"

#: One complete ACP config-option list, as ``session/new`` and ``session/set_config_option`` return it.
ConfigOptions: TypeAlias = list[
    schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean
]


def _skill_references(prompt: Sequence[PromptContentBlock]) -> tuple[str, ...]:
    """Skill ids a client attached to the prompt blocks, in first-seen order.

    A person's ``$skill`` reference travels in the text block's ``_meta`` under
    :data:`SKILLS_META_KEY`: a list of ``{"id": ..., "mention": {...}}`` objects. The
    mention is display information for the client; the server acts on the id and does
    not read the prompt text.
    """
    ids: list[str] = []
    for block in prompt:
        meta = block.field_meta
        if not meta:
            continue
        payload = meta.get(SKILLS_META_KEY)
        if not isinstance(payload, list):
            continue
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            skill_id = entry.get("id")
            if isinstance(skill_id, str) and skill_id and skill_id not in ids:
                ids.append(skill_id)
    return tuple(ids)


def _finish_reason_to_stop_reason(finish_reason: FinishReason | None) -> schema.StopReason:
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "content_filter":
        return "refusal"
    return "end_turn"


_USAGE_LIMIT_STOP_REASONS: dict[str, schema.StopReason] = {
    "request_limit": "max_turn_requests",
    "tool_calls_limit": "max_turn_requests",
    "input_tokens_limit": "max_tokens",
    "output_tokens_limit": "max_tokens",
    "total_tokens_limit": "max_tokens",
    "per_request_input_tokens_limit": "max_tokens",
    "cost_limit": "max_turn_requests",
}

_USAGE_LIMIT_NAME = re.compile(rf"\b({'|'.join(_USAGE_LIMIT_STOP_REASONS)})\b")


def _usage_limit_stop_reason(exc: UsageLimitExceeded) -> schema.StopReason:
    match = _USAGE_LIMIT_NAME.search(str(exc))
    return _USAGE_LIMIT_STOP_REASONS[match.group(1)] if match else "max_turn_requests"


def _committable_history(messages: list[ModelMessage], ran_calls: set[str]) -> list[ModelMessage]:
    committed = list(messages)
    while committed:
        last = committed[-1]
        if isinstance(last, ModelResponse):
            if last.tool_calls and not any(
                call.tool_call_id in ran_calls for call in last.tool_calls
            ):
                committed.pop()
                continue
            break
        if any(
            isinstance(part, ToolReturnPart | RetryPromptPart) and part.tool_name
            for part in last.parts
        ):
            break
        previous = committed[-2] if len(committed) > 1 else None
        if (
            last.state == "interrupted"
            and isinstance(previous, ModelResponse)
            and any(call.tool_call_id in ran_calls for call in previous.tool_calls)
        ):
            break
        committed.pop()
    return committed


def _to_acp_usage(usage: RunUsage) -> schema.Usage:
    return schema.Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cached_read_tokens=usage.cache_read_tokens,
        cached_write_tokens=usage.cache_write_tokens,
    )


def _context_window(model: Model | str | None) -> int | None:
    """The model's context window, when its profile knows one."""
    if isinstance(model, Model):
        window = model.profile.get("context_window")
        return window if window else None
    return None


def _last_response(messages: Sequence[ModelMessage]) -> ModelResponse | None:
    """The newest model response among a run's messages."""
    return next(
        (message for message in reversed(messages) if isinstance(message, ModelResponse)), None
    )


def _context_tokens(response: ModelResponse) -> int:
    """The latest request's input and output, including cached input once."""
    return response.usage.total_tokens


ObservedOutputT = TypeVar("ObservedOutputT")


class RunObserver(Protocol):
    async def consume(
        self,
        stream: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[ObservedOutputT]],
        history: list[ModelMessage],
        user_content: list[UserContent] | None,
        emit: Callable[[AgentStreamEvent], Awaitable[None]],
        run_id: str,
    ) -> AgentRunResult[ObservedOutputT]: ...

    def state_changed(self, state: dict[str, object]) -> None: ...

    def close(self) -> None: ...

    def interrupted(self, messages: list[ModelMessage]) -> None: ...


class _LiveUsage(Protocol):
    """A run handle's usage and messages while it is still streaming."""

    @property
    def usage(self) -> RunUsage: ...

    def all_messages(self) -> list[ModelMessage]: ...


class _TurnCancelled(Exception):
    """Raised internally to unwind a turn the client cancelled mid-flight."""


@dataclass(kw_only=True)
class _TurnState:
    """Per-turn state shared by the stream handlers."""

    session_id: str
    cwd: str
    approval_names: frozenset[str]
    known_non_approval_names: frozenset[str] = frozenset()
    started: set[str] = field(default_factory=set[str])
    denied: set[str] = field(default_factory=set[str])
    resulted: set[str] = field(default_factory=set[str])
    announced_status: dict[str, schema.ToolCallStatus] = field(
        default_factory=dict[str, schema.ToolCallStatus]
    )
    updates: list[SessionUpdate] = field(default_factory=list[SessionUpdate])


@dataclass(frozen=True, kw_only=True)
class _ToolCallFields:
    title: str
    kind: schema.ToolKind | None
    content: list[ToolCallContent] | None
    locations: list[schema.ToolCallLocation] | None


@dataclass(frozen=True)
class SteeringPrompt:
    prompt: tuple[PromptContentBlock, ...]
    origin: Client | None
