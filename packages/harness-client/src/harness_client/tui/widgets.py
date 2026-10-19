"""Terminal widgets: transcript, composer, status, capability/session/activity panels."""

from __future__ import annotations

from collections import deque
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
from ..renderers import RendererRegistry, readable_text
from .state import (
    binding_summary,
    config_option_value,
    is_subagent_tool,
    merge_tool_event,
    pending_application_text,
    presentation_payload,
    tool_status_label,
)

__all__ = [
    "ActivityPanel",
    "CapabilityPanel",
    "Composer",
    "HintBar",
    "SidePanel",
    "SessionPanel",
    "StatusBar",
    "TranscriptPane",
    "TopBar",
]

TOOL_GLYPHS = {
    "read": "▤",
    "edit": "✎",
    "execute": "›",
    "fetch": "⇢",
    "search": "⌕",
    "think": "∴",
    "other": "•",
}

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


class Composer(TextArea):
    """Multiline prompt editor. Enter sends; Ctrl+Enter and Ctrl+J also send."""

    BINDINGS = [
        Binding("ctrl+enter", "submit", "Send", show=False, priority=True),
        Binding("ctrl+j", "submit", "Send", show=False, priority=True),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.show_line_numbers = False
        self.soft_wrap = True

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.action_submit()

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
                self._thought = ReasoningBlock()
                await self._mount_block(self._thought)
            self._thought_buffer += text
            self._thought.append_text(text)
        else:
            if self._stream is None:
                self._stream = MessageBlock("agent", "")
                await self._mount_block(self._stream)
            self._stream_buffer += text
            self._stream.append_text(text)
            self._dirty = True

    async def finish_message(self) -> None:
        self.flush()
        if self._stream is not None:
            self._stream.finish()
        if self._thought is not None:
            self._thought.finish()
        self._stream = None
        self._stream_buffer = ""
        self._thought = None
        self._thought_buffer = ""

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

    def __init__(self, role: str, text: str = "") -> None:
        super().__init__()
        self.role = role
        self._text = text
        self._dropped = 0
        self._marked = False
        self._label = Static(Text(role, style="dim"))
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
    """Agent-provided reasoning, collapsed by default; never fabricated."""

    def __init__(self) -> None:
        self._text = ""
        self._dropped = 0
        self._marked = False
        self._body = Static("", markup=False)
        self._body.add_class("reasoning-body")
        super().__init__(self._body, title="Reasoning", collapsed=True)

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
        self._body.update(readable_text(self._text))

    def finish(self) -> None:
        self._body.update(readable_text(self._text))


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
    """A generic, always-readable tool activity card with expandable details.

    Generic title/status/bounded text are always present. A versioned
    presentation payload may be rendered by a locally installed adapter;
    unknown adapters, versions and exceptions fall back to that generic text.
    """

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
        self._header = Static(markup=False)
        self._detail = Static("", markup=False)
        self._collapsible = Collapsible(
            self._detail, title="Details", collapsed=not auto_expand
        )
        self.add_class("tool-card")
        self._render_header()
        self._render_detail()

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._collapsible

    @property
    def tool_call_id(self) -> str:
        return self.data.get("toolCallId", "")

    def _render_header(self) -> None:
        glyph = TOOL_GLYPHS.get(self.data.get("kind") or "other", "•")
        status = tool_status_label(self.data.get("status", ""))
        title = self.data.get("title") or self.data.get("toolCallId") or "tool"
        text = Text()
        text.append(f"{glyph} ", style="bold")
        text.append(readable_text(title))
        text.append("   ")
        text.append(status, style="dim")
        if self.data.get("kind"):
            text.append(f"   {self.data['kind']}", style="dim")
        self._header.update(text)

    def _render_detail(self) -> None:
        generic = readable_text(self.data.get("detail") or "").strip()
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
        if not rendered.strip():
            rendered = "No details were supplied by the agent."
        self._detail.update(rendered)
        self._collapsible.display = True

    def update_from(self, data: dict[str, Any]) -> None:
        self.data = dict(data)
        self._render_header()
        self._render_detail()


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
        mode: str = "",
        usage: str = "",
        busy: bool = False,
        detail: str = "",
    ) -> None:
        text = Text()
        text.append("● " if connected else "○ ", style="bold" if connected else "dim")
        text.append("connected" if connected else "disconnected", style="bold" if connected else "dim")
        if session_id:
            text.append("   session ")
            text.append(session_id, style="bold")
        if mode:
            text.append(f"   mode {mode}")
        if model:
            text.append(f"   model {model}")
        if usage:
            text.append(f"   {usage}")
        if busy:
            text.append("   working…", style="bold")
        if detail:
            text.append(f"   {detail}", style="dim")
        self.update(text)


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
            "Enter toggles · e edits fields · a adds a local plugin · r refreshes",
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
            "Enter inspects · this host does not advertise configuration updates"
            if not self.can_write
            else "Enter toggles · e edits fields · a adds a local plugin · r refreshes"
        )
        if self._list.option_count:
            self._list.highlighted = 0
            self._show_detail(self._order[0])
        else:
            self._detail.update("No capabilities offered by this agent.")

    def set_standard_options(self, options: list[dict[str, Any]]) -> None:
        self.extension_mode = False
        self.can_write = True
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
            self._list.add_option(Option(f"{name}: {value}", id=option_id))
        self._status.update(
            Text(
                "Standard ACP configuration only. This agent does not advertise the richer "
                "capability extension.",
                style="dim",
            )
        )
        if self._list.option_count:
            self._list.highlighted = 0
            self._show_standard_detail(self._order[0])
        else:
            self._detail.update("This agent advertises no configuration options.")

    def _row_text(self, summary: dict[str, Any]) -> str:
        enabled = "on" if summary["enabled"] else "off"
        bits = [f"{summary['name']}   {enabled}"]
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
        summary = self._summary.get(option_id)
        if summary is None:
            return
        if not self.can_write:
            self._show_detail(option_id)
            self.show_message("This host advertises a read-only capability catalog.")
            return
        if summary["locked"]:
            self.show_message(f"{summary['name']} is locked: {summary['reason'] or 'unavailable'}")
            return
        self.post_message(BindingToggleRequested(option_id, not summary["enabled"]))

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
        summary = self._summary.get(binding_id)
        if summary:
            self.post_message(BindingToggleRequested(binding_id, not summary["enabled"]))

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
            "Enter loads · u resumes · d deletes · x closes · n new session · r refreshes",
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
        actions = ["Enter loads" if can_load else "Enter explains load unavailable"]
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
