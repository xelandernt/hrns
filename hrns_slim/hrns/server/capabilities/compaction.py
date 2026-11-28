"""Context compaction capability with automatic tiered compaction and ``/compact``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic_ai import CapabilityEvent, RunContext
from pydantic_ai.agent import EventStreamHandler
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    AgentStreamEvent,
    CompactionPart,
    ModelMessage,
    ModelRequest,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import (
    DEFAULT_CONTEXT_WINDOW,
    ClearToolResults,
    CompactionStrategy,
    DeduplicateFileReads,
    SummarizingCompaction,
    TieredCompaction,
    compact_now,
    estimate_context_tokens,
    estimate_token_count,
)
from pydantic_ai_harness.compaction._shared import (
    compact_with_span,
    context_for_request,
    record_compaction_reclaim,
)
from pydantic_ai_harness.compaction._summary import is_summary_part

from hrns.server.acp.updates import update_compaction, update_compaction_summary_chunk

from .commands import CommandContext, CommandHandled, SlashCommand

__all__ = [
    "Compaction",
    "CompactionCompleted",
    "CompactionFailed",
    "CompactionStarted",
    "CompactionSummaryChunkEvent",
    "default_file_read_key",
]

CompactionMode = Literal["auto", "manual"]

#: The ``_meta`` key on ACP ``compaction_update`` carrying structured hrns metrics.
COMPACTION_META_KEY = "hrns/compaction"


def default_file_read_key(call: ToolCallPart) -> str | None:
    """Identify superseded file reads for ``DeduplicateFileReads``.

    Matches the harness ``read_file`` tool using ``path`` plus optional ``offset``/``limit``
    so two partial reads of different slices of the same file do not clobber each other,
    while re-reading the same slice or whole file deduplicates cleanly.
    """
    if call.tool_name != "read_file":
        return None
    args = call.args_as_dict()
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return None
    offset = args.get("offset")
    limit = args.get("limit")
    if offset is None and limit is None:
        return path
    return f"{path}:{offset}:{limit}"


@dataclass(kw_only=True)
class CompactionStarted(CapabilityEvent, namespace="hrns.compaction", name="started"):
    """Emitted when automatic compaction begins before a model request."""

    compaction_id: str
    mode: CompactionMode
    tokens_before: int
    messages_before: int
    focus: str | None = None


@dataclass(kw_only=True)
class CompactionSummaryChunkEvent(
    CapabilityEvent, namespace="hrns.compaction", name="summary_chunk"
):
    """Emitted as the summarization model streams chunks of a compaction summary."""

    compaction_id: str
    delta: str


@dataclass(kw_only=True)
class CompactionCompleted(CapabilityEvent, namespace="hrns.compaction", name="completed"):
    """Emitted when automatic compaction finishes and changed the history."""

    compaction_id: str
    mode: CompactionMode
    tokens_before: int
    tokens_after: int
    messages_before: int
    messages_after: int
    summary: str | None = None
    focus: str | None = None


@dataclass(kw_only=True)
class CompactionFailed(CapabilityEvent, namespace="hrns.compaction", name="failed"):
    """Emitted when automatic compaction fails."""

    compaction_id: str
    mode: CompactionMode
    error: str


def _latest_summary_text(messages: Sequence[ModelMessage]) -> str | None:
    """Return the newest compaction summary content present in ``messages``, if any."""
    for message in reversed(messages):
        for part in reversed(message.parts):
            if is_summary_part(part) and part.content:
                return part.content
            if isinstance(part, CompactionPart) and part.content:
                return part.content
    return None


def _compaction_meta(
    *,
    mode: CompactionMode,
    tokens_before: int | None = None,
    tokens_after: int | None = None,
    messages_before: int | None = None,
    messages_after: int | None = None,
    focus: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"mode": mode}
    if tokens_before is not None:
        payload["tokens_before"] = tokens_before
    if tokens_after is not None:
        payload["tokens_after"] = tokens_after
    if messages_before is not None:
        payload["messages_before"] = messages_before
    if messages_after is not None:
        payload["messages_after"] = messages_after
    if focus:
        payload["focus"] = focus
    return {COMPACTION_META_KEY: payload}


def _summary_text_delta(event: AgentStreamEvent) -> str | None:
    """Extract a streamed text chunk from a summarizer run event, if present."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content or None
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta or None
    return None


@dataclass
class Compaction(AbstractCapability[AgentDepsT]):
    """Automatic tiered compaction plus the ``/compact [focus]`` ACP slash command.

    Wraps ``pydantic_ai_harness.compaction``:
    - During a run, escalates through ``DeduplicateFileReads`` -> ``ClearToolResults`` ->
      ``SummarizingCompaction`` via ``TieredCompaction`` once context exceeds
      ``target_fraction`` (or ``target_tokens``).
    - Advertises ``/compact`` over ACP via ``get_slash_commands()``; invoking ``/compact``
      unconditionally runs ``SummarizingCompaction`` via ``compact_now`` with optional focus
      instructions, streams progress/summary over ACP ``compaction_update`` and
      ``compaction_summary_chunk``, and refreshes context usage.

    Not enabled automatically on arbitrary agents — attach ``Compaction()`` to opt in.
    """

    target_fraction: float | None = None
    target_tokens: int | None = None
    context_window: int | None = None
    fallback_context_window: int = DEFAULT_CONTEXT_WINDOW
    keep_pairs: int = 3
    keep_messages: int = 10
    keep_tokens: int | None = None
    summarizer_model: Model | str | None = None
    summarizer_model_settings: ModelSettings | None = None
    file_key: Callable[[ToolCallPart], str | None] = default_file_read_key
    tokenizer: Callable[[str], int] | None = None
    command_name: str = "compact"
    id: str | None = "hrns.compaction"
    _tiers: tuple[CompactionStrategy[AgentDepsT], ...] | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.target_tokens is not None and self.target_fraction is not None:
            raise ValueError("Set at most one of target_tokens or target_fraction.")
        if self.target_tokens is None and self.target_fraction is None:
            self.target_fraction = 0.8

    def _build_summarizer(
        self,
        *,
        keep_messages: int,
        keep_tokens: int | None,
        model: Model | str | None = None,
        model_settings: ModelSettings | None = None,
        event_stream_handler: EventStreamHandler[object] | None = None,
    ) -> SummarizingCompaction[AgentDepsT]:
        return SummarizingCompaction[AgentDepsT](
            model=model if model is not None else self.summarizer_model,
            model_settings=(
                model_settings if model_settings is not None else self.summarizer_model_settings
            ),
            keep_messages=keep_messages,
            keep_tokens=keep_tokens,
            tokenizer=self.tokenizer,
            preserve_first_user_message=True,
            incremental=True,
            event_stream_handler=event_stream_handler,
        )

    def _build_tiered(
        self,
        *,
        event_stream_handler: EventStreamHandler[object] | None = None,
    ) -> TieredCompaction[AgentDepsT]:
        if self._tiers is not None:
            tiers: Sequence[CompactionStrategy[AgentDepsT]] = self._tiers
        else:
            tiers = (
                DeduplicateFileReads[AgentDepsT](
                    file_key=self.file_key,
                    tokenizer=self.tokenizer,
                ),
                ClearToolResults[AgentDepsT](
                    keep_pairs=self.keep_pairs,
                    tokenizer=self.tokenizer,
                ),
                self._build_summarizer(
                    keep_messages=self.keep_messages,
                    keep_tokens=self.keep_tokens,
                    event_stream_handler=event_stream_handler,
                ),
            )
        return TieredCompaction[AgentDepsT](
            tiers=tiers,
            target_tokens=self.target_tokens,
            target_fraction=self.target_fraction,
            context_window=self.context_window,
            fallback_context_window=self.fallback_context_window,
            tokenizer=self.tokenizer,
        )

    def get_slash_commands(self) -> Sequence[SlashCommand[AgentDepsT]]:
        return (
            SlashCommand[AgentDepsT](
                name=self.command_name,
                description="Summarize and compact conversation context",
                input_hint="optional focus instructions",
                handler=self._handle_compact_command,
            ),
        )

    async def before_model_request(
        self,
        ctx: RunContext[AgentDepsT],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        messages: list[ModelMessage] = list(ctx.messages)
        request_ctx = context_for_request(ctx, request_context)

        compaction_id = f"cmp_{uuid4().hex[:12]}"

        async def _stream_summary(
            _summary_ctx: RunContext[object],
            stream: AsyncIterable[AgentStreamEvent],
        ) -> None:
            async for event in stream:
                delta = _summary_text_delta(event)
                if delta:
                    await ctx.emit(
                        CompactionSummaryChunkEvent(compaction_id=compaction_id, delta=delta)
                    )

        tiered = self._build_tiered(event_stream_handler=_stream_summary)
        target = tiered._target(request_ctx.model)
        if target is None:
            return request_context

        current_estimate = estimate_context_tokens(
            messages,
            self.tokenizer,
            model_request_parameters=request_context.model_request_parameters,
        )
        if current_estimate <= target:
            return request_context

        tokens_before = estimate_token_count(messages, self.tokenizer)
        await ctx.emit(
            CompactionStarted(
                compaction_id=compaction_id,
                mode="auto",
                tokens_before=tokens_before,
                messages_before=len(messages),
            )
        )
        try:
            compacted = await compact_with_span(
                request_ctx,
                strategy="TieredCompaction",
                messages=messages,
                compact=lambda: tiered._escalate(
                    messages,
                    request_ctx,
                    target,
                    request_context.model_request_parameters,
                    tiered.skip_on,
                ),
                tokenizer=self.tokenizer,
            )
        except Exception as exc:
            await ctx.emit(
                CompactionFailed(
                    compaction_id=compaction_id,
                    mode="auto",
                    error=str(exc) or type(exc).__name__,
                )
            )
            raise

        tokens_after = estimate_token_count(compacted, self.tokenizer)
        ctx.messages[:] = compacted
        record_compaction_reclaim(ctx, tokens_before, tokens_after)

        summary = _latest_summary_text(compacted) if compacted != messages else None
        await ctx.emit(
            CompactionCompleted(
                compaction_id=compaction_id,
                mode="auto",
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                messages_before=len(messages),
                messages_after=len(compacted),
                summary=summary,
            )
        )

        return replace(request_context, messages=compacted)

    async def _handle_compact_command(self, ctx: CommandContext[AgentDepsT]) -> CommandHandled:
        focus = ctx.arguments.strip() or None
        messages = list(ctx.history)
        if not messages:
            return CommandHandled(message="Nothing to compact yet — conversation history is empty.")

        model = self.summarizer_model if self.summarizer_model is not None else ctx.model
        if model is None:
            return CommandHandled(
                message="Cannot compact: no model is configured for this session."
            )

        compaction_id = f"cmp_{uuid4().hex[:12]}"
        tokens_before = estimate_token_count(messages, self.tokenizer)
        messages_before = len(messages)
        start_meta = _compaction_meta(
            mode="manual",
            tokens_before=tokens_before,
            messages_before=messages_before,
            focus=focus,
        )

        await ctx.emit_update(update_compaction(compaction_id, "in_progress", meta=start_meta))

        async def _stream_summary(
            _summary_ctx: RunContext[object],
            stream: AsyncIterable[AgentStreamEvent],
        ) -> None:
            async for event in stream:
                delta = _summary_text_delta(event)
                if delta:
                    await ctx.emit_update(update_compaction_summary_chunk(compaction_id, delta))

        summarizer = self._build_summarizer(
            keep_messages=1,
            keep_tokens=None,
            model=model,
            model_settings=(
                self.summarizer_model_settings
                if self.summarizer_model_settings is not None
                else ctx.model_settings
            ),
            event_stream_handler=_stream_summary,
        )
        if focus is not None:
            summarizer = summarizer.with_focus(focus)

        # Append an empty trailing request sentinel so ``SummarizingCompaction`` (which always
        # keeps at least the newest message assuming an active turn) summarizes the entire
        # committed history cleanly, then strip the sentinel from the compacted result.
        sentinel = ModelRequest(parts=[UserPromptPart(content="")])
        usage = RunUsage()
        try:
            compacted = await compact_now(
                summarizer,
                [*messages, sentinel],
                model=model,
                deps=ctx.deps,
                usage=usage,
            )
        except asyncio.CancelledError:
            await ctx.emit_update(update_compaction(compaction_id, "cancelled", meta=start_meta))
            raise
        except Exception as exc:
            error_text = str(exc) or type(exc).__name__
            await ctx.emit_update(
                update_compaction(compaction_id, "failed", error=error_text, meta=start_meta)
            )
            return CommandHandled(usage=usage, cost=usage.cost)

        if compacted and compacted[-1] is sentinel:
            compacted = compacted[:-1]

        tokens_after = estimate_token_count(compacted, self.tokenizer)
        summary = _latest_summary_text(compacted)
        ctx.replace_history(compacted)

        cost: Decimal | None = usage.cost
        await ctx.emit_context_usage(tokens_after, cost)

        completed_meta = _compaction_meta(
            mode="manual",
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            messages_before=messages_before,
            messages_after=len(compacted),
            focus=focus,
        )
        await ctx.emit_update(
            update_compaction(compaction_id, "completed", summary=summary, meta=completed_meta)
        )

        return CommandHandled(usage=usage, cost=cost)
