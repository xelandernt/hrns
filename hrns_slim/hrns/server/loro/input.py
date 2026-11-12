"""Collaborative input acceptance, capture, and delivery receipts.

Drafts never execute. At a model-request boundary the server freezes every
integrated ready contribution, assigns consumption order, persists that decision,
and only then lets the native run see it. Later text edits cannot change a
captured contribution; a correction is a new contribution.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import TypeAdapter
from pydantic_ai import ModelRequestContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelRequest, UserContent, UserPromptPart
from pydantic_ai.tools import AgentDepsT, RunContext

from hrns.server.acp.content import PromptContentBlock, prompt_blocks_to_user_content

from .document import SessionDocument
from .models import Author, Contribution, ContributionState

_BLOCK = TypeAdapter(PromptContentBlock)


@dataclass(frozen=True)
class CapturedInput:
    """Every contribution frozen at one boundary, in consumption order."""

    contributions: tuple[Contribution, ...] = ()

    @property
    def skills(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for contribution in self.contributions:
            for skill in contribution.skills:
                seen.setdefault(skill, None)
        return tuple(seen)


def native_content(contribution: Contribution) -> list[UserContent]:
    """Native user content for one contribution."""
    if contribution.blocks:
        blocks = [
            block for raw in contribution.blocks if (block := _validate_block(raw)) is not None
        ]
        if blocks:
            return prompt_blocks_to_user_content(blocks)
    return [contribution.text]


def acp_blocks(contribution: Contribution) -> list[PromptContentBlock]:
    """ACP blocks for transcript replay; text contributions become text blocks."""
    from acp import schema

    blocks = [block for raw in contribution.blocks if (block := _validate_block(raw)) is not None]
    return blocks or [schema.TextContentBlock(text=contribution.text)]


def _validate_block(raw: dict[str, object]) -> PromptContentBlock | None:
    try:
        return _BLOCK.validate_python(raw)
    except ValueError:
        return None


class InputCoordinator:
    """The single acceptance/capture path shared by Python, ACP, and Loro."""

    def __init__(
        self,
        document: SessionDocument,
        *,
        persist: Callable[[], Awaitable[None]],
        on_capture: Callable[[tuple[Contribution, ...]], None] | None = None,
    ) -> None:
        self._document = document
        self._persist = persist
        self._on_capture = on_capture or (lambda contributions: None)

    async def accept(
        self,
        *,
        author: Author,
        text: str = "",
        blocks: Sequence[dict[str, object]] = (),
        skills: Sequence[str] = (),
        origin: str = "python",
        state: ContributionState = "ready",
        contribution_id: str | None = None,
    ) -> Contribution:
        """Write one contribution and make its acceptance durable.

        Ready contributions are persisted before returning; drafts are persisted
        but never dispatched.
        """
        contribution = self._document.create_contribution(
            author=author,
            text=text,
            blocks=blocks,
            skills=skills,
            origin=origin,
            state=state,
            contribution_id=contribution_id,
        )
        try:
            await self._persist()
        except BaseException:
            # A contribution whose acceptance was not durable must never dispatch.
            self._document.cancel_contribution(contribution.id)
            raise
        return contribution

    def eligible(self) -> list[Contribution]:
        return self._document.eligible()

    def pending(self) -> list[Contribution]:
        return [item for item in self._document.contributions() if item.state == "ready"]

    async def capture(self) -> CapturedInput:
        captured = self._document.capture_ready()
        if not captured:
            return CapturedInput()
        # The decision is durable before any model dispatch.
        await self._persist()
        result = CapturedInput(tuple(captured))
        self._on_capture(result.contributions)
        return result

    async def delivered(self, captured: CapturedInput) -> None:
        for contribution in captured.contributions:
            if contribution.consumed_seq is not None:
                self._document.mark_consumption(contribution.consumed_seq, "delivered")
        await self._persist()

    async def failed(self, captured: CapturedInput, outcome: str) -> None:
        for contribution in captured.contributions:
            if contribution.consumed_seq is not None:
                self._document.mark_consumption(
                    contribution.consumed_seq, "failed", outcome=outcome
                )
        await self._persist()

    async def cancel_ready(self) -> list[str]:
        cancelled = self._document.discard_inputs({"ready"})
        if cancelled:
            await self._persist()
        return cancelled

    async def interrupt_orphans(self) -> list[str]:
        """Mark captures that no execution delivered; never replay them silently."""
        if self._document.poisoned:
            return []
        interrupted: list[str] = []
        for record in self._document.consumption():
            if record.state != "captured":
                continue
            self._document.mark_consumption(
                record.seq, "failed", outcome="interrupted before delivery"
            )
            interrupted.append(record.contribution_id)
        if interrupted:
            await self._persist()
        return interrupted


def messages_for(
    captured: CapturedInput,
    injections: Mapping[str, Sequence[str]] | None = None,
) -> list[ModelMessage]:
    """One native request per captured contribution, with author metadata."""
    messages: list[ModelMessage] = []
    for contribution in captured.contributions:
        content: list[UserContent] = list(native_content(contribution))
        injected = injections.get(contribution.id) if injections else None
        if injected:
            content = [*injected, *content]
        messages.append(
            ModelRequest(
                parts=[UserPromptPart(content=content)],
                metadata={
                    "author": contribution.author.id,
                    "author_label": contribution.author.label,
                    "author_kind": contribution.author.kind,
                    "contribution_id": contribution.id,
                    "consumption_seq": contribution.consumed_seq,
                    "capture_version": contribution.capture_version,
                },
            )
        )
    return messages


class InputCapture(AbstractCapability[AgentDepsT]):
    """Capture input at every model request, after any preceding tool step."""

    def __init__(
        self,
        capture: Callable[[], Awaitable[list[ModelMessage]]],
    ) -> None:
        self._capture = capture

    async def before_model_request(
        self, ctx: RunContext[AgentDepsT], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        messages = await self._capture()
        if messages:
            request_context.messages = [*request_context.messages, *messages]
            ctx.messages.extend(messages)
        return request_context
