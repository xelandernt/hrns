"""Terminal widgets: transcript, composer, status, capability/session/activity panels."""

from __future__ import annotations

import contextlib
import json
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, NamedTuple

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Offset
from textual.message import Message
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Input,
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
    merge_presentation,
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
    "CommandResultBlock",
    "CommandSuggestionKey",
    "CommandSuggestions",
    "Composer",
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
    return "…" + text[-(MAX_STREAM_MESSAGE_CHARS - 1) :]


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
        self.prompt_history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""
        self._history_text = ""

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
        row, _ = self.cursor_location
        if (
            not self.suggestions_open
            and self.selection.is_empty
            and (
                (event.key == "up" and row == 0)
                or (event.key == "down" and row == self.document.line_count - 1)
            )
            and self.prompt_history
        ):
            if self._history_index is not None and self.text != self._history_text:
                self._history_index = None
            if event.key == "up":
                if self._history_index is None:
                    self._history_draft = self.text
                    self._history_index = len(self.prompt_history)
                self._history_index = max(0, self._history_index - 1)
            elif self._history_index is not None:
                self._history_index += 1
                if self._history_index >= len(self.prompt_history):
                    self._history_index = None
            else:
                return
            self._history_text = (
                self._history_draft
                if self._history_index is None
                else self.prompt_history[self._history_index]
            )
            self.load_text(self._history_text)
            self.move_cursor(self.document.end)
            event.prevent_default()
            event.stop()
        # Let TextArea's own ``_on_key`` handle printable input and navigation.

    def on_blur(self) -> None:
        if self.suggestions_open:
            self.post_message(CommandSuggestionKey("escape"))

    def action_submit(self) -> None:
        text = self.text.strip()
        if text:
            self.post_message(Submitted(text))

    def clear_composer(self) -> None:
        self._history_index = None
        self.load_text("")


class QueueAction(Message):
    def __init__(self, action: str, index: int = 0) -> None:
        super().__init__()
        self.action = action
        self.index = index


class AttachmentRemoved(Message):
    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


class AttachmentBar(OptionList):
    """Persistent attachment metadata; selecting a file removes it from the draft."""

    DEFAULT_CSS = """
    AttachmentBar { height: auto; max-height: 5; margin: 0 2; background: $surface; }
    """

    def show_files(self, summaries: list[dict[str, Any]]) -> None:
        self.display = bool(summaries)
        self.clear_options()
        for index, item in enumerate(summaries):
            size = item["size"]
            size_text = f"{size:,} bytes" if size is not None else "unavailable"
            self.add_option(
                Option(
                    f"× {item['name']} · {size_text} · {item['mimeType']} · select to remove",
                    id=str(index),
                )
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_id is not None:
            self.post_message(AttachmentRemoved(int(event.option_id)))


class PromptQueue(Vertical):
    DEFAULT_CSS = """
    PromptQueue { height: auto; max-height: 10; padding: 0 2; }
    PromptQueue OptionList { height: auto; max-height: 5; background: $surface; }
    PromptQueue Button { height: 1; min-width: 12; border: none; margin: 0 1 0 0; }
    PromptQueue Horizontal { height: 1; }
    """

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        yield Label("Queued follow-ups", id="queue-label")
        yield OptionList(id="queue-items", markup=False)
        with Horizontal():
            yield Button("Edit", id="queue-edit")
            yield Button("Remove", id="queue-remove")
            yield Button("Resume queue", id="queue-resume")

    def show_prompts(self, prompts: list[tuple[str, list[str]]], *, paused: bool) -> None:
        self.display = bool(prompts)
        choices = self.query_one(OptionList)
        selected = choices.highlighted or 0
        choices.clear_options()
        for index, (text, attachments) in enumerate(prompts):
            suffix = f" · {len(attachments)} files" if attachments else ""
            choices.add_option(Option(f"{index + 1}. {' '.join(text.split())[:100]}{suffix}"))
        if prompts:
            choices.highlighted = min(selected, len(prompts) - 1)
        self.query_one("#queue-label", Label).update(
            "Queued follow-ups · paused" if paused else "Queued follow-ups"
        )
        self.query_one("#queue-resume").display = paused

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = (event.button.id or "").removeprefix("queue-")
        self.post_message(QueueAction(action, self.query_one(OptionList).highlighted or 0))


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
        text.append(
            "connected" if connected else "disconnected", style="bold" if connected else "dim"
        )
        if session_id:
            text.append("   session ", style="dim")
            text.append(session_id)
        text.append("   ", style="dim")
        text.append(extension_text, style="dim")
        self.update(text)


class OutputPending(Message):
    def __init__(self, pending: bool) -> None:
        super().__init__()
        self.pending = pending


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
        # Supplemental snapshots can arrive before the standard ACP tool-call start.
        self._pending_presentations: dict[str, Any] = {}
        self._stream: "MessageBlock | None" = None
        self._stream_buffer = ""
        self._thought: "ReasoningBlock | None" = None
        self._thought_buffer = ""
        self._dirty = False
        self._replay = False
        self._replay_entries: list[tuple[str, Any]] = []
        self.auto_expand_tools = False
        #: Session-scoped context for explicit plugin-widget actions.
        self.view_context: Any = None
        #: Expand reasoning text (the Thinking/Thought row appears either way).
        self.show_thoughts = False
        self._ticker: Any = None
        self._following = True
        self._navigation_block: Any = None
        self.renderers = RendererRegistry()
        try:
            self.renderers.discover()
        except Exception:
            # A broken third-party entry point must leave generic rendering usable.
            pass
        from .tool_views import register_builtin_renderers

        register_builtin_renderers(self.renderers)

    def on_mount(self) -> None:
        self._ticker = self.set_interval(0.06, self.flush)
        self.anchor()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value < old_value:
            self._following = False
        if self.is_vertical_scroll_end:
            if not self._following:
                self.anchor()
            self._following = True
            self.post_message(OutputPending(False))

    def _output_updated(self) -> None:
        if not self._following:
            self.post_message(OutputPending(True))

    def jump_to_latest(self) -> None:
        if self._navigation_block is not None:
            self._navigation_block.remove_class("navigation-target")
        self._navigation_block = None
        self._following = True
        self.anchor()
        self.post_message(OutputPending(False))

    def jump_user(self, direction: int) -> None:
        messages = [
            block
            for block in self._blocks
            if isinstance(block, MessageBlock) and block.role == "you"
        ]
        if not messages:
            return
        if self._navigation_block in messages:
            index = messages.index(self._navigation_block) + direction
        else:
            visible = [
                i for i, block in enumerate(messages) if block.virtual_region.y <= self.scroll_y + 1
            ]
            index = (visible[-1] if visible else 0) + (1 if direction > 0 else -1)
            if self.is_vertical_scroll_end and direction < 0:
                index = len(messages) - 1
        block = messages[max(0, min(index, len(messages) - 1))]
        self.reveal(block)

    def reveal(self, block: Any) -> None:
        if self._navigation_block is not None:
            self._navigation_block.remove_class("navigation-target")
        block.add_class("navigation-target")
        if isinstance(block, ToolCard):
            block._expanded = True
            block._refresh()
        elif isinstance(block, ReasoningBlock):
            block.collapsed = False
        self._following = False
        self.anchor(False)
        self._navigation_block = block
        self.scroll_to_widget(block, top=True, animate=False)
        self.focus()

    def search_entries(self) -> list[tuple[Any, str, str]]:
        entries: list[tuple[Any, str, str]] = []
        for block in self._blocks:
            if isinstance(block, MessageBlock):
                entries.append((block, block.role, block._text))
            elif isinstance(block, ToolCard):
                text = f"{block.data.get('title', 'Tool')}\n{block.data.get('detail') or block._body_text}"
                entries.append((block, "tool", text))
            elif isinstance(block, ReasoningBlock):
                entries.append((block, "reasoning", block._text))
            elif isinstance(block, CommandResultBlock):
                entries.append((block, "command", block._text))
        return entries

    def latest_response(self) -> MessageBlock | None:
        return next(
            (
                block
                for block in reversed(self._blocks)
                if isinstance(block, MessageBlock)
                and block.role == "agent"
                and block is not self._stream
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
        card = self._tools.get(tool_call_id)
        return dict(card.data) if card is not None else None

    # ------------------------------------------------------------- mounting

    async def _mount_block(self, block: Any) -> None:
        self._blocks.append(block)
        await self.mount(block)
        while len(self._blocks) > UI_MAX_TRANSCRIPT_BLOCKS:
            oldest = self._blocks.popleft()
            if isinstance(oldest, ToolCard):
                # Evicted cards must not linger in the update registry.
                self._tools.pop(oldest.tool_call_id, None)
                self._pending_presentations.pop(oldest.tool_call_id, None)
            if oldest is self._stream or oldest is self._thought:
                self._blocks.append(oldest)
                break
            await oldest.remove()
        self._output_updated()

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
        await self._mount_block(CommandResultBlock(data, view=view))

    def command_blocks(self) -> list[CommandResultBlock]:
        return [block for block in self._blocks if isinstance(block, CommandResultBlock)]

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
            self._output_updated()
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
        self._output_updated()

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
        snapshot = self._pending_presentations.pop(merged["toolCallId"], None)
        if snapshot is not None:
            merged = merge_presentation(merged, snapshot)
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
        self._tools[data.get("toolCallId", "")] = card
        await self._mount_block(card)

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
        card = self._tools.get(tool_id)
        if card is not None:
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
        self._stream_buffer = ""
        self._thought_buffer = ""
        self._following = True
        self._navigation_block = None
        self.anchor()
        self.post_message(OutputPending(False))
        self._tools.clear()
        self._pending_presentations.clear()
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

    def retained_tool_ids(self) -> list[str]:
        """Tool-call ids retained in the transcript, for passive presentation replay."""
        ids: list[str] = []
        for block in self._blocks:
            if isinstance(block, ToolCard) and block.tool_call_id:
                ids.append(block.tool_call_id)
        return ids


class MessageBlock(Vertical):
    """One message: a small role label and streaming Markdown body.

    Retained text is capped at ``MAX_STREAM_MESSAGE_CHARS``; older characters
    are dropped from the head and counted so the trim stays visible.
    """

    ROLE_LABELS = {"you": "You", "agent": "Assistant"}
    REVEAL_DURATION = 0.35
    DEFAULT_CSS = """
    MessageBlock .message-actions {
        height: 1;
        align: right middle;
    }
    MessageBlock .message-actions Button {
        height: 1; min-width: 0; padding: 0 1; border: none; margin: 0 0 0 1;
        color: $text-muted; background: transparent;
    }
    MessageBlock .message-actions Button:hover {
        color: $accent; background: $boost;
    }
    """

    def __init__(self, role: str, text: str = "") -> None:
        super().__init__()
        self.role = role
        self._text = text
        self._dropped = 0
        self._marked = False
        self._finished = False
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
        if self.role == "agent":
            from textual.containers import Horizontal

            with Horizontal(classes="message-actions"):
                yield Button("Copy response", id="copy-response", disabled=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "copy-response":
            self.app.copy_to_clipboard(self._text)
            self.app.notify("Response copied")

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
        if self._finished:
            for button in self.query(Button):
                button.disabled = False
            return
        # The action is revealed when the reply ends, not while it streams.
        for button in self.query(Button):
            button.display = False

    def finish(self) -> None:
        self.flush()
        if self._finished:
            return
        self._finished = True
        if not self.is_mounted:
            return
        for button in self.query(Button):
            button.disabled = False
            self._reveal(button)

    def _reveal(self, button: Button) -> None:
        """Fade the action in from the right once the reply is complete."""
        button.styles.opacity = 0.0
        button.styles.offset = Offset(2, 0)
        button.display = True
        button.styles.animate("opacity", 1.0, duration=self.REVEAL_DURATION, easing="out_cubic")
        button.animate("offset", Offset(0, 0), duration=self.REVEAL_DURATION, easing="out_cubic")


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
        self.title = title  # type: ignore[assignment]  # Textual accepts rich Content at runtime.

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
        args = tool_args_preview(self.data)
        text = Text()
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
        self._full.update(self._render_lines(full_lines, limit=None))

        preview_visible = (
            bool(preview_lines) and not self._expanded and not self._is_pending() and not custom
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

    def update_from(self, data: dict[str, Any]) -> None:
        was_pending = self._is_pending()
        self.data = dict(data)
        if was_pending and not self._is_pending():
            self._finished = max(0.0, time.monotonic() - self._started)
            self._stop_spinner()
        self._refresh()


def _parse_color(value: str | None) -> Color | None:
    if not value:
        return None
    try:
        return Color.parse(value)
    except Exception:  # a theme may carry a colour name Rich does not know
        return None


class _Palette(NamedTuple):
    accent: Color | None = None
    foreground: Color | None = None
    muted: Color | None = None
    warning: Color | None = None
    error: Color | None = None


def _palette(widget: Static) -> _Palette:
    """Theme colours the band needs, tolerant of partial or ANSI themes."""
    try:
        theme = widget.app.current_theme
    except Exception:
        return _Palette()
    variables = theme.variables or {}
    return _Palette(
        accent=_parse_color(theme.accent or theme.primary),
        foreground=_parse_color(theme.foreground),
        muted=_parse_color(variables.get("text-muted")),
        warning=_parse_color(theme.warning),
        error=_parse_color(theme.error),
    )


def _mix(start: Color, end: Color, weight: float) -> Color:
    weight = max(0.0, min(1.0, weight))
    left = start.get_truecolor()
    right = end.get_truecolor()
    return Color.from_rgb(
        round(left.red + (right.red - left.red) * weight),
        round(left.green + (right.green - left.green) * weight),
        round(left.blue + (right.blue - left.blue) * weight),
    )


def _clip(text: Text, width: int) -> Text:
    if width <= 0:
        return Text()
    if len(text) <= width:
        return text
    head = text[: width - 1].copy() if width > 1 else Text()
    head.append("…", style="dim")
    return head


class StatusBar(Static):
    """One line of chrome: live activity on the left, agent state on the right.

    The right cluster is pinned to the edge, so it never shifts while the
    activity label changes width. While a turn runs the label carries a slow
    gradient sweep instead of a spinner, and the timer driving it only exists
    while something is actually happening.
    """

    SWEEP_INTERVAL = 0.09
    SWEEP_SPREAD = 8

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("markup", False)
        super().__init__("", **kwargs)
        self._connected = True
        self._model = ""
        self._effort = ""
        self._mode = ""
        self._usage = ""
        self._permission = ""
        self._activity = ""
        self._hint = ""
        self._busy = False
        self._phase = 0
        self._timer: Any = None

    def show(
        self,
        *,
        connected: bool,
        model: str = "",
        effort: str = "",
        mode: str = "",
        usage: str = "",
        busy: bool = False,
        activity: str = "",
        permission: str = "",
        hint: str = "",
    ) -> None:
        self._connected = connected
        self._model = model
        self._effort = effort
        self._mode = mode
        self._usage = usage
        self._permission = permission
        self._activity = activity
        self._hint = hint
        self._set_busy(busy)
        self.refresh()

    def _set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._phase = 0
            self._start_timer()
        else:
            self._stop_timer()

    def _start_timer(self) -> None:
        if self._timer is None and self.is_mounted:
            self._timer = self.set_interval(self.SWEEP_INTERVAL, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._phase += 1
        self.refresh()

    def on_mount(self) -> None:
        if self._busy:
            self._start_timer()

    def on_unmount(self) -> None:
        self._stop_timer()

    # ---------------------------------------------------------------- render

    def render(self) -> Text:
        width = self.size.width  # content width: padding is already excluded
        if width <= 0:
            return Text()
        # Mid-turn the activity label is the point of the band, so it keeps a
        # reserved slice of the line and the right cluster gives way instead.
        reserve = min(len(self._activity or "working") + 2, 26) if self._busy else 0
        right = self._right(max(0, width - reserve))
        budget = width - len(right) - 2
        left = self._left(budget) if budget >= 8 else Text()
        left = _clip(left, budget)
        gap = " " * max(2, width - len(left) - len(right))
        return left + Text(gap) + right

    def _left(self, budget: int) -> Text:
        if not self._busy:
            return Text(self._hint, style="dim")
        label = self._activity or "working"
        text = self._sweep(label)
        # The hint is an extra, never a truncated fragment; it appears only when
        # the whole phrase fits beside the activity label.
        if self._hint and len(label) + len(self._hint) + 3 <= budget:
            text.append(f"   {self._hint}", style="dim")
        return text

    def _right(self, budget: int) -> Text:
        palette = _palette(self)
        model = (
            Text(self._model, style=Style(color=palette.foreground or palette.accent, bold=True))
            if self._model
            else None
        )
        mode = Text(self._mode) if self._mode else None
        effort = Text(self._effort) if self._effort else None
        permission = (
            Text(self._permission, style=Style(color=palette.warning, bold=True))
            if self._permission and self._permission != "ask"
            else None
        )
        usage = Text(self._usage, style="dim") if self._usage else None
        connection = (
            None
            if self._connected
            else Text("○ disconnected", style=Style(color=palette.error, bold=True))
        )
        fields: list[tuple[str, Text | None]] = [
            ("model", model),
            ("mode", mode),
            ("effort", effort),
            ("permission", permission),
            ("usage", usage),
            ("connection", connection),
        ]

        def assemble() -> Text:
            identity = Text(" · ", style="dim").join(
                [piece for _, piece in fields[:3] if piece is not None]
            )
            text = Text()
            for piece in [identity, *(piece for _, piece in fields[3:])]:
                if piece is None or not piece.plain:
                    continue
                if text.plain:
                    text.append("   ")
                text += piece
            return text

        # Ambiguity is resolved by dropping the least load-bearing field first;
        # the model name is the last thing to go, and only in a very narrow band.
        for name in ("usage", "permission", "effort", "mode", "model"):
            if len(assemble()) <= budget:
                break
            fields = [(label, None if label == name else piece) for label, piece in fields]
        return _clip(assemble(), budget)

    def _sweep(self, label: str) -> Text:
        palette = _palette(self)
        if palette.accent is None:
            return Text(label, style="bold")
        # The label stays readable at rest; the sweep is a travelling accent,
        # not a fade from invisible to visible.
        base = palette.muted or palette.foreground or palette.accent
        cycle = len(label) + self.SWEEP_SPREAD
        head = (self._phase % cycle) - self.SWEEP_SPREAD
        text = Text()
        for index, char in enumerate(label):
            weight = max(0.0, 1.0 - abs(index - head) / self.SWEEP_SPREAD) ** 1.6
            text.append(
                char, style=Style(color=_mix(base, palette.accent, weight), bold=weight > 0.8)
            )
        return text


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
        old = raw.get("old_string") or raw.get("old_text")
        new = raw.get("new_string") or raw.get("new_text")
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
        lines.extend(
            (request.detail or "No additional tool details were provided.").splitlines()[:10]
        )

    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        style = "green" if line.startswith("+ ") else "red" if line.startswith("- ") else ""
        text.append(readable_text(line, limit=240), style=style)
    return text


def _approval_view_state(request: PermissionRequest, data: Mapping[str, Any]) -> Any:
    """The approval's own identity, plus whatever the tool call already published.

    Presentation data is a snapshot of a call that may not have run yet, so anything the
    payload cannot answer is left to the request: the arguments under review, the title and
    the readable detail are all on ``PermissionRequest``.
    """
    base = _tool_view_state(data, live=True)
    return replace(
        base,
        session_id=request.session_id or base.session_id,
        tool_call_id=request.tool_call_id or base.tool_call_id,
        title=request.title or base.title,
        kind=request.kind or base.kind,
        status=request.status or base.status or "pending",
        raw_input=request.raw_input if request.raw_input is not None else base.raw_input,
        fallback=base.fallback or request.detail,
    )


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
        self._view_host = Vertical(classes="approval-view")
        self._view: Any = None
        self._options = OptionList(id="approval-options", markup=False)
        self._hint = Static("", markup=False)
        self._hint.add_class("approval-hint")
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._detail
        yield self._view_host
        yield self._options
        yield self._hint

    @property
    def pending(self) -> bool:
        return self._request is not None

    def show(
        self,
        request: PermissionRequest,
        *,
        data: Mapping[str, Any] | None = None,
        renderer: RendererRegistry | None = None,
        view_context: Any = None,
    ) -> None:
        """Show one request, preferring a local approval view over the generic detail.

        ``data`` is the tool call the request belongs to, when the transcript still holds
        it. Without a payload there is nothing for a provider to render, so the readable
        fallback stays in place.
        """
        self._clear_view()
        self._request = request
        header = Text()
        header.append("Approval needed", style="bold")
        header.append("  ")
        header.append(readable_text(request.summary(), limit=200), style="bold")
        if request.kind:
            header.append(f"  ·  {readable_text(request.kind, limit=40)}", style="dim")
        self._header.update(header)
        self._detail.update(_approval_detail(request))
        self._build_view(request, data or {}, renderer, view_context)
        self._options.clear_options()
        for option in request.options:
            kind = str(option.get("kind") or "")
            label, shortcut = APPROVAL_LABELS.get(
                kind, (str(option.get("name") or kind or "Option"), "")
            )
            suffix = f"   ({shortcut})" if shortcut else ""
            label = readable_text(label, limit=120)
            self._options.add_option(
                Option(f"{label}{suffix}", id=str(option.get("optionId") or ""))
            )
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

    def _build_view(
        self,
        request: PermissionRequest,
        data: Mapping[str, Any],
        renderer: RendererRegistry | None,
        view_context: Any,
    ) -> None:
        from .extensions import build_view

        if renderer is None or view_context is None:
            return
        payload = _presentation_of(data)
        if payload is None:
            return
        view = build_view(
            renderer,
            payload,
            "approval",
            _approval_view_state(request, data),
            view_context,
        )
        if view is None:
            return
        self._view = view
        self._detail.display = False
        if self.is_mounted and not view.widget.is_mounted:
            self._view_host.mount(view.widget)

    def _clear_view(self) -> None:
        view, self._view = self._view, None
        self._detail.display = True
        if view is not None and view.widget.is_mounted:
            with contextlib.suppress(Exception):
                view.widget.remove()

    def hide(self) -> None:
        self._request = None
        self.display = False
        self._options.clear_options()
        self._clear_view()

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
    """Plugins, sessions and activity. Hidden in narrow terminals."""

    PANE_ORDER: tuple[str, ...] = ("capabilities", "sessions", "activity")

    BINDINGS = [
        Binding("tab", "next_pane", "Next panel", show=False),
        Binding("shift+tab", "previous_pane", "Previous panel", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(initial="capabilities", id="side-panel")
        self.capabilities = CapabilityPanel(id="capability-panel")
        self.sessions = SessionPanel(id="session-panel")
        self.activity = ActivityPanel(id="activity-panel")
        # TabbedContent.compose() builds its private ContentTabs from this list.
        # Overriding compose() here would omit ContentTabs entirely.
        self._tab_content.extend(
            [
                TabPane("Plugins", self.capabilities, id="capabilities"),
                TabPane("Sessions", self.sessions, id="sessions"),
                TabPane("Activity", self.activity, id="activity"),
            ]
        )

    def activate(self, tab: str) -> None:
        """Show ``tab`` and move focus into its content."""
        self.active = tab
        # The pane is only focusable once the switch has been laid out.
        self.call_after_refresh(self._focus_pane, tab)

    def _focus_pane(self, tab: str) -> None:
        if tab == "capabilities":
            self.capabilities._list.focus()
        elif tab == "sessions":
            self.sessions._table.focus()
        else:
            self.activity.focus()

    def action_next_pane(self) -> None:
        self._cycle_pane(1)

    def action_previous_pane(self) -> None:
        self._cycle_pane(-1)

    def _cycle_pane(self, step: int) -> None:
        order = self.PANE_ORDER
        current = self.active
        index = order.index(current) if current in order else 0
        self.activate(order[(index + step) % len(order)])


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
        Binding("e", "edit", "edit", show=False),
        Binding("space", "toggle_binding", "Toggle", show=False),
        Binding("a", "add", "Add local plugin", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.extension_mode = False
        self.can_write = False
        self._summary: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._options: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[str, Any] | None = None
        self._status = Static("Loading plugins…", markup=False)
        self._status.add_class("panel-status")
        self._list = OptionList(id="binding-list", markup=False)
        self._detail = Static("Select a plugin to inspect it.", markup=False)
        self._detail.add_class("panel-detail")
        self._hint = Static(
            "e edit · Space/←/→ toggle · a add plugin · r refresh",
            markup=False,
            classes="panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._hint
        yield self._list
        yield self._detail

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
        selected = (
            selected_revision
            if selected_revision is not None
            else (snapshot or {}).get("selectedRevision")
        )
        active = (
            active_revision
            if active_revision is not None
            else (snapshot or {}).get("activeRevision")
        )
        total = len(self._order)
        notes = [f"{total} plugin" + ("" if total == 1 else "s")]
        if selected is not None and active is not None and selected != active:
            notes.append("selected changes apply next turn")
        if not self.can_write:
            notes.append("read-only")
        self._status.update(Text(" · ".join(notes), style="dim"))
        self._hint.update(
            "select to inspect · this host does not advertise configuration updates"
            if not self.can_write
            else "e edit · Space/←/→ toggle · a add plugin · r refresh"
        )
        if self._list.option_count:
            index = self._highlight_index(previous)
            self._list.highlighted = index
            self._show_detail(self._order[index])
        else:
            self._detail.update("No plugins offered by this agent.")

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
        self._hint.update("←/→ change value · Enter open chooser · r refresh")
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
            active_on = (
                active.get("enabled") if isinstance(active, dict) else summary.get("activeEnabled")
            )
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
        lines.append(
            f"id {summary['bindingId']}"
            + (f" · plugin {summary['pluginId']}" if summary["pluginId"] else "")
        )
        if summary["description"]:
            lines.append("")
            lines.append(summary["description"])
        lines.append("")
        selected_state = "on" if summary["enabled"] else "off"
        active = summary.get("state") or {}
        active_enabled = (
            active.get("enabled") if isinstance(active, dict) else summary.get("activeEnabled")
        )
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
            lines.append("toggle allowed" if summary["canToggle"] else "toggle locked")
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
            self.show_message("This host advertises a read-only plugin catalog.")
            return
        if summary["locked"]:
            self.show_message(f"{summary['name']} is locked: {summary['reason'] or 'unavailable'}")
            return
        self.post_message(BindingToggleRequested(binding_id, not summary["enabled"]))

    def action_refresh(self) -> None:
        self.post_message(CapabilityRefreshRequested())

    def action_edit(self) -> None:
        if self.extension_mode and not self.can_write:
            self.show_message("This host advertises a read-only plugin catalog.")
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
            self.show_message("This host advertises a read-only plugin catalog.")
            return
        self.post_message(CapabilityAddRequested())

    def action_toggle_binding(self) -> None:
        if self.extension_mode and not self.can_write:
            self.show_message("This host advertises a read-only plugin catalog.")
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
        Binding("slash", "filter_sessions", "Find session", show=False),
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
        self._sessions: list[dict[str, Any]] = []
        self._current_session: str | None = None
        self._session_notes = ""
        self._filter = Input(placeholder="Filter title or workspace…", id="session-filter")
        self._status = Static(
            "Sessions load when you open this panel.", markup=False, classes="panel-status"
        )
        self._table = DataTable(id="session-table", cursor_type="row", zebra_stripes=False)
        self._hint = Static(
            "u resume · d delete · x close · n new session · r refresh",
            markup=False,
            classes="panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._filter
        yield self._table
        yield self._hint

    def on_mount(self) -> None:
        self._table.add_columns("title", "updated", "workspace")
        self._table.cursor_type = "row"
        self.set_interval(60, self._render_sessions)

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
        self._sessions = list(sessions)
        delete_note = "" if can_delete else " · delete not advertised"
        close_note = "" if can_close else " · close not advertised"
        resume_note = "" if can_resume else " · resume not advertised"
        more = " · m load more pages" if next_cursor else ""
        self._session_notes = f"{more}{delete_note}{close_note}{resume_note}"
        self._render_sessions()
        actions = [] if can_load else ["load unavailable"]
        if can_resume:
            actions.append("u resume")
        if can_delete:
            actions.append("d delete")
        if can_close:
            actions.append("x close")
        if next_cursor:
            actions.append("m more")
        actions.extend(("/ filter", "n new session", "r refresh"))
        self._hint.update(Text(" · ".join(actions), style="dim"))

    def mark_current(self, session_id: str | None) -> None:
        if session_id != self._current_session:
            self._current_session = session_id
            self._render_sessions()

    def _render_sessions(self) -> None:
        selected = self.selected_session_id()
        self._table.clear()
        self._ids.clear()
        self._cwds.clear()
        needle = self._filter.value.strip().casefold()
        for item in self._sessions:
            session_id = str(item.get("sessionId") or item.get("session_id") or "")
            if not session_id:
                continue
            title = str(item.get("title") or "(untitled)")
            cwd = str(item.get("cwd") or "")
            self._cwds[session_id] = cwd
            if needle and needle not in f"{title} {cwd} {session_id}".casefold():
                continue
            updated = str(item.get("updatedAt") or item.get("updated_at") or "")
            try:
                timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                seconds = max(0, int((datetime.now(timezone.utc) - timestamp).total_seconds()))
                updated = (
                    "just now"
                    if seconds < 60
                    else f"{seconds // 60}m ago"
                    if seconds < 3600
                    else f"{seconds // 3600}h ago"
                    if seconds < 86400
                    else f"{seconds // 86400}d ago"
                )
            except ValueError:
                pass
            current = session_id == self._current_session
            label = Text(f"● {title}" if current else title, style="bold" if current else "")
            key = self._table.add_row(label, updated, cwd, key=session_id)
            self._ids[str(key.value)] = session_id
        if selected in self._ids:
            self._table.move_cursor(row=list(self._ids).index(selected))
        self._status.update(
            Text(
                f"{len(self._ids)}/{len(self._sessions)} sessions{self._session_notes}", style="dim"
            )
        )

    def action_filter_sessions(self) -> None:
        self._filter.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self._filter:
            event.stop()
            self._render_sessions()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self._filter:
            event.stop()
            self._table.focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._filter.has_focus:
            event.stop()
            self._filter.value = ""
            self._table.focus()

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
    """Plan, commands, usage and connection details in one scroll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connection = Static("Not connected.", markup=False, classes="activity-section")
        self._plan = Static("No plan reported yet.", markup=False, classes="activity-section")
        self._commands = Static(
            "No agent commands advertised.", markup=False, classes="activity-section"
        )
        self._usage = Static("No usage reported.", markup=False, classes="activity-section")
        self._session = Static("", markup=False, classes="activity-section")

    def compose(self) -> ComposeResult:
        yield Label("Connection", classes="section-title")
        yield self._connection
        yield Label("Plan", classes="section-title")
        yield self._plan
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
        from .state import format_usage

        self._usage.update("Session total\n" + format_usage(summary).replace(" · ", "\n"))

    def set_session(self, lines: list[str]) -> None:
        self._session.update("\n".join(lines))
