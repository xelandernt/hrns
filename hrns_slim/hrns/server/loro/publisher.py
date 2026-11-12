"""Project one native run into the session document.

The publisher is fed from the same ACP-update choke point as transport delivery,
so a single native stream produces both. Streamed text is buffered and committed
at boundaries instead of writing one CRDT operation per token. Display records
are projections: they never replace native model history.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from acp import schema

from hrns.server.acp.session import SessionUpdate

from .document import DisplaySlot, SessionDocument
from .models import DisplayKind, LifecycleStatus

#: Flush buffered streamed text after this many characters.
_TEXT_FLUSH_THRESHOLD = 1024


def _text_content(update: SessionUpdate) -> str | None:
    content = getattr(update, "content", None)
    if isinstance(content, schema.TextContentBlock):
        return content.text
    return None


def _raw(update: SessionUpdate) -> dict[str, object]:
    return update.model_dump(mode="json", by_alias=True, exclude_none=True)


@dataclass
class _TextStream:
    kind: DisplayKind
    slot: DisplaySlot
    chunks: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(len(chunk) for chunk in self.chunks)


class DocumentPublisher:
    """Write live display content, status, and session settings into a document."""

    def __init__(self, document: SessionDocument) -> None:
        self._document = document
        self._text: _TextStream | None = None
        self._tools: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def document(self) -> SessionDocument:
        return self._document

    async def begin_turn(self, *, active_contribution_id: str | None = None) -> None:
        self._document.set_active_contribution(active_contribution_id)
        self._document.set_lifecycle_status("running")
        self._document.commit_lifecycle()

    async def publish(self, update: SessionUpdate) -> None:
        if self._closed:
            return
        async with self._lock:
            if isinstance(update, schema.AgentMessageChunk):
                await self._append_text("assistant", _text_content(update) or "", update)
            elif isinstance(update, schema.AgentThoughtChunk):
                await self._append_text("thinking", _text_content(update) or "", update)
            elif isinstance(update, schema.UserMessageChunk):
                await self._flush_text()
                self._user_record(_text_content(update) or "", update)
            elif isinstance(update, schema.ToolCallStart):
                await self._flush_text()
                self._tool_record(update)
            elif isinstance(update, schema.ToolCallProgress):
                await self._flush_text()
                self._tool_progress(update)
            elif isinstance(update, schema.AgentPlanUpdate):
                await self._flush_text()
                self._simple_record("plan", update, status="")
            elif isinstance(update, schema.UsageUpdate):
                await self._flush_text()
                self._simple_record("usage", update, status="")
            elif isinstance(update, schema.SessionInfoUpdate):
                await self._flush_text()
                self._simple_record("title", update, status="")

    async def record_user(self, text: str, *, acp: Sequence[dict[str, object]] = ()) -> None:
        """Record a captured contribution as a user display item."""
        async with self._lock:
            await self._flush_text()
            payload: dict[str, object] = {"acp": list(acp)}
            self._document.start_display("user", payload, status="complete", text=text)

    async def record_error(self, message: str) -> None:
        async with self._lock:
            await self._flush_text()
            self._document.start_display("error", {"message": message}, status="failed")

    async def end_turn(self, outcome: str) -> None:
        async with self._lock:
            await self._flush_text()
            if self._text is not None:
                self._document.set_display_status(self._text.slot.record.id, "complete")
                self._text = None
            self._document.set_last_outcome(outcome)
            self._document.set_lifecycle_status("idle")
            self._document.set_active_contribution(None)
            self._document.set_resume_required(False)
            self._document.commit_lifecycle()

    async def interrupt(self) -> None:
        """Flush partial display and mark it interrupted, without committing history."""
        async with self._lock:
            await self._flush_text()
            if self._text is not None:
                self._document.set_display_status(self._text.slot.record.id, "interrupted")
                self._text = None
            self._document.set_lifecycle_status("paused")
            self._document.set_resume_required(True)
            self._document.set_active_contribution(None)
            self._document.commit_lifecycle()

    async def pause(self) -> None:
        self._document.set_lifecycle_status("paused")
        self._document.commit_lifecycle()

    async def set_status(self, status: LifecycleStatus) -> None:
        self._document.set_lifecycle_status(status)
        self._document.commit_lifecycle()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_text()

    def close(self) -> None:
        self._closed = True

    # -- internals ------------------------------------------------------------

    async def _flush_text(self) -> None:
        stream = self._text
        if stream is None or not stream.chunks:
            return
        stream.slot.text.push_str("".join(stream.chunks))
        stream.chunks.clear()
        self._document.commit(origin="publisher")

    async def _append_text(self, kind: DisplayKind, text: str, update: SessionUpdate) -> None:
        if self._text is None or self._text.kind != kind:
            await self._flush_text()
            template = _raw(update)
            if "content" in template:
                template = {**template, "content": {"type": "text", "text": ""}}
            slot = self._document.start_display(kind, {"acp": [template]}, status="streaming")
            self._text = _TextStream(kind=kind, slot=slot)
        if text:
            self._text.chunks.append(text)
            if self._text.size >= _TEXT_FLUSH_THRESHOLD:
                await self._flush_text()

    def _user_record(self, text: str, update: SessionUpdate) -> None:
        self._document.start_display("user", {"acp": [_raw(update)]}, status="complete", text=text)

    def _tool_record(self, update: schema.ToolCallStart) -> None:
        slot = self._document.start_display(
            "tool_call",
            {
                "acp": [_raw(update)],
                "toolCallId": update.tool_call_id,
                "title": update.title,
                "kind": update.kind,
            },
            status=update.status or "in_progress",
        )
        self._tools[update.tool_call_id] = slot.record.id

    def _tool_progress(self, update: schema.ToolCallProgress) -> None:
        display_id = self._tools.get(update.tool_call_id)
        if display_id is None:
            self._tool_record(
                schema.ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=update.tool_call_id,
                    title=update.title or update.tool_call_id,
                    kind=update.kind,
                    status=update.status or "in_progress",
                )
            )
            return
        record = self._document.display_record(display_id)
        raw_list: list[object] = []
        if record is not None:
            existing = record.payload.get("acp")
            if isinstance(existing, list):
                raw_list = list(existing)
        raw_list.append(_raw(update))
        payload = dict(record.payload) if record is not None else {}
        payload["acp"] = raw_list
        if update.status is not None:
            payload["status"] = update.status
            self._document.set_display_status(display_id, update.status)
        self._document.set_display_payload(display_id, payload)

    def _simple_record(
        self,
        kind: DisplayKind,
        update: SessionUpdate,
        *,
        status: str,
    ) -> None:
        self._document.start_display(kind, {"acp": [_raw(update)]}, status=status)


__all__ = ["DocumentPublisher"]
