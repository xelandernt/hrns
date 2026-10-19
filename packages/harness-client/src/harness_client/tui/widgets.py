"""Terminal widgets: transcript, composer, status, capability/session/activity panels."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import (
    Collapsible,
    DataTable,
    Label,
    Markdown,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.option_list import Option

from ..constants import UI_MAX_TOOL_CONTENT_CHARS, UI_MAX_TRANSCRIPT_BLOCKS
from ..interactions import PermissionRequest
from ..renderers import RendererRegistry, readable_text
from .choice_keys import choice_direction
from .state import (
    binding_summary,
    config_option_value,
    is_subagent_tool,
    merge_tool_event,
    pending_application_text,
    presentation_payload,
    tool_args_preview,
)

__all__ = [
    "ActivityPanel",
    "ApprovalBar",
    "ApprovalDecided",
    "CapabilityPanel",
    "CommandSuggestionKey",
    "CommandSuggestions",
    "Composer",
    "HintBar",
    "SidePanel",
    "SessionPanel",
    "StatusBar",
    "TranscriptPane",
    "TopBar",
]

# One monochrome marker per tool call; status carries the meaning while it
# runs. The result body is shown as a short ``⎿`` preview beneath the header.
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
TOOL_PREVIEW_LINES = 12
TOOL_OUTPUT_INDENT = "    ⎿  "

# Retained text per message block is bounded so a single very long streaming
# reply cannot grow the widget tree or make Markdown re-renders unbounded.
MAX_STREAM_MESSAGE_CHARS = 24_000


def bounded_user_text(text: str) -> str:
    """Keep a very large user prompt from creating an unbounded Static."""
    if len(text) <= MAX_STREAM_MESSAGE_CHARS:
        return text
    return "…" + text[-(MAX_STREAM_MESSAGE_CHARS - 1):]


class Submitted(Message):
    """The composer wants to submit its text."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class CommandSuggestionKey(Message):
    """A navigation key consumed by the inline command picker."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key


class CommandSuggestions(OptionList):
    """Compact, non-focusable command list shown above the composer."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("id", "command-suggestions")
        kwargs.setdefault("markup", False)
        super().__init__(**kwargs)
        self.display = False

    def show_entries(self, entries: list[tuple[str, str]], highlighted: int = 0) -> None:
        self.clear_options()
        if not entries:
            self.hide_entries()
            return
        for label, option_id in entries:
            self.add_option(Option(label, id=option_id))
        self.highlighted = max(0, min(highlighted, len(entries) - 1))
        self.display = True

    def hide_entries(self) -> None:
        self.display = False
        self.clear_options()


class Composer(TextArea):
    """Multiline prompt editor with inline command suggestions."""

    BINDINGS = [
        Binding("ctrl+enter", "submit", "Send", show=False, priority=True),
        Binding("ctrl+j", "submit", "Send", show=False, priority=True),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.show_line_numbers = False
        self.soft_wrap = True
        self.suggestions_open = False

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if self.suggestions_open:
                self.post_message(CommandSuggestionKey("enter"))
            else:
                self.action_submit()
            return
        if self.suggestions_open and event.key in {"up", "down", "tab", "escape"}:
            event.prevent_default()
            event.stop()
            self.post_message(CommandSuggestionKey(event.key))
            return
        # Let TextArea's own ``_on_key`` handle printable input and navigation.

    def on_blur(self) -> None:
        if self.suggestions_open:
            self.post_message(CommandSuggestionKey("escape"))

    def action_submit(self) -> None:
        text = self.text.strip()
        if text:
            self.post_message(Submitted(text))

    def clear_composer(self) -> None:
        self.load_text("")


class TopBar(Static):
    """Location and connection summary; text labels, never color alone."""

    def show(
        self,
        *,
        target_text: str,
        agent_text: str,
        extension_text: str,
        session_id: str | None,
        connected: bool,
    ) -> None:
        text = Text()
        text.append("harness", style="bold")
        text.append("  ")
        text.append(target_text or "no connection", style="dim")
        text.append("\n")
        text.append(agent_text, style="bold")
        text.append("   ")
        text.append("connected" if connected else "disconnected", style="bold" if connected else "dim")
        if session_id:
            text.append("   session ", style="dim")
            text.append(session_id)
        text.append("   ", style="dim")
        text.append(extension_text, style="dim")
        self.update(text)


class TranscriptPane(VerticalScroll):
    """Streaming transcript with bounded retained widgets.

    Assistant text is coalesced and flushed on a ticker. Tool cards are updated
    in place by id. The pane keeps at most ``UI_MAX_TRANSCRIPT_BLOCKS`` blocks,
    dropping oldest history first.
    """

    can_focus = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # No deque maxlen: it silently drops items without running the prune
        # loop, which is where the tool-card registry is kept consistent.
        self._blocks: deque[Any] = deque()
        self._tools: dict[str, "ToolCard"] = {}
        self._stream: "MessageBlock | None" = None
        self._stream_buffer = ""
        self._thought: "ReasoningBlock | None" = None
        self._thought_buffer = ""
        self._dirty = False
        self._replay = False
        self._replay_entries: list[tuple[str, Any]] = []
        self.auto_expand_tools = False
        #: Expand reasoning text (the Thinking/Thought row appears either way).
        self.show_thoughts = False
        self._ticker: Any = None
        self.renderers = RendererRegistry()
        try:
            self.renderers.discover()
        except Exception:
            # A broken third-party entry point must leave generic rendering usable.
            pass

    def on_mount(self) -> None:
        self._ticker = self.set_interval(0.06, self.flush)

    def _tool_card(self, data: dict[str, Any]) -> "ToolCard":
        return ToolCard(
            data,
            auto_expand=self.auto_expand_tools,
            renderer=self.renderers,
        )

    # ------------------------------------------------------------- mounting

    async def _mount_block(self, block: Any) -> None:
        self._blocks.append(block)
        await self.mount(block)
        while len(self._blocks) > UI_MAX_TRANSCRIPT_BLOCKS:
            oldest = self._blocks.popleft()
            if isinstance(oldest, ToolCard):
                # Evicted cards must not linger in the update registry.
                self._tools.pop(oldest.tool_call_id, None)
            if oldest is self._stream or oldest is self._thought:
                self._blocks.append(oldest)
                break
            await oldest.remove()
        self.scroll_end(animate=False)

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
            self._thought_buffer += text
            self._thought.append_text(text)
            self.scroll_end(animate=False)
        else:
            self._end_thought()
            if self._stream is None:
                self._stream = MessageBlock("agent", "")
                await self._mount_block(self._stream)
            self._stream_buffer += text
            self._stream.append_text(text)
            self._dirty = True

    def _end_thought(self) -> None:
        """Finish the reasoning segment so its spinner stops before other output."""
        if self._thought is not None:
            self._thought.finish()
        self._thought = None
        self._thought_buffer = ""

    async def finish_message(self) -> None:
        self.flush()
        if self._stream is not None:
            self._stream.finish()
        self._end_thought()
        self._stream = None
        self._stream_buffer = ""

    def flush(self) -> None:
        if not self._dirty or self._stream is None:
            self._dirty = False
            return
        self._stream.flush()
        self._dirty = False
        self.scroll_end(animate=False)

    def _end_agent_stream(self) -> None:
        """Finish the current assistant block so later text starts below a tool card."""
        self.flush()
        if self._stream is not None:
            self._stream.finish()
        self._stream = None
        self._stream_buffer = ""

    async def start_tool(self, event: Any) -> None:
        if self._replay:
            existing = self._replay_tool(event.tool_call_id)
            self._replay_entries.append(("tool", merge_tool_event(existing, event)))
            return
        self._end_thought()
        self._end_agent_stream()
        merged = merge_tool_event(None, event)
        card = self._tool_card(merged)
        self._tools[merged["toolCallId"]] = card
        await self._mount_block(card)

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
        card = self._tools.get(event.tool_call_id)
        if card is None:
            await self.start_tool(event)
            return
        card.update_from(merge_tool_event(card.data, event))
        self.scroll_end(animate=False)

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
        self._tools[data.get("toolCallId", "")] = card
        await self._mount_block(card)

    async def clear_transcript(self) -> None:
        self._stream = None
        self._thought = None
        self._stream_buffer = ""
        self._thought_buffer = ""
        self._tools.clear()
        self._blocks.clear()
        await self.remove_children()

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

    def replay_entries(self) -> list[tuple[str, Any]]:
        return list(self._replay_entries)


class MessageBlock(Vertical):
    """One message: a small role label and streaming Markdown body.

    Retained text is capped at ``MAX_STREAM_MESSAGE_CHARS``; older characters
    are dropped from the head and counted so the trim stays visible.
    """

    ROLE_LABELS = {"you": "You", "agent": "Assistant"}

    def __init__(self, role: str, text: str = "") -> None:
        super().__init__()
        self.role = role
        self._text = text
        self._dropped = 0
        self._marked = False
        label = Text()
        label.append(self.ROLE_LABELS.get(role, role.title()), style="bold")
        label.append("  ")
        label.append(datetime.now().strftime("%H:%M"), style="dim")
        self._label = Static(label, markup=False)
        self._label.add_class("message-role")
        self._body = Markdown(text) if role == "agent" else Static(text, markup=False)
        self._body.add_class("message-body")
        self.add_class(f"role-{role}")

    def compose(self) -> ComposeResult:
        yield self._label
        yield self._body

    def append_text(self, text: str) -> None:
        self._text += text
        if len(self._text) > MAX_STREAM_MESSAGE_CHARS:
            overflow = len(self._text) - MAX_STREAM_MESSAGE_CHARS
            self._dropped += overflow
            self._text = self._text[overflow:]
            if not self._marked:
                self._marked = True
                self._dropped += 1
                self._text = "…" + self._text[1:]

    def flush(self) -> None:
        marker = (
            f"⋯ {self._dropped:,} earlier characters of this message trimmed from view ⋯\n\n"
            if self._dropped
            else ""
        )
        # Markdown.update needs a live app for its theme; pre-mount callers are
        # buffered until on_mount rather than raising NoActiveAppError.
        if self.is_mounted:
            self._body.update(marker + readable_text(self._text))

    def on_mount(self) -> None:
        self.flush()

    def finish(self) -> None:
        self.flush()


class ReasoningBlock(Collapsible):
    """Agent-provided reasoning with a live ``Thinking`` indicator.

    Mirrors OpenCode's presentation: a spinner and ``Thinking`` while tokens
    arrive, then ``Thought`` with the elapsed time. The body follows the user's
    reasoning preference; providers that do not stream thoughts never create it.
    """

    DEFAULT_CSS = """
    ReasoningBlock {
        background: transparent;
        border-top: none;
        padding: 0 0 0 1;
        margin: 0 0 1 1;
        height: auto;
    }
    ReasoningBlock CollapsibleTitle {
        background: transparent;
        color: $warning;
        padding: 0;
        width: auto;
    }
    ReasoningBlock CollapsibleTitle:hover,
    ReasoningBlock CollapsibleTitle:focus {
        background: transparent;
        color: $foreground;
    }
    ReasoningBlock > Contents {
        padding: 0 0 0 2;
    }
    """

    def __init__(self, *, expanded: bool = False) -> None:
        self._text = ""
        self._dropped = 0
        self._marked = False
        self._started = time.monotonic()
        self._finished: float | None = None
        self._spin_index = 0
        self._timer: Any = None
        self._body = Static("", markup=False)
        self._body.add_class("reasoning-body")
        super().__init__(self._body, title="Thinking", collapsed=not expanded)
        self.add_class("reasoning-block")

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick)
        self._refresh_title()

    def on_unmount(self) -> None:
        self._stop_spinner()

    def _stop_spinner(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        if self._finished is not None:
            self._stop_spinner()
            return
        self._spin_index = (self._spin_index + 1) % len(SPINNER_FRAMES)
        self._refresh_title()

    def _refresh_title(self) -> None:
        title = Text()
        if self._finished is None:
            title.append(f"{SPINNER_FRAMES[self._spin_index]} ", style="bold")
            title.append("Thinking")
        else:
            title.append("Thought")
            title.append(f" · {self._finished - self._started:.1f}s", style="dim")
        self.title = title

    def append_text(self, text: str) -> None:
        self._text += text
        if len(self._text) > MAX_STREAM_MESSAGE_CHARS:
            overflow = len(self._text) - MAX_STREAM_MESSAGE_CHARS
            self._dropped += overflow
            self._text = self._text[overflow:]
            if not self._marked:
                self._marked = True
                self._dropped += 1
                self._text = "…" + self._text[1:]
        if self.is_mounted:
            self._body.update(readable_text(self._text))

    def finish(self) -> None:
        if self._finished is not None:
            return
        self._finished = time.monotonic()
        self._stop_spinner()
        if self.is_mounted:
            self._body.update(readable_text(self._text))
        self._refresh_title()


class NoticeBlock(Static):
    def __init__(self, text: str, *, severity: str = "info") -> None:
        self.severity = severity
        super().__init__(self._render_notice(text, severity), markup=False)
        self.add_class("notice")
        self.add_class(f"notice-{severity}")

    @staticmethod
    def _render_notice(text: str, severity: str) -> Text:
        prefix = {"info": "·", "warning": "!", "error": "✕", "success": "✓"}.get(severity, "·")
        rendered = Text()
        rendered.append(f"{prefix} ", style="bold")
        rendered.append(text)
        return rendered


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
    ) -> None:
        super().__init__()
        self.data = dict(data)
        self.renderer = renderer
        self._expanded = bool(auto_expand)
        self._started = time.monotonic()
        self._finished: float | None = None
        self._spin_index = 0
        self._timer: Any = None
        self._body_text = ""
        self._header = Static(markup=False)
        self._header.add_class("tool-header")
        self._preview = Static("", markup=False)
        self._preview.add_class("tool-output")
        self._full = Static("", markup=False)
        self._full.add_class("tool-output-expanded")
        self.add_class("tool-card")
        self._refresh()

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._preview
        yield self._full

    def on_mount(self) -> None:
        if self._is_pending():
            self._timer = self.set_interval(0.1, self._tick)
        self._refresh()

    def on_unmount(self) -> None:
        self._stop_spinner()

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
        args = tool_args_preview(self.data)
        text = Text()
        if is_subagent_tool(self.data):
            text.append("│  ", style="dim")
        text.append(f"{symbol} ", style="bold" if not pending else "dim")
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
        rendered = generic
        payload = presentation_payload(self.data)
        if payload is not None:
            if self.renderer is None:
                fallback = payload.get("fallback")
                rendered = readable_text(fallback) if fallback else generic
            else:
                candidate = dict(payload)
                if not candidate.get("fallback"):
                    candidate["fallback"] = generic
                rendered = self.renderer.render(candidate, fallback=generic or "Activity")
        return rendered.strip()

    def _render_header(self) -> None:
        self._header.update(self._header_text())

    def _render_lines(self, lines: list[str], *, limit: int | None) -> Text:
        selected = lines if limit is None else lines[:limit]
        text = Text()
        for index, line in enumerate(selected):
            if index:
                text.append("\n")
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
        preview_source = self.data.get("result") or self.data.get("detail") or ""
        full_source = self.data.get("detail") or self.data.get("result") or ""
        raw_input = self.data.get("raw_input")
        if raw_input is not None:
            rendered_input = readable_text(raw_input, limit=UI_MAX_TOOL_CONTENT_CHARS)
            if rendered_input and rendered_input not in full_source:
                prefix = f"{full_source}\n\n" if full_source else ""
                full_source = f"{prefix}input\n{rendered_input}"
        preview_body = self._detail_text(preview_source)
        full_body = preview_body if full_source == preview_source else self._detail_text(full_source)
        self._body_text = preview_body or full_body

        preview_lines = preview_body.splitlines()
        full_lines = full_body.splitlines()
        self._preview.update(self._render_lines(preview_lines, limit=TOOL_PREVIEW_LINES))
        self._full.update(self._render_lines(full_lines, limit=None))

        preview_visible = bool(preview_lines) and not self._expanded and not self._is_pending()
        full_visible = bool(full_lines) and self._expanded
        self._preview.set_class(preview_visible, "visible")
        self._full.set_class(full_visible, "visible")

    def _refresh(self) -> None:
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

    def update_from(self, data: dict[str, Any]) -> None:
        was_pending = self._is_pending()
        self.data = dict(data)
        if was_pending and not self._is_pending():
            self._finished = max(0.0, time.monotonic() - self._started)
            self._stop_spinner()
        self._refresh()


class HintBar(Static):
    def show(self, text: str) -> None:
        self.update(Text(text, style="dim"))


class StatusBar(Static):
    """One-line session/connection/usage strip with explicit words."""

    def show(
        self,
        *,
        connected: bool,
        session_id: str | None,
        model: str = "",
        effort: str = "",
        mode: str = "",
        usage: str = "",
        busy: bool = False,
        detail: str = "",
        permission: str = "",
    ) -> None:
        text = Text()
        text.append("● " if connected else "○ ", style="bold" if connected else "dim")
        text.append("connected" if connected else "disconnected", style="bold" if connected else "dim")
        if session_id:
            text.append("   session ")
            text.append(session_id, style="bold")
        if mode:
            text.append(f"   mode {mode}")
        if permission:
            text.append("   approvals ")
            text.append(permission, style="bold" if permission != "ask" else "")
        if model:
            text.append(f"   model {model}")
        if effort:
            text.append(f"   effort {effort}")
        if usage:
            text.append(f"   {usage}")
        if busy:
            text.append("   working…", style="bold")
        if detail:
            text.append(f"   {detail}", style="dim")
        self.update(text)


class ApprovalDecided(Message):
    """The local user answered a pending tool approval."""

    def __init__(self, option_id: str | None) -> None:
        super().__init__()
        self.option_id = option_id


APPROVAL_LABELS: dict[str, tuple[str, str]] = {
    "allow_once": ("Yes, proceed", "y"),
    "allow_always": ("Yes, always allow this exact call", "a"),
    "reject_once": ("No, reject this call", "n"),
    "reject_always": ("No, always reject this exact call", "shift+n"),
}


def _approval_detail(request: PermissionRequest) -> Text:
    """Render the actual call, not the clipped JSON blob from the wire."""
    raw = request.raw_input
    lines: list[str] = []
    if isinstance(raw, Mapping):
        command = raw.get("command") or raw.get("cmd")
        if command:
            if isinstance(command, (list, tuple)):
                command = " ".join(str(part) for part in command)
            lines.append(f"$ {command}")
        path = raw.get("path") or raw.get("file_path") or raw.get("filename")
        if path and not command:
            lines.append(f"path: {path}")
        pattern = raw.get("pattern") or raw.get("query")
        if pattern and not command:
            lines.append(f"pattern: {pattern}")
        diff = raw.get("patch") or raw.get("diff")
        old = raw.get("old_string")
        new = raw.get("new_string")
        content = raw.get("content")
        if isinstance(diff, str) and diff.strip():
            lines.extend(diff.splitlines()[:10])
        elif isinstance(old, str) and isinstance(new, str):
            lines.extend(f"- {line}" for line in old.splitlines()[:5])
            lines.extend(f"+ {line}" for line in new.splitlines()[:5])
        elif isinstance(content, str) and content.strip():
            lines.extend(f"+ {line}" for line in content.splitlines()[:8])
        elif not lines:
            try:
                rendered = json.dumps(raw, indent=2, ensure_ascii=False, default=str)
            except Exception:
                rendered = str(raw)
            lines.extend(rendered.splitlines()[:10])
    elif raw is not None:
        lines.extend(str(raw).splitlines()[:10])
    if not lines:
        lines.extend((request.detail or "No additional tool details were provided.").splitlines()[:10])

    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        style = "green" if line.startswith("+ ") else "red" if line.startswith("- ") else ""
        text.append(readable_text(line, limit=240), style=style)
    return text


class ApprovalBar(Vertical):
    """Non-modal approval prompt above the composer.

    The transcript stays readable and the agent's tool card remains in place.
    Focus moves to the option list; the screen owns the y/a/n shortcuts so they
    cannot fire while the composer is focused.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._request: PermissionRequest | None = None
        self._header = Static("", markup=False)
        self._header.add_class("approval-header")
        self._detail = Static("", markup=False)
        self._detail.add_class("approval-detail")
        self._options = OptionList(id="approval-options", markup=False)
        self._hint = Static("", markup=False)
        self._hint.add_class("approval-hint")
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._detail
        yield self._options
        yield self._hint

    @property
    def pending(self) -> bool:
        return self._request is not None

    def show(self, request: PermissionRequest) -> None:
        self._request = request
        header = Text()
        header.append("Approval needed", style="bold")
        header.append("  ")
        header.append(readable_text(request.summary(), limit=200), style="bold")
        if request.kind:
            header.append(f"  ·  {readable_text(request.kind, limit=40)}", style="dim")
        self._header.update(header)
        self._detail.update(_approval_detail(request))
        self._options.clear_options()
        for option in request.options:
            kind = str(option.get("kind") or "")
            label, shortcut = APPROVAL_LABELS.get(
                kind, (str(option.get("name") or kind or "Option"), "")
            )
            suffix = f"   ({shortcut})" if shortcut else ""
            label = readable_text(label, limit=120)
            self._options.add_option(Option(f"{label}{suffix}", id=str(option.get("optionId") or "")))
        kinds = {str(option.get("kind") or "") for option in request.options}
        hints = ["↑/↓ choose"]
        if "allow_once" in kinds:
            hints.append("y allow once")
        if "allow_always" in kinds:
            hints.append("a always allow")
        if "reject_once" in kinds:
            hints.append("n reject")
        if "reject_always" in kinds:
            hints.append("shift+n always reject")
        hints.append("Esc stop and redirect")
        self._hint.update(" · ".join(hints))
        self.display = True
        if self._options.option_count:
            self._options.highlighted = 0
        self.call_after_refresh(self._options.focus)

    def hide(self) -> None:
        self._request = None
        self.display = False
        self._options.clear_options()

    def option_id_for_kind(self, kind: str) -> str | None:
        if self._request is None:
            return None
        option = self._request.first_of_kind(kind)
        option_id = str(option.get("optionId") or "") if option else ""
        return option_id or None

    def option_id_at(self, index: int) -> str | None:
        if self._request is None or not 0 <= index < len(self._request.options):
            return None
        option_id = str(self._request.options[index].get("optionId") or "")
        return option_id or None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.post_message(ApprovalDecided(str(event.option_id)))


# --------------------------------------------------------------- side panels


class SidePanel(TabbedContent):
    """Capabilities, sessions and activity. Hidden in narrow terminals."""

    def __init__(self) -> None:
        super().__init__(initial="capabilities", id="side-panel")
        self.capabilities = CapabilityPanel(id="capability-panel")
        self.sessions = SessionPanel(id="session-panel")
        self.activity = ActivityPanel(id="activity-panel")
        # TabbedContent.compose() builds its private ContentTabs from this list.
        # Overriding compose() here would omit ContentTabs entirely.
        self._tab_content.extend(
            [
                TabPane("Capabilities", self.capabilities, id="capabilities"),
                TabPane("Sessions", self.sessions, id="sessions"),
                TabPane("Activity", self.activity, id="activity"),
            ]
        )


class BindingToggleRequested(Message):
    def __init__(self, binding_id: str, enabled: bool) -> None:
        super().__init__()
        self.binding_id = binding_id
        self.enabled = enabled


class BindingEditRequested(Message):
    def __init__(self, binding_id: str) -> None:
        super().__init__()
        self.binding_id = binding_id


class ConfigOptionRequested(Message):
    def __init__(self, option_id: str) -> None:
        super().__init__()
        self.option_id = option_id


class ConfigOptionStepRequested(Message):
    """Move a standard ACP select option one value in a direction."""

    def __init__(self, option_id: str, direction: int) -> None:
        super().__init__()
        self.option_id = option_id
        self.direction = direction


class CapabilityRefreshRequested(Message):
    pass


class CapabilityAddRequested(Message):
    """The local user wants to add a registered plugin as a new binding."""


class CapabilityPanel(Vertical):
    """Selected vs active capability state, with honest locked reasons."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
        Binding("e", "edit", "Configure", show=False),
        Binding("space", "toggle", "Toggle", show=False),
        Binding("a", "add", "Add local capability", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.extension_mode = False
        self.can_write = False
        self._summary: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._options: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[str, Any] | None = None
        self._status = Static("Loading capabilities…", markup=False)
        self._status.add_class("panel-status")
        self._list = OptionList(id="binding-list", markup=False)
        self._detail = Static("Select a capability to inspect it.", markup=False)
        self._detail.add_class("panel-detail")
        self._hint = Static(
            "space or ←/→ toggles · e edits fields · a adds a local plugin · r refreshes",
            markup=False,
            classes="panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._list
        yield self._detail
        yield self._hint

    # ------------------------------------------------------------------ data

    def show_message(self, text: str) -> None:
        self._status.update(Text(text))

    def set_bindings(
        self,
        bindings: list[dict[str, Any]],
        snapshot: dict[str, Any] | None,
        *,
        active_revision: int | None = None,
        selected_revision: int | None = None,
        policy_revision: int | None = None,
        can_write: bool = True,
    ) -> None:
        self.extension_mode = True
        self.can_write = bool(can_write)
        self._snapshot = snapshot
        previous = self.current_binding_id()
        self._summary = {}
        self._order = []
        self._options = {}
        self._list.clear_options()
        for raw in bindings:
            summary = binding_summary(raw, snapshot)
            binding_id = summary["bindingId"]
            if not binding_id:
                continue
            self._summary[binding_id] = summary
            self._order.append(binding_id)
            self._options[binding_id] = raw
            self._list.add_option(Option(self._row_text(summary), id=binding_id))
        selected = selected_revision if selected_revision is not None else (snapshot or {}).get("selectedRevision")
        active = active_revision if active_revision is not None else (snapshot or {}).get("activeRevision")
        policy = policy_revision if policy_revision is not None else (snapshot or {}).get("policyRevision")
        revision = f"selected v{selected} · active v{active} · policy v{policy}"
        if not self.can_write:
            revision += " · read-only (update not advertised)"
        self._status.update(
            Text(f"{len(bindings)} capabilities · {revision}", style="dim")
        )
        self._hint.update(
            "select to inspect · this host does not advertise configuration updates"
            if not self.can_write
            else "space or ←/→ toggles · e edits fields · a adds a local plugin · r refreshes"
        )
        if self._list.option_count:
            index = self._highlight_index(previous)
            self._list.highlighted = index
            self._show_detail(self._order[index])
        else:
            self._detail.update("No capabilities offered by this agent.")

    def set_standard_options(self, options: list[dict[str, Any]]) -> None:
        self.extension_mode = False
        self.can_write = True
        previous = self.current_binding_id()
        self._summary = {}
        self._order = []
        self._options = {}
        self._list.clear_options()
        for option in options:
            option_id = str(option.get("id", ""))
            if not option_id:
                continue
            self._order.append(option_id)
            self._options[option_id] = option
            name = option.get("name") or option_id
            value = config_option_value(option)
            self._list.add_option(Option(f"{name}: <{value}>", id=option_id))
        self._status.update(
            Text(
                "Standard ACP configuration only. This agent does not advertise the richer "
                "capability extension.",
                style="dim",
            )
        )
        self._hint.update("←/→ changes values · Enter opens the full chooser · r refreshes")
        if self._list.option_count:
            index = self._highlight_index(previous)
            self._list.highlighted = index
            self._show_standard_detail(self._order[index])
        else:
            self._detail.update("This agent advertises no configuration options.")

    def _highlight_index(self, previous: str | None) -> int:
        """Keep the edited row selected instead of jumping back to the top."""
        if previous and previous in self._order:
            return self._order.index(previous)
        return 0

    def _row_text(self, summary: dict[str, Any]) -> str:
        enabled = "on" if summary["enabled"] else "off"
        bits = [f"{summary['name']}   <{enabled}>"]
        if summary["locked"]:
            bits.append("locked")
        elif summary.get("pending"):
            active = summary.get("state") or {}
            active_on = active.get("enabled") if isinstance(active, dict) else summary.get("activeEnabled")
            if active_on is not None and bool(active_on) != summary["enabled"]:
                bits.append("next turn")
        if summary["changeMode"] == "new_session":
            bits.append("new session")
        return "   ".join(bits)

    def _show_detail(self, binding_id: str) -> None:
        summary = self._summary.get(binding_id)
        if not summary:
            return
        lines: list[str] = []
        lines.append(summary["name"])
        lines.append(f"id {summary['bindingId']}" + (f" · plugin {summary['pluginId']}" if summary["pluginId"] else ""))
        if summary["description"]:
            lines.append("")
            lines.append(summary["description"])
        lines.append("")
        selected_state = "on" if summary["enabled"] else "off"
        active = summary.get("state") or {}
        active_enabled = active.get("enabled") if isinstance(active, dict) else summary.get("activeEnabled")
        active_text = "unknown" if active_enabled is None else ("on" if active_enabled else "off")
        lines.append(f"selected: {selected_state} · active: {active_text}")
        lines.append(f"change: {summary['modeLabel']} · state: {summary['stateMode']}")
        default_state = "on" if summary.get("enabledByDefault") else "off"
        if summary.get("defaults"):
            rendered_defaults = ", ".join(
                f"{key}={value}" for key, value in list(summary["defaults"].items())[:8]
            )
            lines.append(f"default: {default_state} · {rendered_defaults}")
        else:
            lines.append(f"default: {default_state}")
        if summary["locked"]:
            lines.append(f"locked: {summary['reason'] or 'not available in this deployment'}")
        else:
            permissions = []
            permissions.append("enable allowed" if summary["canEnable"] else "enable locked")
            permissions.append("disable allowed" if summary["canDisable"] else "disable locked")
            lines.append(" · ".join(permissions))
        if summary["requires"]:
            lines.append(f"requires: {', '.join(summary['requires'])}")
        if summary["conflicts"]:
            lines.append(f"conflicts: {', '.join(summary['conflicts'])}")
        if summary["editableFields"]:
            lines.append(f"editable: {', '.join(summary['editableFields'])}")
        pending = pending_application_text(summary, self._snapshot)
        if pending:
            lines.append("")
            lines.append(pending)
        if summary["config"]:
            lines.append("")
            lines.append("configuration")
            for key, value in list(summary["config"].items())[:12]:
                lines.append(f"  {key}: {value}")
        self._detail.update("\n".join(lines))

    def _show_standard_detail(self, option_id: str) -> None:
        option = self._options.get(option_id) or {}
        lines = [
            str(option.get("name") or option_id),
            f"id {option_id} · type {option.get('type', '?')}",
            "",
            str(option.get("description") or "No description supplied."),
        ]
        if option.get("category"):
            lines.append(f"category: {option['category']}")
        current = config_option_value(option)
        lines.append("")
        lines.append(f"current: {current}")
        self._detail.update("\n".join(lines))

    # --------------------------------------------------------------- events

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id is None:
            return
        if self.extension_mode:
            self._show_detail(event.option_id)
        else:
            self._show_standard_detail(event.option_id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id
        if option_id is None:
            return
        if not self.extension_mode:
            self.post_message(ConfigOptionRequested(option_id))
            return
        self._toggle_binding(option_id)

    def on_key(self, event: Any) -> None:
        """Left/Right change the highlighted capability or standard option."""
        direction = choice_direction(str(event.key))
        if direction is None or not self._list.has_focus:
            return
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        event.stop()
        if self.extension_mode:
            self._toggle_binding(binding_id)
            return
        option = self._options.get(binding_id) or {}
        if option.get("type") == "boolean":
            self.post_message(ConfigOptionRequested(binding_id))
        else:
            self.post_message(ConfigOptionStepRequested(binding_id, direction))

    def _toggle_binding(self, binding_id: str) -> None:
        summary = self._summary.get(binding_id)
        if summary is None:
            return
        if not self.can_write:
            self._show_detail(binding_id)
            self.show_message("This host advertises a read-only capability catalog.")
            return
        if summary["locked"]:
            self.show_message(f"{summary['name']} is locked: {summary['reason'] or 'unavailable'}")
            return
        self.post_message(BindingToggleRequested(binding_id, not summary["enabled"]))

    def action_refresh(self) -> None:
        self.post_message(CapabilityRefreshRequested())

    def action_edit(self) -> None:
        if self.extension_mode and not self.can_write:
            self.show_message("This host advertises a read-only capability catalog.")
            return
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        summary = self._summary.get(binding_id)
        if summary and summary["editableFields"]:
            self.post_message(BindingEditRequested(binding_id))
        elif summary:
            self.show_message(f"{summary['name']} has no editable fields.")

    def action_add(self) -> None:
        if self.extension_mode and not self.can_write:
            self.show_message("This host advertises a read-only capability catalog.")
            return
        self.post_message(CapabilityAddRequested())

    def action_toggle(self) -> None:
        if self.extension_mode and not self.can_write:
            self.show_message("This host advertises a read-only capability catalog.")
            return
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        if not self.extension_mode:
            self.post_message(ConfigOptionRequested(binding_id))
            return
        self._toggle_binding(binding_id)

    def current_binding_id(self) -> str | None:
        index = self._list.highlighted
        if index is None or index < 0 or index >= len(self._order):
            return None
        return self._order[index]

    def selected_option(self) -> dict[str, Any] | None:
        binding_id = self.current_binding_id()
        return self._options.get(binding_id) if binding_id else None

    def binding(self, binding_id: str) -> dict[str, Any] | None:
        return self._summary.get(binding_id)

    def option(self, option_id: str) -> dict[str, Any] | None:
        return self._options.get(option_id)


class SessionLoadRequested(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionResumeRequested(Message):
    """Resume a session without asking the agent to replay its history."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionDeleteRequested(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionCloseRequested(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionNewRequested(Message):
    pass


class SessionRefreshRequested(Message):
    pass


class SessionMoreRequested(Message):
    pass


class SessionPanel(Vertical):
    """Advertised session list with explicit load/close/delete actions."""

    can_focus = True
    BINDINGS = [
        Binding("d", "delete_session", "Delete", show=False),
        Binding("x", "close_session", "Close", show=False),
        Binding("u", "resume_session", "Resume", show=False),
        Binding("n", "new_session", "New", show=False),
        Binding("m", "more_sessions", "More", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ids: dict[str, str] = {}
        self._cwds: dict[str, str] = {}
        self._status = Static("Sessions load when you open this panel.", markup=False, classes="panel-status")
        self._table = DataTable(id="session-table", cursor_type="row", zebra_stripes=False)
        self._hint = Static(
            "u resumes · d deletes · x closes · n new session · r refreshes",
            markup=False,
            classes="panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._table
        yield self._hint

    def on_mount(self) -> None:
        self._table.add_columns("title", "updated", "workspace")
        self._table.cursor_type = "row"

    def show_message(self, text: str) -> None:
        self._status.update(Text(text))

    def set_sessions(
        self,
        sessions: list[dict[str, Any]],
        *,
        can_delete: bool,
        can_close: bool,
        can_resume: bool = False,
        can_load: bool = True,
        next_cursor: str | None = None,
    ) -> None:
        self._table.clear()
        self._ids.clear()
        self._cwds.clear()
        for item in sessions:
            session_id = str(item.get("sessionId") or item.get("session_id") or "")
            if not session_id:
                continue
            title = str(item.get("title") or "(untitled)")
            updated = str(item.get("updatedAt") or item.get("updated_at") or "")
            cwd = str(item.get("cwd") or "")
            row_key = self._table.add_row(title, updated, cwd, key=session_id)
            self._ids[str(row_key.value)] = session_id
            self._cwds[session_id] = cwd
        delete_note = "" if can_delete else " · delete not advertised"
        close_note = "" if can_close else " · close not advertised"
        resume_note = "" if can_resume else " · resume not advertised"
        more = " · m loads more pages" if next_cursor else ""
        self._status.update(
            Text(
                f"{len(self._ids)} sessions{more}{delete_note}{close_note}{resume_note}",
                style="dim",
            )
        )
        actions = [] if can_load else ["load unavailable"]
        if can_resume:
            actions.append("u resumes")
        if can_delete:
            actions.append("d deletes")
        if can_close:
            actions.append("x closes")
        if next_cursor:
            actions.append("m more")
        actions.extend(("n new session", "r refreshes"))
        self._hint.update(Text(" · ".join(actions), style="dim"))

    def selected_session_id(self) -> str | None:
        if self._table.row_count == 0:
            return None
        try:
            row_key, _ = self._table.coordinate_to_cell_key(self._table.cursor_coordinate)
        except Exception:
            return None
        return self._ids.get(str(row_key.value))

    def cwd_for(self, session_id: str) -> str | None:
        return self._cwds.get(session_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        session_id = self._ids.get(str(event.row_key.value))
        if session_id:
            self.post_message(SessionLoadRequested(session_id))

    def _selected(self) -> str | None:
        session_id = self.selected_session_id()
        if session_id is None:
            self.show_message("Select a session row first.")
        return session_id

    def action_delete_session(self) -> None:
        session_id = self._selected()
        if session_id:
            self.post_message(SessionDeleteRequested(session_id))

    def action_close_session(self) -> None:
        session_id = self._selected()
        if session_id:
            self.post_message(SessionCloseRequested(session_id))

    def action_resume_session(self) -> None:
        session_id = self._selected()
        if session_id:
            self.post_message(SessionResumeRequested(session_id))

    def action_new_session(self) -> None:
        self.post_message(SessionNewRequested())

    def action_more_sessions(self) -> None:
        self.post_message(SessionMoreRequested())

    def action_refresh(self) -> None:
        self.post_message(SessionRefreshRequested())


class ActivityPanel(VerticalScroll):
    """Plan, delegated work, commands, usage and connection details in one scroll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connection = Static("Not connected.", markup=False, classes="activity-section")
        self._plan = Static("No plan reported yet.", markup=False, classes="activity-section")
        self._subagents = Static(
            "No subagent activity reported.", markup=False, classes="activity-section"
        )
        self._commands = Static("No agent commands advertised.", markup=False, classes="activity-section")
        self._usage = Static("No usage reported.", markup=False, classes="activity-section")
        self._session = Static("", markup=False, classes="activity-section")
        self._subagent_state: dict[str, dict[str, str]] = {}

    def compose(self) -> ComposeResult:
        yield Label("Connection", classes="section-title")
        yield self._connection
        yield Label("Plan", classes="section-title")
        yield self._plan
        yield Label("Subagents", classes="section-title")
        yield self._subagents
        yield Label("Agent commands", classes="section-title")
        yield self._commands
        yield Label("Usage", classes="section-title")
        yield self._usage
        yield Label("Session", classes="section-title")
        yield self._session

    def set_connection(self, lines: list[str]) -> None:
        self._connection.update("\n".join(lines))

    def set_plan(self, entries: tuple[tuple[str, str, str], ...], *, cleared: bool = False) -> None:
        if cleared or not entries:
            self._plan.update("No plan reported yet.")
            return
        rendered = []
        for content, priority, status in entries[:50]:
            mark = {"pending": "○", "in_progress": "◐", "completed": "●"}.get(status, "○")
            rendered.append(f"{mark} {content}   ({priority})")
        self._plan.update("\n".join(rendered))

    def note_tool(self, data: dict[str, Any]) -> None:
        """Show explicitly-named delegation tool calls as subagent activity."""
        tool_id = str(data.get("toolCallId") or data.get("tool_call_id") or "")
        existing = self._subagent_state.get(tool_id)
        if existing is None and not is_subagent_tool(data):
            return
        if not tool_id:
            tool_id = str(data.get("title") or "delegated task")
        record = dict(existing or {})
        title = str(data.get("title") or record.get("title") or tool_id)
        record["title"] = title
        record["status"] = str(data.get("status") or record.get("status") or "pending")
        self._subagent_state[tool_id] = record
        rendered = []
        for item in list(self._subagent_state.values())[-20:]:
            mark = {
                "pending": "○",
                "in_progress": "◐",
                "completed": "●",
                "failed": "✕",
            }.get(item["status"], "○")
            rendered.append(f"{mark} {item['title']}   ({item['status']})")
        self._subagents.update("\n".join(rendered))

    def clear_subagents(self) -> None:
        self._subagent_state.clear()
        self._subagents.update("No subagent activity reported.")

    def subagent_lines(self) -> list[str]:
        """Current delegated-work lines, for the read-only subagent overview."""
        rendered = []
        for item in list(self._subagent_state.values())[-20:]:
            mark = {
                "pending": "○",
                "in_progress": "◐",
                "completed": "●",
                "failed": "✕",
            }.get(item["status"], "○")
            rendered.append(f"{mark} {item['title']}   ({item['status']})")
        return rendered

    def set_commands(self, commands: tuple[dict[str, Any], ...]) -> None:
        if not commands:
            self._commands.update("No agent commands advertised.")
            return
        rendered = []
        for command in commands[:100]:
            name = command.get("name", "")
            description = command.get("description", "")
            rendered.append(f"/{name} — {description}" if description else f"/{name}")
        self._commands.update("\n".join(rendered))

    def set_usage(self, summary: dict[str, Any] | None) -> None:
        if not summary:
            self._usage.update("No usage reported.")
            return
        rendered = [
            f"{key}: {value}" for key, value in summary.items() if key not in {"_meta", "cost"} and not isinstance(value, dict)
        ]
        cost = summary.get("cost")
        if isinstance(cost, dict):
            rendered.append(f"cost: {cost.get('amount')} {cost.get('currency', '')}".strip())
        self._usage.update("\n".join(rendered) or "No usage reported.")

    def set_session(self, lines: list[str]) -> None:
        self._session.update("\n".join(lines))
