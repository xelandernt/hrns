"""Composer, command suggestions, attachments, and the prompt queue."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.strip import Strip
from textual.widgets import Button, Label, OptionList, TextArea
from textual.widgets.option_list import Option

from .formatting import _restyle_spans
from .messages import AttachmentRemoved, CommandSuggestionKey, QueueAction, Submitted

#: ``$skill`` and ``@path`` references in the composer. A match is only coloured when
#: it names a known, user-invocable skill or a file in the workspace index, so a typo
#: like ``$dwadawdawda`` stays plain.
MENTION_PATTERN = re.compile(r"(?<!\S)(\$[A-Za-z0-9][\w-]*|@[^\s]+)")

#: Punctuation that may trail a file reference without being part of the path.
MENTION_TRAILING = ".,;:!?)]}'\""


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

    COMPONENT_CLASSES = TextArea.COMPONENT_CLASSES | {
        "text-area--skill-token",
        "text-area--file-token",
    }

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
        self._skill_names: frozenset[str] = frozenset()
        self._file_paths: frozenset[str] = frozenset()

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

    def set_mention_catalog(self, *, skills: Iterable[str], files: Iterable[str]) -> None:
        """Declare which ``$skill`` and ``@path`` references are real."""
        skill_names = frozenset(name for name in skills if name)
        file_paths = frozenset(path for path in files if path)
        if skill_names == self._skill_names and file_paths == self._file_paths:
            return
        self._skill_names = skill_names
        self._file_paths = file_paths
        self._line_cache.clear()
        self.refresh()

    def _mention_spans(self, text: str) -> list[tuple[int, int, str]]:
        """Valid ``$skill`` / ``@path`` spans in a rendered line."""
        spans: list[tuple[int, int, str]] = []
        for match in MENTION_PATTERN.finditer(text):
            token = match.group(0)
            marker = token[0]
            name = token[1:]
            if marker == "@":
                name = name.rstrip(MENTION_TRAILING)
                if name not in self._file_paths:
                    continue
                spans.append((match.start(), match.start() + 1 + len(name), marker))
            elif name in self._skill_names:
                spans.append((match.start(), match.end(), marker))
        return spans

    def _render_line(self, y: int) -> Strip:
        """Re-colour known ``$skill`` / ``@path`` references in the rendered line."""
        strip = super()._render_line(y)
        text = strip.text
        if "$" not in text and "@" not in text:
            return strip
        spans = self._mention_spans(text)
        if not spans:
            return strip
        return _restyle_spans(
            strip,
            spans,
            {
                "$": self.get_component_rich_style("text-area--skill-token"),
                "@": self.get_component_rich_style("text-area--file-token"),
            },
        )


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
