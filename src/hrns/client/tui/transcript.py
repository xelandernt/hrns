"""Streaming transcript pane, command results, and tool cards."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from ..constants import (
    UI_MAX_TOOL_CONTENT_CHARS,
    UI_MAX_TRANSCRIPT_BLOCKS,
    UI_STREAM_COALESCE_SECONDS,
)
from ..renderers import RendererRegistry, readable_text
from .blocks import MessageBlock, NoticeBlock, ReasoningBlock
from .formatting import (
    SPINNER_FRAMES,
    TOOL_OUTPUT_INDENT,
    TOOL_PREVIEW_LINES,
    bounded_user_text,
    truncate_text,
)
from .history import HistoryEntry, HistoryPane
from .messages import OutputPending
from .state import (
    is_shell_call,
    merge_presentation,
    merge_tool_event,
    presentation_payload,
    tool_args_preview,
)


def _presentation_of(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """The richest presentation payload a tool call has published so far."""
    payload = (
        data.get("presentationOutput")
        or data.get("presentationInput")
        or data.get("presentationProgress")
        or presentation_payload(dict(data))
    )
    return dict(payload) if isinstance(payload, Mapping) else None


def _tool_view_state(data: Mapping[str, Any], *, live: bool | None = None) -> Any:
    """One immutable snapshot for a tool body, an approval body, or both."""
    from .extensions import ToolViewState

    pending = str(data.get("status") or "") in {"", "pending", "in_progress"}
    return ToolViewState(
        session_id=str(data.get("sessionId") or ""),
        tool_call_id=str(data.get("toolCallId") or ""),
        title=str(data.get("title") or ""),
        kind=str(data.get("kind") or ""),
        status=str(data.get("status") or ""),
        raw_input=data.get("raw_input"),
        raw_output=data.get("raw_output"),
        input=data.get("presentationInput"),
        output=data.get("presentationOutput"),
        progress=data.get("presentationProgress"),
        fallback=str(data.get("presentationFallback") or ""),
        revision=int(data.get("presentationRevision") or 0),
        live=pending if live is None else live,
        binding_id=data.get("bindingId"),
    )


class CommandResultBlock(Vertical):
    """Inline output of one user-only plugin command.

    This is deliberately not an assistant message and not a tool card: it has no model
    role, no tool status, and never enters native history. Its fallback text participates in
    transcript search and copy; a local plugin widget may replace the body.
    """

    DEFAULT_CSS = """
    CommandResultBlock {
        height: auto;
        margin: 0 0 1 0;
    }
    CommandResultBlock .command-header {
        height: 1;
        color: $text-muted;
    }
    CommandResultBlock .command-body {
        margin: 0 0 0 2;
    }
    """

    def __init__(
        self,
        data: dict[str, Any],
        *,
        view: Any = None,
    ) -> None:
        super().__init__()
        self.data = dict(data)
        self._text = readable_text(
            str(self.data.get("fallback") or ""), limit=UI_MAX_TOOL_CONTENT_CHARS
        )
        self._header = Static(self._header_text(), markup=False)
        self._header.add_class("command-header")
        self._body = Static(self._text, markup=False)
        self._body.add_class("command-body")
        self._view = view
        self.add_class("command-result")

    @property
    def tool_call_id(self) -> str:
        return ""

    def _header_text(self) -> Text:
        binding_id = str(self.data.get("bindingId") or "")
        command_name = str(self.data.get("commandName") or "")
        text = Text()
        text.append("❯ ", style="bold")
        text.append(f"/{binding_id}:{command_name}", style="bold")
        text.append("  ")
        text.append(datetime.now().strftime("%H:%M"), style="dim")
        return text

    def compose(self) -> ComposeResult:
        yield self._header
        if self._view is not None:
            yield self._view.widget
        else:
            yield self._body

    def on_unmount(self) -> None:
        self._view = None


class TranscriptPane(HistoryPane):
    """Streaming transcript with a measured window over all retained block types."""

    can_focus = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tools: dict[str, HistoryEntry] = {}
        # Supplemental snapshots can arrive before the standard ACP tool-call start.
        self._pending_presentations: dict[str, Any] = {}
        self._stream: "MessageBlock | None" = None
        self._thought: "ReasoningBlock | None" = None
        self._dirty = False
        self._replay = False
        self._replay_entries: list[tuple[str, Any]] = []
        self.auto_expand_tools = False
        #: Session-scoped context for explicit plugin-widget actions.
        self.view_context: Any = None
        #: Expand reasoning text (the Thinking/Thought row appears either way).
        self.show_thoughts = False
        self._following = True
        self._navigation_block: HistoryEntry | None = None
        self.renderers = RendererRegistry()
        try:
            self.renderers.discover()
        except Exception:
            # A broken third-party entry point must leave generic rendering usable.
            pass
        from .tool_views import register_builtin_renderers

        register_builtin_renderers(self.renderers)

    def on_mount(self) -> None:
        self.set_interval(UI_STREAM_COALESCE_SECONDS, self.flush)
        self.anchor()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._schedule_visibility()
        if self._compensating_scroll or (
            self._following and self._anchored and not self._anchor_released
        ):
            return
        self._scroll_anchor = None
        if new_value < old_value:
            self._following = False
        if self.is_vertical_scroll_end:
            if not self._following:
                # Re-anchor after the refresh. Calling anchor() here would
                # force-stop the scroll animation that is currently assigning
                # scroll_y; the animator would then try to delete an animation
                # that was already removed, raising KeyError.
                self._following = True
                self.call_after_refresh(self._resume_following)
            self.post_message(OutputPending(False))
        elif not self._following:
            # Keep the jump-to-end affordance visible while the user reads back,
            # even when no new output arrives after they scroll away.
            self.post_message(OutputPending(True))

    def _resume_following(self) -> None:
        if self._following and self.is_mounted:
            self.anchor()

    def _output_updated(self) -> None:
        if not self._following:
            self.post_message(OutputPending(True))

    def jump_to_latest(self) -> None:
        if self._navigation_block is not None:
            self._navigation_block.block.remove_class("navigation-target")
        self._navigation_block = None
        self._following = True
        self.anchor()
        self.post_message(OutputPending(False))

    def jump_user(self, direction: int) -> None:
        messages = [
            entry
            for entry in self._blocks
            if isinstance(entry.block, MessageBlock) and entry.block.role == "you"
        ]
        if not messages:
            return
        if self._navigation_block in messages:
            index = messages.index(self._navigation_block) + direction
        else:
            visible = [i for i, block in enumerate(messages) if block.offset <= self.scroll_y + 1]
            index = (visible[-1] if visible else 0) + (1 if direction > 0 else -1)
            if self.is_vertical_scroll_end and direction < 0:
                index = len(messages) - 1
        block = messages[max(0, min(index, len(messages) - 1))]
        self.reveal(block)

    def reveal(self, entry: HistoryEntry) -> None:
        if entry not in self._blocks:
            return
        block = entry.block
        if self._navigation_block is not None:
            self._navigation_block.block.remove_class("navigation-target")
        block.add_class("navigation-target")
        if isinstance(block, ToolCard):
            block._expanded = True
            block._refresh()
        elif isinstance(block, ReasoningBlock):
            block.collapsed = False
        self._following = False
        self.anchor(False)
        self._navigation_block = entry
        self.reveal_entry(entry)
        self.focus()

    def search_entries(self) -> list[tuple[HistoryEntry, str, str]]:
        entries: list[tuple[HistoryEntry, str, str]] = []
        for entry in self._blocks:
            block = entry.block
            if isinstance(block, MessageBlock):
                entries.append((entry, block.role, block._text))
            elif isinstance(block, ToolCard):
                text = f"{block.data.get('title', 'Tool')}\n{block.data.get('detail') or block._body_text}"
                entries.append((entry, "tool", text))
            elif isinstance(block, ReasoningBlock):
                entries.append((entry, "reasoning", block._text))
            elif isinstance(block, CommandResultBlock):
                entries.append((entry, "command", block._text))
        return entries

    def latest_response(self) -> MessageBlock | None:
        return next(
            (
                entry.block
                for entry in reversed(self._blocks)
                if isinstance(entry.block, MessageBlock)
                and entry.block.role == "agent"
                and entry.block is not self._stream
            ),
            None,
        )

    def _tool_card(self, data: dict[str, Any]) -> "ToolCard":
        return ToolCard(
            data,
            auto_expand=self.auto_expand_tools,
            renderer=self.renderers,
            view_context=self.view_context,
        )

    def tool_data(self, tool_call_id: str) -> dict[str, Any] | None:
        """The current snapshot of one tool call, if its card is still in the transcript."""
        entry = self._tools.get(tool_call_id)
        return (
            dict(entry.block.data)
            if entry is not None and isinstance(entry.block, ToolCard)
            else None
        )

    def restore_block(self, block: Widget) -> Widget:
        if isinstance(block, MessageBlock):
            restored = MessageBlock(block.role, block._text)
            restored._label.update(block._label.render())
            restored._dropped = block._dropped
            restored._marked = block._marked
            if block._finished:
                restored.finish()
            if self._stream is block:
                self._stream = restored
        elif isinstance(block, ToolCard):
            restored = self._tool_card(block.data)
            restored._expanded = block._expanded
            restored._started = block._started
            restored._finished = block._finished
        elif isinstance(block, ReasoningBlock):
            restored = ReasoningBlock(expanded=not block.collapsed)
            restored._text = block._text
            restored._dropped = block._dropped
            restored._marked = block._marked
            restored._started = block._started
            restored._finished = block._finished
            restored._body.update(readable_text(block._text))
            if self._thought is block:
                self._thought = restored
        elif isinstance(block, NoticeBlock):
            restored = NoticeBlock(block.text, severity=block.severity)
        elif isinstance(block, CommandResultBlock):
            restored = self._command_block(block.data)
            restored._header.update(block._header.render())
        else:
            raise TypeError(f"Unsupported history block: {type(block).__name__}")
        if block.has_class("navigation-target"):
            restored.add_class("navigation-target")
        return restored

    # ------------------------------------------------------------- mounting

    async def _mount_block(self, block: Widget) -> HistoryEntry:
        entry = await self._append_history(block)
        self._output_updated()
        return entry

    async def add_user(self, text: str) -> None:
        text = bounded_user_text(text)
        if self._replay:
            self._replay_entries.append(("user", text))
            return
        await self._mount_block(MessageBlock("you", text))

    async def add_notice(self, text: str, *, severity: str = "info") -> None:
        text = readable_text(text, limit=UI_MAX_TOOL_CONTENT_CHARS)
        if self._replay:
            self._replay_entries.append(("notice", (text, severity)))
            return
        await self._mount_block(NoticeBlock(text, severity=severity))

    async def add_command(self, data: dict[str, Any], *, view: Any = None) -> None:
        """Show a user-only command result as its own inline block."""
        if self._replay:
            # Command output is current-UI-only; a native session/load replays only the
            # upstream transcript, so this is intentionally not appended to replay.
            return
        self._end_thought()
        self._end_agent_stream()
        await self._mount_block(self._command_block(data, view=view))

    def _command_block(self, data: dict[str, Any], *, view: Any = None) -> CommandResultBlock:
        if view is None and self.view_context is not None:
            from .extensions import CommandViewState, build_view

            payload = data.get("presentation")
            if payload is not None:
                state = CommandViewState(
                    session_id=str(data.get("sessionId") or ""),
                    binding_id=str(data.get("bindingId") or ""),
                    command_name=str(data.get("commandName") or ""),
                    presentation=payload if isinstance(payload, dict) else None,
                    fallback=str(data.get("fallback") or ""),
                    request_id=data.get("requestId"),
                )
                view = build_view(self.renderers, payload, "command", state, self.view_context)
        return CommandResultBlock(data, view=view)

    async def append_agent_text(self, text: str, *, thought: bool = False) -> None:
        if not text:
            return
        if self._replay:
            kind = "thought" if thought else "agent"
            if self._replay_entries and self._replay_entries[-1][0] == kind:
                # session/load often replays token-sized chunks. Coalesce them
                # exactly like live streaming instead of retaining one tuple per token.
                previous = self._replay_entries[-1][1]
                self._replay_entries[-1] = (kind, previous + text)
            else:
                self._replay_entries.append((kind, text))
            return
        if thought:
            if self._thought is None:
                self._thought = ReasoningBlock(expanded=self.show_thoughts)
                await self._mount_block(self._thought)
            self._thought.append_text(text)
            self._output_updated()
        else:
            self._end_thought()
            if self._stream is None:
                self._stream = MessageBlock("agent", "")
                await self._mount_block(self._stream)
            self._stream.append_text(text)
            self._dirty = True

    def _end_thought(self) -> None:
        """Finish the reasoning segment so its spinner stops before other output."""
        if self._thought is not None:
            self._thought.finish()
        self._thought = None

    async def finish_message(self) -> None:
        self.flush()
        if self._stream is not None:
            self._stream.finish()
        self._end_thought()
        self._stream = None
        self._schedule_visibility()

    def flush(self) -> None:
        if not self._dirty or self._stream is None:
            self._dirty = False
            return
        self._stream.flush()
        self._dirty = False
        self._output_updated()

    def _end_agent_stream(self) -> None:
        """Finish the current assistant block so later text starts below a tool card."""
        self.flush()
        if self._stream is not None:
            self._stream.finish()
        self._stream = None

    async def start_tool(self, event: Any) -> None:
        if self._replay:
            existing = self._replay_tool(event.tool_call_id)
            self._replay_entries.append(("tool", merge_tool_event(existing, event)))
            return
        self._end_thought()
        self._end_agent_stream()
        merged = merge_tool_event(None, event)
        snapshot = self._pending_presentations.pop(merged["toolCallId"], None)
        if snapshot is not None:
            merged = merge_presentation(merged, snapshot)
        card = self._tool_card(merged)
        self._tools[merged["toolCallId"]] = await self._mount_block(card)

    async def update_tool(self, event: Any) -> None:
        if self._replay:
            existing = self._replay_tool(event.tool_call_id)
            merged = merge_tool_event(existing, event)
            for index in range(len(self._replay_entries) - 1, -1, -1):
                kind, payload = self._replay_entries[index]
                if kind == "tool" and payload.get("toolCallId") == event.tool_call_id:
                    self._replay_entries[index] = ("tool", merged)
                    break
            else:
                self._replay_entries.append(("tool", merged))
            return
        entry = self._tools.get(event.tool_call_id)
        card = entry.block if entry is not None else None
        if not isinstance(card, ToolCard):
            await self.start_tool(event)
            return
        merged = merge_tool_event(card.data, event)
        snapshot = self._pending_presentations.pop(event.tool_call_id, None)
        if snapshot is not None:
            merged = merge_presentation(merged, snapshot)
        card.update_from(merged)
        self._output_updated()

    def _replay_tool(self, tool_call_id: str) -> dict[str, Any] | None:
        for kind, payload in reversed(self._replay_entries):
            if kind == "tool" and payload.get("toolCallId") == tool_call_id:
                return payload
        return None

    async def add_tool_snapshot(self, data: dict[str, Any]) -> None:
        if self._replay:
            tool_id = data.get("toolCallId", "")
            for index in range(len(self._replay_entries) - 1, -1, -1):
                kind, payload = self._replay_entries[index]
                if kind == "tool" and payload.get("toolCallId") == tool_id:
                    self._replay_entries[index] = ("tool", {**payload, **data})
                    return
            self._replay_entries.append(("tool", data))
            return
        card = self._tool_card(data)
        self._tools[data.get("toolCallId", "")] = await self._mount_block(card)

    async def apply_presentation(self, snapshot: Any) -> None:
        """Merge a supplemental snapshot into the matching live or replayed tool card."""
        tool_id = getattr(snapshot, "tool_call_id", "")
        if not tool_id:
            return
        if self._replay:
            for index in range(len(self._replay_entries) - 1, -1, -1):
                kind, payload = self._replay_entries[index]
                if kind == "tool" and payload.get("toolCallId") == tool_id:
                    self._replay_entries[index] = (
                        "tool",
                        merge_presentation(payload, snapshot),
                    )
                    return
            self._pending_presentations[tool_id] = snapshot
            self._bound_pending()
            return
        entry = self._tools.get(tool_id)
        card = entry.block if entry is not None else None
        if isinstance(card, ToolCard):
            card.update_from(merge_presentation(card.data, snapshot))
            self._output_updated()
            return
        self._pending_presentations[tool_id] = snapshot
        self._bound_pending()

    def _bound_pending(self) -> None:
        while len(self._pending_presentations) > UI_MAX_TRANSCRIPT_BLOCKS:
            self._pending_presentations.pop(next(iter(self._pending_presentations)))

    async def clear_transcript(self) -> None:
        self._stream = None
        self._thought = None
        self._following = True
        self._navigation_block = None
        self.anchor()
        self.post_message(OutputPending(False))
        self._tools.clear()
        self._pending_presentations.clear()
        await self.clear_history()

    # ---------------------------------------------------------------- replay

    def begin_replay(self) -> None:
        self._replay = True
        self._replay_entries = []

    async def end_replay(self) -> None:
        entries = self._replay_entries
        self._replay = False
        self._replay_entries = []
        await self.clear_transcript()
        for kind, payload in entries:
            if kind == "user":
                # A replayed user prompt is a turn boundary. Without this, the
                # next agent chunk would append to the previous turn's block.
                await self.finish_message()
                await self.add_user(payload)
            elif kind == "agent":
                await self.append_agent_text(payload)
            elif kind == "thought":
                await self.append_agent_text(payload, thought=True)
            elif kind == "notice":
                await self.add_notice(payload[0], severity=payload[1])
            elif kind == "tool":
                await self.add_tool_snapshot(payload)
        await self.finish_message()

    def retained_tool_ids(self) -> list[str]:
        """Tool-call ids retained in the transcript, for passive presentation replay."""
        ids: list[str] = []
        for entry in self._blocks:
            block = entry.block
            if isinstance(block, ToolCard) and block.tool_call_id:
                ids.append(block.tool_call_id)
        return ids


class ToolCard(Vertical):
    """An inline tool activity row with a short, expandable result preview.

    The general layout mirrors the Pydantic Deep Agents terminal: one header
    line carries the status mark or spinner, the tool title, compact arguments,
    elapsed time and a diff badge. Results appear underneath as a dim,
    indented ``⎿`` preview capped at :data:`TOOL_PREVIEW_LINES` lines. A click or
    the expand action shows the full result in place instead of opening a separate
    details panel.
    """

    can_focus = True
    BINDINGS = [Binding("enter", "toggle_expand", "Expand output", show=False)]

    def __init__(
        self,
        data: dict[str, Any],
        *,
        auto_expand: bool = False,
        renderer: RendererRegistry | None = None,
        view_context: Any = None,
    ) -> None:
        super().__init__()
        self.data = dict(data)
        self.renderer = renderer
        self.view_context = view_context
        self._view: Any = None
        self._view_built = False
        self._expanded = auto_expand
        self._started = time.monotonic()
        self._finished: float | None = None
        self._spin_index = 0
        self._timer: Timer | None = None
        self._body_text = ""
        self._header = Static(markup=False)
        self._header.add_class("tool-header")
        self._preview = Static("", markup=False)
        self._preview.add_class("tool-output")
        self._full = Static("", markup=False)
        self._full.add_class("tool-output-expanded")
        self._view_host = Vertical(classes="tool-view")
        self.add_class("tool-card")
        self._build_view()
        self._refresh()

    def _build_view(self) -> None:
        from .extensions import build_view

        if self._view_built or self.renderer is None or self.view_context is None:
            return
        payload = _presentation_of(self.data)
        if payload is None:
            return
        view = build_view(
            self.renderer,
            payload,
            "tool",
            self._view_state(),
            self.view_context,
        )
        if view is None:
            return
        self._view_built = True
        self._view = view
        if self.is_mounted and not view.widget.is_mounted:
            self._view_host.mount(view.widget)

    def compose(self) -> ComposeResult:
        yield Static(classes="transcript-bar")
        yield self._header
        yield self._view_host
        yield self._preview
        yield self._full

    def on_mount(self) -> None:
        self._build_view()
        if self._view is not None and not self._view.widget.is_mounted:
            self._view_host.mount(self._view.widget)
        if self._is_pending():
            self._timer = self.set_interval(0.1, self._tick)
        self._refresh()

    def on_unmount(self) -> None:
        self._stop_spinner()
        self._view = None

    def _view_state(self) -> Any:
        return _tool_view_state(self.data, live=self._is_pending())

    def _apply_view(self) -> bool:
        if self._view is None:
            return False
        if not self.is_mounted:
            return True
        try:
            self._view.apply(self._view_state())
        except Exception:
            # A failing plugin widget degrades to the generic tool body; the outer
            # identity, status and approval handling stay usable.
            with contextlib.suppress(Exception):
                self._view.widget.display = False
            self._view = None
            return False
        return True

    @property
    def tool_call_id(self) -> str:
        return self.data.get("toolCallId", "")

    def _is_pending(self) -> bool:
        return str(self.data.get("status") or "") in {"", "pending", "in_progress"}

    def _elapsed(self) -> float | None:
        if self._finished is not None:
            return self._finished
        if self._is_pending():
            return max(0.0, time.monotonic() - self._started)
        return None

    def _stop_spinner(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        if not self._is_pending():
            self._stop_spinner()
            return
        self._spin_index = (self._spin_index + 1) % len(SPINNER_FRAMES)
        self._header.update(self._header_text())

    def _header_text(self) -> Text:
        pending = self._is_pending()
        status = str(self.data.get("status") or "")
        if pending:
            symbol = SPINNER_FRAMES[self._spin_index]
            word = {"pending": "pending", "in_progress": "running"}.get(status, "queued")
        elif status == "completed":
            symbol, word = "✓", "completed"
        elif status == "failed":
            symbol, word = "✗", "failed"
        else:
            symbol, word = "•", status

        title = readable_text(
            self.data.get("title") or self.data.get("toolCallId") or "tool",
            limit=200,
        )
        width = self.size.width
        args_limit = max(32, width - 26) if width else 80
        args = tool_args_preview(self.data, limit=args_limit)
        text = Text()
        text.append(
            symbol, style="bold #ff8787" if status == "failed" else "bold" if not pending else "dim"
        )
        text.append(" ")
        if is_shell_call(self.data) and args:
            # Shell calls read like a prompt line: ``$ <command>``.
            text.append("$ ", style="bold")
            text.append(args)
        else:
            text.append(title)
            if args:
                text.append("(", style="dim")
                text.append(args, style="dim")
                text.append(")", style="dim")

        added = int(self.data.get("diffAdded") or 0)
        removed = int(self.data.get("diffRemoved") or 0)
        if added or removed:
            text.append("  ")
            if added:
                text.append(f"+{added}", style="bold")
            if added and removed:
                text.append(" ")
            if removed:
                text.append(f"-{removed}", style="bold")

        text.append("  ")
        text.append(word, style="dim")
        elapsed = self._elapsed()
        if elapsed is not None:
            text.append(f"  {elapsed:.1f}s", style="dim")
        if self._body_text:
            text.append("  ")
            text.append("▾" if self._expanded else "▸", style="dim")
        return text

    def _detail_text(self, source: str) -> str:
        generic = readable_text(source).strip()
        payload = presentation_payload(self.data)
        if payload is not None:
            fallback = self.data.get("presentationFallback") or payload.get("fallback")
            if fallback:
                return readable_text(fallback).strip()
        return generic

    def _render_header(self) -> None:
        self._header.update(self._header_text())

    def _render_lines(self, lines: list[str], *, limit: int | None) -> Text:
        selected = lines if limit is None else lines[:limit]
        # The collapsed preview keeps one row per output line; wrapping long
        # shell output would defeat the point of collapsing it.
        room = 0
        if limit is not None:
            width = self._preview.size.width or self.size.width
            room = max(0, width - len(TOOL_OUTPUT_INDENT)) if width else 0
        text = Text()
        for index, line in enumerate(selected):
            if index:
                text.append("\n")
            if room:
                line = truncate_text(line, room)
            text.append(TOOL_OUTPUT_INDENT, style="dim")
            # Only the diff markers get weight; the rest stays quiet so the
            # transcript reads as one calm stream.
            style = "bold" if line.startswith(("+ ", "- ")) else "dim"
            text.append(line, style=style)
        if limit is not None and len(lines) > limit:
            if selected:
                text.append("\n")
            text.append(TOOL_OUTPUT_INDENT, style="dim")
            remaining = len(lines) - limit
            plural = "line" if remaining == 1 else "lines"
            text.append(f"… ({remaining} more {plural})", style="dim")
        return text

    def _render_body(self) -> None:
        # The preview is the result only, while the expanded view also carries
        # the full argument payload when the peer supplies one.
        custom = self._apply_view()
        preview_source = self.data.get("result") or self.data.get("detail") or ""
        full_source = self.data.get("detail") or self.data.get("result") or ""
        raw_input = self.data.get("raw_input")
        if raw_input is not None:
            rendered_input = readable_text(raw_input, limit=UI_MAX_TOOL_CONTENT_CHARS)
            if rendered_input and rendered_input not in full_source:
                prefix = f"{full_source}\n\n" if full_source else ""
                full_source = f"{prefix}input\n{rendered_input}"
        preview_body = self._detail_text(preview_source)
        full_body = (
            preview_body if full_source == preview_source else self._detail_text(full_source)
        )
        self._body_text = preview_body or full_body

        preview_lines = preview_body.splitlines()
        full_lines = full_body.splitlines()
        self._preview.update(self._render_lines(preview_lines, limit=TOOL_PREVIEW_LINES))
        if self._expanded:
            self._full.update(self._render_lines(full_lines, limit=None))

        # A plugin view is the expanded body: collapsed cards keep the generic
        # preview (or nothing, while the call is still running) so a rich view can
        # never make the transcript tall by default.
        show_custom = custom and (self._expanded or self._is_pending())
        if custom:
            self._view_host.display = show_custom
        preview_visible = (
            bool(preview_lines)
            and not self._expanded
            and not self._is_pending()
            and not show_custom
        )
        full_visible = bool(full_lines) and self._expanded and not custom
        self._preview.set_class(preview_visible, "visible")
        self._full.set_class(full_visible, "visible")

    def _refresh(self) -> None:
        self._build_view()
        self._render_body()
        self._render_header()

    def action_toggle_expand(self) -> None:
        if not self._body_text:
            return
        self._expanded = not self._expanded
        self._refresh()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_toggle_expand()

    def on_resize(self, event: events.Resize) -> None:
        """Re-clip the collapsed preview when the transcript width changes."""
        if self._body_text and self.is_mounted:
            self._render_body()

    def update_from(self, data: dict[str, Any]) -> None:
        was_pending = self._is_pending()
        self.data = dict(data)
        if was_pending and not self._is_pending():
            self._finished = max(0.0, time.monotonic() - self._started)
            self._stop_spinner()
        self._refresh()
