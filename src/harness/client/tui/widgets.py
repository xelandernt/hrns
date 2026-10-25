"""Terminal widgets: transcript, composer, status, todo/capability/session/activity panels."""

from __future__ import annotations

import contextlib
import json
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
from textual.message import Message
from textual.widgets import (
    DataTable,
    Input,
    Label,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from ..interactions import (
    AskUserAnswer,
    AskUserQuestion,
    AskUserRequest,
    AskUserResponse,
    PermissionRequest,
)
from ..renderers import RendererRegistry, readable_text
from .choice_keys import choice_direction
from .formatting import truncate_text
from .messages import (
    ApprovalDecided,
    AskUserAnswered,
    CommandSuggestionKey,
)
from .state import binding_summary, config_option_value
from .transcript import (
    CommandResultBlock,
    TranscriptPane,
    _presentation_of,
    _tool_view_state,
)

__all__ = [
    "ActivityPanel",
    "ApprovalBar",
    "ApprovalDecided",
    "AskUserAnswered",
    "AskUserBar",
    "CapabilityPanel",
    "CommandResultBlock",
    "CommandSuggestionKey",
    "McpPanel",
    "McpRefreshRequested",
    "SidePanel",
    "SidePanelCloseRequested",
    "SessionPanel",
    "StatusBar",
    "TodoPanel",
    "TranscriptPane",
]


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
    success: Color | None = None
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
        success=_parse_color(theme.success),
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
        self._cost = ""
        self._permission = ""
        self._activity = ""
        self._hint = ""
        self._context_line = ""
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
        cost: str = "",
        usage_detail: str = "",
        busy: bool = False,
        activity: str = "",
        permission: str = "",
        hint: str = "",
        context: str = "",
    ) -> None:
        self._connected = connected
        self._model = model
        self._effort = effort
        self._mode = mode
        self._usage = usage
        self._cost = cost
        self.tooltip = usage_detail or None
        self._permission = permission
        self._activity = activity
        self._hint = hint
        self._context_line = context
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
            if self._hint:
                return Text(self._hint, style="dim")
            return self._context_text()
        label = self._activity or "working"
        text = self._sweep(label)
        # The hint is an extra, never a truncated fragment; it appears only when
        # the whole phrase fits beside the activity label.
        if self._hint and len(label) + len(self._hint) + 3 <= budget:
            text.append(f"   {self._hint}", style="dim")
        return text

    def _context_text(self) -> Text:
        if not self._context_line:
            return Text()
        palette = _palette(self)
        color = palette.success if self._connected else palette.error
        text = Text()
        text.append("● ", style=Style(color=color))
        text.append(self._context_line, style=Style(color=palette.muted))
        return text

    def _right(self, budget: int) -> Text:
        palette = _palette(self)
        model = (
            Text(self._model, style=Style(color=palette.accent or palette.foreground, bold=True))
            if self._model
            else None
        )
        mode = Text(self._mode, style=Style(color=palette.foreground)) if self._mode else None
        effort = (
            Text(self._effort, style=Style(color=palette.muted or palette.foreground))
            if self._effort
            else None
        )
        permission = (
            Text(self._permission, style=Style(color=palette.warning, bold=True))
            if self._permission and self._permission != "ask"
            else None
        )
        usage = (
            Text(f"ctx {self._usage}", style=Style(color=palette.foreground))
            if self._usage
            else None
        )
        cost = (
            Text(self._cost, style=Style(color=palette.muted or palette.foreground))
            if self._cost
            else None
        )
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
            ("cost", cost),
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
        # the model name and the two usage figures are the last to go.
        for name in ("permission", "effort", "mode", "cost", "usage", "model"):
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
            kind = option.get("kind") or ""
            label, shortcut = APPROVAL_LABELS.get(
                kind, (option.get("name") or kind or "Option", "")
            )
            suffix = f"   ({shortcut})" if shortcut else ""
            label = readable_text(label, limit=120)
            self._options.add_option(Option(f"{label}{suffix}", id=option.get("optionId") or ""))
        kinds = {option.get("kind") or "" for option in request.options}
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
        option_id = option.get("optionId") or "" if option else ""
        return option_id or None

    def option_id_at(self, index: int) -> str | None:
        if self._request is None or not 0 <= index < len(self._request.options):
            return None
        option_id = self._request.options[index].get("optionId") or ""
        return option_id or None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.post_message(ApprovalDecided(str(event.option_id)))


class AskUserBar(Vertical):
    """Non-modal question dock above the composer, one tab per question.

    Modeled on the OpenCode/Codex prompts: the transcript stays visible, the
    composer keeps its place, and the question is answered in place with Tab
    between questions, arrows between options, and a typed custom answer.
    """

    BINDINGS = [
        Binding("tab", "next_question", "Next question", show=False, priority=True),
        Binding("shift+tab", "previous_question", "Previous question", show=False, priority=True),
        Binding("ctrl+enter", "submit", "Submit", show=False, priority=True),
        Binding("ctrl+j", "submit", "Submit", show=False, priority=True),
        # Ctrl+Enter and Ctrl+J only reach us when the terminal reports modified
        # keys (kitty keyboard protocol). Ctrl+S is a plain control byte, so it
        # works everywhere.
        Binding("ctrl+s", "submit", "Submit", show=False, priority=True),
        Binding("escape", "decline", "Decline", show=False, priority=True),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._request: AskUserRequest | None = None
        self.questions: list[AskUserQuestion] = []
        self._tab = 0
        self._answers: list[tuple[str, ...]] = []
        self._custom: list[str] = []
        self._custom_on: list[bool] = []
        self._editing = False
        self._edit_before: tuple[tuple[str, ...], bool, str] | None = None
        self._tabs = Static("", markup=False, classes="ask-tabs")
        self._prompt = Static("", markup=False, classes="ask-question")
        self._options = OptionList(id="ask-options", markup=False)
        self._input = Input(placeholder="Type your own answer…", id="ask-custom-input")
        self._error = Static("", markup=False, classes="dialog-error")
        self._hint = Static("", markup=False, classes="ask-hint")
        self.display = False

    @property
    def pending(self) -> bool:
        return self._request is not None

    def compose(self) -> ComposeResult:
        yield self._tabs
        yield self._prompt
        yield self._options
        yield self._input
        yield self._error
        yield self._hint

    def show(self, request: AskUserRequest) -> None:
        self._request = request
        self.questions = list(request.questions)
        self._tab = 0
        self._answers = [() for _ in self.questions]
        self._custom = ["" for _ in self.questions]
        self._custom_on = [False for _ in self.questions]
        self._editing = False
        self._edit_before = None
        self._error.update("")
        self._hint.update(
            "Tab/Shift+Tab question · ↑/↓ option · 1–9 pick · Enter choose · "
            "Enter on Submit or Ctrl+S submits · Esc decline"
        )
        self._input.display = False
        self._input.value = ""
        self._refresh()
        self.display = True
        self.call_after_refresh(self._options.focus)

    def hide(self) -> None:
        self._request = None
        self._editing = False
        self.display = False
        with contextlib.suppress(Exception):
            self._options.set_options([])

    # ------------------------------------------------------------- rendering

    def _question(self) -> AskUserQuestion:
        return self.questions[self._tab]

    def _answered(self, index: int) -> bool:
        if self._custom_on[index]:
            return bool(self._custom[index].strip())
        return bool(self._answers[index])

    def _tab_text(self) -> Text:
        text = Text()
        for index, question in enumerate(self.questions):
            if index:
                text.append("   ")
            if index == self._tab:
                text.append(f" {index + 1} {question.header} ", style="bold underline")
            else:
                text.append(f" {index + 1} {question.header} ", style="dim")
        text.append(f"   ({self._tab + 1}/{len(self.questions)})", style="dim")
        return text

    def _option_text(self, index: int, label: str, description: str) -> Text:
        question = self._question()
        selected = (
            self._custom_on[self._tab] and index == len(question.options)
        ) or label in self._answers[self._tab]
        if question.multi_select:
            mark = "☑" if selected else "☐"
        else:
            mark = "◉" if selected else "○"
        text = Text()
        text.append(f"{index + 1}. ")
        text.append(f"{mark} ")
        text.append(label, style="bold" if selected else "")
        if description:
            text.append(f"  {description}", style="dim")
        return text

    def _refresh(self, *, highlight: int | None = None) -> None:
        question = self._question()
        self._tabs.update(self._tab_text())
        self._prompt.update(question.question)
        options: list[Option] = []
        for index, option in enumerate(question.options):
            options.append(
                Option(
                    self._option_text(index, option.label, option.description),
                    id=f"option:{index}",
                )
            )
        custom = self._custom[self._tab].strip()
        custom_label = f'Type your own answer — "{custom}"' if custom else "Type your own answer"
        options.append(
            Option(
                self._option_text(len(question.options), custom_label, ""),
                id="custom",
            )
        )
        options.append(Option(Text("↵ Submit answers", style="bold"), id="submit"))
        self._options.set_options(options)
        if highlight is not None:
            self._options.highlighted = max(0, min(highlight, len(options) - 1))
        elif self._custom_on[self._tab]:
            self._options.highlighted = len(question.options)
        else:
            selected = self._answers[self._tab]
            labels = [option.label for option in question.options]
            index = next((labels.index(label) for label in selected if label in labels), 0)
            self._options.highlighted = index

    # -------------------------------------------------------------- editing

    def _begin_edit(self) -> None:
        self._edit_before = (
            self._answers[self._tab],
            self._custom_on[self._tab],
            self._custom[self._tab],
        )
        self._editing = True
        self._custom_on[self._tab] = True
        self._input.value = self._custom[self._tab]
        self._input.display = True
        self._input.focus()
        self._refresh()

    def _commit_edit(self) -> None:
        if not self._editing:
            return
        value = self._input.value.strip()
        self._custom[self._tab] = value
        self._custom_on[self._tab] = bool(value)
        if value:
            self._answers[self._tab] = ()
        self._editing = False
        self._edit_before = None
        self._input.display = False
        self._error.update("")
        self._refresh()

    def _cancel_edit(self) -> None:
        if self._edit_before is not None:
            self._answers[self._tab], self._custom_on[self._tab], self._custom[self._tab] = (
                self._edit_before
            )
        self._editing = False
        self._edit_before = None
        self._input.display = False
        self._refresh()
        self._options.focus()

    def _switch(self, index: int) -> None:
        if self._editing:
            self._commit_edit()
        self._tab = index % len(self.questions)
        self._error.update("")
        self._refresh()
        self._options.focus()

    def _pick(self, index: int) -> None:
        question = self._question()
        if index == len(question.options):
            self._begin_edit()
            return
        label = question.options[index].label
        if question.multi_select:
            current = self._answers[self._tab]
            self._answers[self._tab] = (
                tuple(item for item in current if item != label)
                if label in current
                else (*current, label)
            )
        else:
            self._answers[self._tab] = (label,)
        self._custom_on[self._tab] = False
        self._custom[self._tab] = ""
        self._error.update("")
        self._refresh(highlight=index)

    # --------------------------------------------------------------- events

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        option_id = event.option_id or ""
        if option_id == "submit":
            self.action_submit()
            return
        if option_id == "custom":
            self._begin_edit()
            return
        prefix, _, raw = option_id.partition(":")
        if prefix == "option" and raw.isdigit():
            self._pick(int(raw))

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._editing:
            self._custom[self._tab] = event.value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._commit_edit()
        self._options.focus()

    def on_key(self, event: Any) -> None:
        if self._editing:
            return
        key = str(event.key)
        if key == "space":
            highlighted = self._options.highlighted
            if highlighted is not None:
                event.stop()
                self._pick(highlighted)
            return
        if key.isdigit() and key != "0":
            index = int(key) - 1
            if 0 <= index <= len(self._question().options):
                event.stop()
                self._pick(index)

    # -------------------------------------------------------------- actions

    def action_decline(self) -> None:
        if self._editing:
            self._cancel_edit()
            return
        self.post_message(AskUserAnswered(AskUserResponse(cancelled=True)))

    def action_next_question(self) -> None:
        self._switch(self._tab + 1)

    def action_previous_question(self) -> None:
        self._switch(self._tab - 1)

    def action_submit(self) -> None:
        if self._editing:
            self._commit_edit()
        missing = [index for index in range(len(self.questions)) if not self._answered(index)]
        if missing:
            index = missing[0]
            self._tab = index
            self._refresh()
            self._options.focus()
            self._error.update(
                f"Answer question {index + 1} before submitting, or press Esc to decline."
            )
            return
        answers: list[AskUserAnswer] = []
        for index, question in enumerate(self.questions):
            if self._custom_on[index]:
                answers.append(
                    AskUserAnswer(header=question.header, custom_answer=self._custom[index].strip())
                )
            else:
                answers.append(AskUserAnswer(header=question.header, selected=self._answers[index]))
        self.post_message(AskUserAnswered(AskUserResponse(answers=tuple(answers))))


# --------------------------------------------------------------- side panels


class McpRefreshRequested(Message):
    """The local user asked to probe every configured MCP server again."""


class McpPanel(Vertical):
    """Configured MCP servers, their connection state and discovered tools.

    Adding, authenticating and removing servers belong to ``hrns mcp``; this panel is the
    live view, with enable/disable going through the same capability policy as plugins.
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
        Binding("space", "toggle_server", "Toggle", show=False),
        Binding("a", "authenticate", "Authenticate", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.can_write = False
        self._server_bindings: list[dict[str, Any]] = []
        self._snapshot: dict[str, Any] | None = None
        self._statuses: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._summary: dict[str, dict[str, Any]] = {}
        self._status = Static("MCP servers load when you open /mcp.", markup=False)
        self._status.add_class("panel-status")
        self._list = OptionList(id="mcp-list", markup=False)
        self._detail = Static("", markup=False, classes="panel-detail")
        self._hint = Static(
            "r refresh · Space enable/disable · a auth hint · Esc close",
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
        snapshot: dict[str, Any] | None = None,
        *,
        can_write: bool = True,
    ) -> None:
        self.can_write = can_write
        self._server_bindings = list(bindings)
        self._snapshot = snapshot
        self._render_rows()

    def set_statuses(self, statuses: dict[str, dict[str, Any]]) -> None:
        self._statuses = dict(statuses)
        self._render_rows()

    def statuses(self) -> dict[str, dict[str, Any]]:
        return dict(self._statuses)

    def binding(self, binding_id: str) -> dict[str, Any] | None:
        return self._summary.get(binding_id)

    def current_binding_id(self) -> str | None:
        index = self._list.highlighted
        if index is None or index < 0 or index >= len(self._order):
            return None
        return self._order[index]

    def _render_rows(self) -> None:
        previous = self.current_binding_id()
        self._summary = {}
        self._order = []
        self._list.clear_options()
        for raw in self._server_bindings:
            summary = binding_summary(raw, self._snapshot)
            binding_id = summary["bindingId"]
            if not binding_id:
                continue
            self._summary[binding_id] = summary
            self._order.append(binding_id)
            self._list.add_option(Option(self._row_text(summary), id=binding_id))

        total = len(self._order)
        deferred = sum(1 for status in self._statuses.values() if bool(status.get("deferred")))
        notes = [f"{total} server" + ("" if total == 1 else "s")]
        if deferred:
            notes.append(f"{deferred} deferred")
        if not self.can_write:
            notes.append("read-only")
        self._status.update(Text(" · ".join(notes), style="dim"))
        self._hint.update(
            "r refresh · Esc close"
            if not self.can_write
            else "r refresh · Space enable/disable · a auth hint · Esc close"
        )
        if self._list.option_count:
            index = self._order.index(previous) if previous in self._order else 0
            self._list.highlighted = index
            self._show_detail(self._order[index])
        else:
            self._detail.update(
                Text(
                    "No MCP servers configured. Add one with "
                    "`hrns mcp add NAME -- npx -y PACKAGE`.",
                    style="dim",
                )
            )
        if not self._server_bindings:
            self._status.update(Text("No MCP servers configured.", style="dim"))

    def _row_text(self, summary: dict[str, Any]) -> str:
        name = str(
            (self._statuses.get(summary["bindingId"]) or {}).get("name")
            or server_name_of(summary["bindingId"])
        )
        if not summary["enabled"]:
            return f"{name}   disabled"
        status = self._statuses.get(summary["bindingId"]) or {}
        state = str(status.get("state") or "checking…")
        bits = [f"{name}   {state}"]
        tools = status.get("tools")
        if isinstance(tools, list) and state == "connected":
            bits.append(f"{len(tools)} tool" + ("" if len(tools) == 1 else "s"))
        if status.get("deferred"):
            bits.append("deferred")
        return "   ".join(bits)

    def _show_detail(self, binding_id: str) -> None:
        summary = self._summary.get(binding_id) or {}
        status = self._statuses.get(binding_id) or {}
        if not summary.get("enabled"):
            self._detail.update(
                Text(
                    f"{server_name_of(binding_id)} is disabled. "
                    f"Press Space or run `hrns mcp enable {server_name_of(binding_id)}`.",
                    style="dim",
                )
            )
            return
        lines: list[str] = []
        target = str(status.get("target") or "")
        if target:
            lines.append(target)
        error = status.get("error")
        if error:
            lines.append(str(error))
        tools = status.get("tools")
        if isinstance(tools, list) and tools:
            names = [str(tool.get("name", "")) for tool in tools if isinstance(tool, dict)]
            lines.append("tools: " + ", ".join(names))
        if not lines:
            lines.append("Probing on next refresh…")
        self._detail.update(Text("\n".join(lines), style="dim"))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id:
            self._show_detail(event.option_id)

    def action_refresh(self) -> None:
        self.post_message(McpRefreshRequested())

    def action_toggle_server(self) -> None:
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        summary = self._summary.get(binding_id)
        if summary is None:
            return
        if not self.can_write:
            self.show_message("This host advertises a read-only plugin catalog.")
            return
        if summary["locked"]:
            self.show_message(f"{summary['name']} is locked: {summary['reason'] or 'unavailable'}")
            return
        self.post_message(BindingToggleRequested(binding_id, not summary["enabled"]))

    def action_authenticate(self) -> None:
        binding_id = self.current_binding_id()
        if not binding_id:
            return
        name = server_name_of(binding_id)
        status = self._statuses.get(binding_id) or {}
        if status.get("authenticated") is None:
            self.show_message(f"{name} does not use OAuth. Inspect it with `hrns mcp get {name}`.")
        elif status.get("authenticated") is False or status.get("state") == "needs auth":
            self.show_message(f"Run `hrns mcp auth {name}` in a terminal, then press r to refresh.")
        else:
            self.show_message(
                f"OAuth is managed by the CLI: `hrns mcp auth {name}` / `hrns mcp logout {name}`."
            )


def server_name_of(binding_id: str) -> str:
    """The configured server name behind an ``mcp.<name>`` binding id."""
    return binding_id.removeprefix("mcp.")


class SidePanelCloseRequested(Message):
    """The user asked to dismiss the on-demand side panel."""


class SidePanel(TabbedContent):
    """Todos, plugins, MCP servers, sessions and activity, opened as a right drawer."""

    PANE_ORDER: tuple[str, ...] = ("todos", "capabilities", "mcp", "sessions", "activity")

    BINDINGS = [
        Binding("tab", "next_pane", "Next panel", show=False),
        Binding("shift+tab", "previous_pane", "Previous panel", show=False),
        Binding("escape", "close", "Close panel", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(initial="capabilities", id="side-panel")
        self.todos = TodoPanel(id="todo-panel")
        self.capabilities = CapabilityPanel(id="capability-panel")
        self.mcp = McpPanel(id="mcp-panel")
        self.sessions = SessionPanel(id="session-panel")
        self.activity = ActivityPanel(id="activity-panel")
        # TabbedContent.compose() builds its private ContentTabs from this list.
        # Overriding compose() here would omit ContentTabs entirely.
        self._tab_content.extend(
            [
                TabPane("Todos", self.todos, id="todos"),
                TabPane("Plugins", self.capabilities, id="capabilities"),
                TabPane("MCP", self.mcp, id="mcp"),
                TabPane("Sessions", self.sessions, id="sessions"),
                TabPane("Activity", self.activity, id="activity"),
            ]
        )

    def activate(self, tab: str) -> None:
        """Show ``tab`` and move focus into its content."""
        self.active = tab
        # The pane is only focusable once the switch has been laid out.
        self.call_after_refresh(self._focus_pane, tab)

    def select(self, tab: str) -> None:
        """Show ``tab`` without taking focus away from the composer."""
        self.active = tab

    def _focus_pane(self, tab: str) -> None:
        if tab == "todos":
            self.todos.focus()
        elif tab == "capabilities":
            self.capabilities._list.focus()
        elif tab == "mcp":
            self.mcp._list.focus()
        elif tab == "sessions":
            self.sessions._table.focus()
        else:
            self.activity.focus()

    def action_next_pane(self) -> None:
        self._cycle_pane(1)

    def action_previous_pane(self) -> None:
        self._cycle_pane(-1)

    def action_close(self) -> None:
        self.post_message(SidePanelCloseRequested())

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
        self._plugin_bindings: list[dict[str, Any]] = []
        self._snapshot: dict[str, Any] | None = None
        self._summary: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._options: dict[str, dict[str, Any]] = {}
        self._row_chars = 0
        self._name_chars = 6
        self._status = Static("Loading plugins…", markup=False)
        self._status.add_class("panel-status")
        self._list = OptionList(id="binding-list", markup=False)
        self._detail = Static("", markup=False, classes="panel-detail")
        self._detail.display = False
        self._hint = Static(
            "e edit · Space toggle · a add · r refresh · Esc close",
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
        self.can_write = can_write
        self._plugin_bindings = list(bindings)
        self._snapshot = snapshot
        self._render_rows(active_revision=active_revision, selected_revision=selected_revision)

    def _render_rows(
        self,
        *,
        active_revision: int | None = None,
        selected_revision: int | None = None,
    ) -> None:
        """Rebuild every plugin row; descriptions follow the current panel width."""
        previous = self.current_binding_id()
        self._row_chars = self._row_width()
        self._summary = {}
        self._order = []
        self._options = {}
        self._list.clear_options()
        for raw in self._plugin_bindings:
            summary = binding_summary(raw, self._snapshot)
            binding_id = summary["bindingId"]
            if not binding_id:
                continue
            self._summary[binding_id] = summary
            self._order.append(binding_id)
            self._options[binding_id] = raw
        self._name_chars = self._name_column()
        for binding_id in self._order:
            self._list.add_option(Option(self._row_text(self._summary[binding_id]), id=binding_id))
        selected = (
            selected_revision
            if selected_revision is not None
            else (self._snapshot or {}).get("selectedRevision")
        )
        active = (
            active_revision
            if active_revision is not None
            else (self._snapshot or {}).get("activeRevision")
        )
        total = len(self._order)
        notes = [f"{total} plugin" + ("" if total == 1 else "s")]
        if selected is not None and active is not None and selected != active:
            notes.append("selected changes apply next turn")
        if not self.can_write:
            notes.append("read-only")
        self._status.update(Text(" · ".join(notes), style="dim"))
        self._hint.update(
            "read-only · r refresh · Esc close"
            if not self.can_write
            else "e edit · Space toggle · a add · r refresh · Esc close"
        )
        if self._list.option_count:
            index = self._highlight_index(previous)
            self._list.highlighted = index
            self._show_detail(self._order[index])
        else:
            self._show_detail(None)

    def _row_width(self) -> int:
        """Characters available to one list row."""
        width = self._list.content_size.width
        if width <= 0:
            width = self.size.width - 6
        return max(0, width)

    def _show_detail(self, binding_id: str | None) -> None:
        """Render the highlighted plugin's full description below the list."""
        summary = self._summary.get(binding_id) if binding_id else None
        if summary is None:
            self._detail.update(Text(""))
            self._detail.display = False
            return
        lines = [
            line for line in (" ".join(str(summary.get("description") or "").split()),) if line
        ]
        if summary["locked"]:
            lines.append(f"locked: {summary['reason'] or 'unavailable'}")
        requires = [str(item) for item in summary.get("requires") or ()]
        if requires:
            lines.append("requires: " + ", ".join(requires))
        needs = [str(item) for item in summary.get("needs") or ()]
        if needs:
            lines.append("needs: " + ", ".join(needs))
        conflicts = [str(item) for item in summary.get("conflicts") or ()]
        if conflicts:
            lines.append("conflicts: " + ", ".join(conflicts))
        self._detail.update(Text("\n".join(lines), style="dim"))
        self._detail.display = bool(lines)

    def set_standard_options(self, options: list[dict[str, Any]]) -> None:
        self.extension_mode = False
        self.can_write = True
        self._plugin_bindings = []
        self._snapshot = None
        previous = self.current_binding_id()
        self._summary = {}
        self._order = []
        self._options = {}
        self._list.clear_options()
        self._show_detail(None)
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
        self._hint.update("←/→ change · Enter chooser · r refresh · Esc close")
        if self._list.option_count:
            self._list.highlighted = self._highlight_index(previous)
        else:
            self.show_message("This agent advertises no configuration options.")

    def _highlight_index(self, previous: str | None) -> int:
        """Keep the edited row selected instead of jumping back to the top."""
        if previous and previous in self._order:
            return self._order.index(previous)
        return 0

    def _name_column(self) -> int:
        """Width of the name column, so toggles and descriptions line up."""
        longest = max(
            (len(str(self._summary[binding_id]["name"])) for binding_id in self._order),
            default=0,
        )
        # Keep at least a sliver of description visible on narrow drawers.
        cap = max(6, self._row_chars - 24)
        return max(6, min(longest, cap, 24))

    def _row_text(self, summary: dict[str, Any]) -> str:
        enabled = "on" if summary["enabled"] else "off"
        name = truncate_text(str(summary["name"]), self._name_chars)
        toggle = f"<{enabled}>"
        prefix = f"{name:<{self._name_chars}}  {toggle:<5}  "
        tail: list[str] = []
        if summary["locked"]:
            tail.append("locked")
        elif summary.get("pending"):
            active = summary.get("state") or {}
            active_on = (
                active.get("enabled") if isinstance(active, dict) else summary.get("activeEnabled")
            )
            if active_on is not None and bool(active_on) != summary["enabled"]:
                tail.append("next turn")
        if summary["changeMode"] == "new_session":
            tail.append("new session")
        description = " ".join(str(summary.get("description") or "").split())
        # The description starts in its own column and is clipped to whatever room is
        # left; selecting the row shows the full text in the detail line.
        room = self._row_chars - len(prefix) - sum(len(bit) + 3 for bit in tail)
        description = truncate_text(description, room) if room >= 8 else ""
        suffix = "".join(f"   {bit}" for bit in tail)
        head = f"{name:<{self._name_chars}}  {toggle:<5}"
        if description:
            head += f"  {description}"
        return f"{head}{suffix}".rstrip()

    # --------------------------------------------------------------- events

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.extension_mode:
            self._show_detail(event.option_id)

    def on_resize(self, event: events.Resize) -> None:
        """Re-clip descriptions when the drawer changes width."""
        if not self.extension_mode or not self._plugin_bindings:
            return
        width = self._row_width()
        if width and width != self._row_chars:
            self._render_rows()

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
            "u resume · d delete · x close · n new · r refresh · Esc close",
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
    """Commands, usage and connection details in one scroll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connection = Static("Not connected.", markup=False, classes="activity-section")
        self._commands = Static(
            "No agent commands advertised.", markup=False, classes="activity-section"
        )
        self._usage = Static("No usage reported.", markup=False, classes="activity-section")
        self._session = Static("", markup=False, classes="activity-section")

    def compose(self) -> ComposeResult:
        yield Label("Connection", classes="section-title")
        yield self._connection
        yield Label("Agent commands", classes="section-title")
        yield self._commands
        yield Label("Usage", classes="section-title")
        yield self._usage
        yield Label("Session", classes="section-title")
        yield self._session

    def set_connection(self, lines: list[str]) -> None:
        self._connection.update("\n".join(lines))

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


class TodoPanel(VerticalScroll):
    """The current session's plan, as reported by the agent."""

    can_focus = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._summary = Static("No plan yet.", markup=False, classes="todo-summary")
        self._list = Static("", markup=False, classes="todo-list")
        self._entries: tuple[tuple[str, str, str], ...] = ()

    def compose(self) -> ComposeResult:
        yield self._summary
        yield self._list

    def set_plan(
        self,
        entries: tuple[tuple[str, str, str], ...],
        *,
        cleared: bool = False,
    ) -> None:
        """Replace the visible plan with a complete agent snapshot."""
        self._entries = () if cleared else entries
        if not self._entries:
            self._summary.update("No plan yet.")
            self._list.update("")
            return
        done = sum(1 for _, _, status in self._entries if status == "completed")
        total = len(self._entries)
        self._summary.update(f"{done} of {total} done")
        self._list.update(self._render_plan(self._entries))

    @staticmethod
    def _render_plan(entries: tuple[tuple[str, str, str], ...]) -> Text:
        body = Text()
        for content, priority, status in entries:
            if status == "completed":
                marker, style = "●", "dim"
            elif status == "in_progress":
                marker, style = "◐", "bold"
            elif status == "cancelled":
                marker, style = "✕", "dim strike"
            else:
                marker, style = "○", ""
            body.append(f"{marker} ", style="bold" if status == "in_progress" else "dim")
            body.append(content, style=style)
            if priority == "high":
                body.append("  high", style="bold yellow")
            body.append("\n")
        return body
