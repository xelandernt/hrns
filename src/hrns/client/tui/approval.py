"""Approval and ask-user docks above the composer."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ..interactions import (
    AskUserAnswer,
    AskUserQuestion,
    AskUserRequest,
    AskUserResponse,
    PermissionRequest,
)
from ..renderers import RendererRegistry, readable_text
from .messages import ApprovalDecided, AskUserAnswered
from .transcript import _presentation_of, _tool_view_state

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
        self._content = VerticalScroll(self._detail, self._view_host, classes="approval-content")
        self._expand = Button("Show full call", classes="approval-expand", compact=True)
        self._expanded = False
        self._full_detail = ""
        self._view: Any = None
        self._options = OptionList(id="approval-options", markup=False)
        self._hint = Static("", markup=False)
        self._hint.add_class("approval-hint")
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._content
        yield self._expand
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
        self._expanded = False
        self._expand.label = "Show full call"
        self._full_detail = (
            json.dumps(request.raw_input, indent=2, ensure_ascii=False, default=str)
            if request.raw_input is not None
            else request.detail or "No additional tool details were provided."
        )
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
        self.call_after_refresh(self._content.scroll_home, animate=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button is not self._expand or self._request is None:
            return
        event.stop()
        self._expanded = not self._expanded
        self._detail.display = self._expanded or self._view is None
        self._view_host.display = not self._expanded and self._view is not None
        self._detail.update(
            readable_text(self._full_detail, limit=len(self._full_detail))
            if self._expanded
            else _approval_detail(self._request)
        )
        self._expand.label = "Show preview" if self._expanded else "Show full call"
        self._content.scroll_home(animate=False)

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
        self._view_host.display = True
        self._detail.display = False
        if self.is_mounted and not view.widget.is_mounted:
            self._view_host.mount(view.widget)

    def _clear_view(self) -> None:
        view, self._view = self._view, None
        self._detail.display = True
        self._view_host.display = False
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
