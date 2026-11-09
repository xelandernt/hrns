"""Approval dock above the composer."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from ..interactions import PermissionRequest
from ..renderers import RendererRegistry, readable_text
from .messages import ApprovalDecided
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
