"""Transcript message, reasoning, and notice blocks."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Button, Collapsible, Markdown, Static

from ..renderers import readable_text
from .formatting import MAX_STREAM_MESSAGE_CHARS, SPINNER_FRAMES


class MessageBlock(Vertical):
    """One message: a small role label and streaming Markdown body.

    Retained text is capped at ``MAX_STREAM_MESSAGE_CHARS``; older characters
    are dropped from the head and counted so the trim stays visible.
    """

    ROLE_LABELS = {"you": "You", "agent": "Assistant"}
    COPY_FEEDBACK_DURATION = 1.5
    DEFAULT_CSS = """
    MessageBlock .message-header {
        height: 1;
    }
    MessageBlock .message-role {
        width: 1fr;
        height: 1;
        margin-top: 0;
    }
    MessageBlock .copy-message {
        width: 3;
        min-width: 3;
        height: 1;
        min-height: 1;
        padding: 0;
        border: none;
        color: $text-muted;
        background: transparent;
    }
    MessageBlock .copy-message:hover {
        color: $foreground;
        background: $accent 15%;
    }
    MessageBlock .copy-message:focus {
        color: $foreground;
        background: $accent 20%;
        text-style: bold;
    }
    MessageBlock .copy-message:disabled {
        color: $text-disabled;
        background: transparent;
    }
    """

    def __init__(self, role: str, text: str = "") -> None:
        super().__init__()
        self.role = role
        self._text = text
        self._dropped = 0
        self._marked = False
        self._finished = role != "agent"
        self._copy_reset_timer: Timer | None = None
        # Textual's Markdown parses and mounts the whole document on every update, so a
        # streaming reply re-rendered its entire history 16 times a second. Remembering
        # what was already handed over lets each tick append only the new fragment.
        self._rendered = ""
        self._render_job: AwaitComplete | None = None
        self._catch_up: asyncio.Task[None] | None = None
        label = Text()
        label.append(self.ROLE_LABELS.get(role, role.title()), style="bold")
        label.append("  ")
        label.append(datetime.now().strftime("%H:%M"), style="dim")
        self._label = Static(label, markup=False)
        self._label.add_class("message-role")
        self._copy_button = Button(
            "⧉",
            id="copy-message",
            classes="copy-message",
            disabled=not self._finished,
            tooltip="Copy message",
            compact=True,
        )
        self._copy_button.active_effect_duration = 0
        self._body = Markdown(text) if role == "agent" else Static(text, markup=False)
        self._body.add_class("message-text" if role == "you" else "message-body")
        self.add_class(f"role-{role}")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="message-header"):
            yield self._label
            yield self._copy_button
        if self.role == "you":
            with Vertical(classes="message-body"):
                yield Static(classes="transcript-bar")
                yield self._body
        else:
            yield self._body

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button is not self._copy_button:
            return
        event.stop()
        self.app.copy_to_clipboard(self._text)
        self._copy_button.label = "✓"
        self._copy_button.tooltip = "Message copied"
        if self._copy_reset_timer is not None:
            self._copy_reset_timer.stop()
        self._copy_reset_timer = self.set_timer(
            self.COPY_FEEDBACK_DURATION, self._reset_copy_feedback
        )

    def _reset_copy_feedback(self) -> None:
        self._copy_reset_timer = None
        self._copy_button.label = "⧉"
        self._copy_button.tooltip = "Copy message"

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
        # Markdown.update needs a live app for its theme; pre-mount callers are
        # buffered until on_mount rather than raising NoActiveAppError.
        if not self.is_mounted:
            return
        source = self._source_text()
        if source == self._rendered:
            return
        body = self._body
        if not isinstance(body, Markdown):
            # "you" blocks are plain Static text; there is nothing to re-parse.
            self._rendered = source
            body.update(source)
            return
        if self._render_job is not None and not self._render_job.is_done:
            # The fragment already queued will follow this text; let it land before
            # appending more instead of piling up full re-parses.
            self._schedule_catch_up()
            return
        if self._rendered and source.startswith(self._rendered):
            delta = source[len(self._rendered) :]
            self._rendered = source
            self._render_job = body.append(delta)
        else:
            self._rendered = source
            self._render_job = body.update(source)

    def _source_text(self) -> str:
        marker = (
            f"⋯ {self._dropped:,} earlier characters of this message trimmed from view ⋯\n\n"
            if self._dropped
            else ""
        )
        return marker + readable_text(self._text)

    def _schedule_catch_up(self) -> None:
        render = self._render_job
        if render is None or self._catch_up is not None:
            return

        async def follow_up() -> None:
            with contextlib.suppress(BaseException):
                await render
            self._catch_up = None
            self.call_later(self.flush)

        # Owned by this block and cancelled on unmount; follow_up() observes errors.
        self._catch_up = asyncio.create_task(follow_up())

    def on_unmount(self) -> None:
        if self._catch_up is not None:
            self._catch_up.cancel()
            self._catch_up = None
        if self._copy_reset_timer is not None:
            self._copy_reset_timer.stop()
            self._copy_reset_timer = None

    def on_mount(self) -> None:
        # Markdown mounts its initial document asynchronously; flush buffered text
        # after that initialization so an empty initial document cannot replace it.
        self.call_after_refresh(self.flush)
        self._copy_button.disabled = not self._finished

    def finish(self) -> None:
        self.flush()
        if self._finished:
            return
        self._finished = True
        if self.is_mounted:
            self._copy_button.disabled = False


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
        self._timer: Timer | None = None
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


class NoticeBlock(Vertical):
    def __init__(self, text: str, *, severity: str = "info") -> None:
        self.severity = severity
        self.text = text
        super().__init__()
        self._body = Static(self._render_notice(text, severity), markup=False)
        self.add_class("notice")
        self.add_class(f"notice-{severity}")

    def compose(self) -> ComposeResult:
        yield Static(classes="transcript-bar")
        yield self._body

    @staticmethod
    def _render_notice(text: str, severity: str) -> Text:
        prefix = {"info": "·", "warning": "!", "error": "✕", "success": "✓"}.get(severity, "·")
        rendered = Text()
        rendered.append(f"{prefix} ", style="bold")
        rendered.append(text)
        return rendered
