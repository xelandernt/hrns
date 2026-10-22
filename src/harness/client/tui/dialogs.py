"""Modal dialogs: approvals, elicitation forms, configuration, commands, settings."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from ..interactions import (
    PERMISSION_MODES,
    ElicitationAnswer,
    ElicitationRequest,
    PermissionRequest,
)
from .choice_keys import handle_choice_arrow
from .commands import LocalCommand, command_suggestion_text
from .state import Settings, build_form_fields, coerce_value

__all__ = [
    "ChoiceDialog",
    "CommandPaletteDialog",
    "ConfirmDialog",
    "ElicitationDialog",
    "FormDialog",
    "HelpDialog",
    "InfoDialog",
    "PermissionDialog",
    "SettingsDialog",
]


class _DialogBase(ModalScreen[Any]):
    """Shared esc-to-cancel and dialog chrome."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, subtitle: str = "") -> None:
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._error = Static("", markup=False, classes="dialog-error")
        self.query_prefix = ""

    def dialog_header(self) -> ComposeResult:
        yield Static(Text(self._title, style="bold"), classes="dialog-title")
        if self._subtitle:
            yield Static(self._subtitle, markup=False, classes="dialog-subtitle")

    def show_error(self, message: str) -> None:
        self._error.update(message)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event: Any) -> None:
        """Left/Right change the focused choice control."""
        if handle_choice_arrow(self.focused, str(event.key)):
            event.stop()


class PermissionDialog(_DialogBase):
    """Approval choice dialog: agent-supplied options, keyboard-first."""

    def __init__(self, request: PermissionRequest) -> None:
        super().__init__("Approval needed", request.summary())
        self.request = request
        self._options = OptionList(id="permission-options", markup=False)
        self._detail = Static("", markup=False, classes="permission-detail")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            yield Static(
                "The agent is waiting for a decision. Choosing an option is the only way it continues.",
                markup=False,
                classes="dialog-help",
            )
            detail = ""
            if self.request.kind:
                detail += f"kind: {self.request.kind}\n"
            if self.request.status:
                detail += f"status: {self.request.status}\n"
            if self.request.detail:
                detail += f"\n{self.request.detail}"
            self._detail.update(detail or "No additional tool details were provided.")
            yield self._detail
            for index, option in enumerate(self.request.options):
                label = f"{index + 1}. {option.get('name', '')}   ({option.get('kind', '')})"
                self._options.add_option(Option(label, id=option.get("optionId", "")))
            yield self._options
            yield self._error
            yield Static(
                "↑/↓ choose · 1–9 quick select · Esc cancel",
                markup=False,
                classes="dialog-hint",
            )

    def on_mount(self) -> None:
        self._options.focus()
        if self._options.option_count:
            self._options.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def on_key(self, event: Any) -> None:
        if handle_choice_arrow(self.focused, str(event.key)):
            event.stop()
            return
        if event.key.isdigit() and event.key != "0":
            index = int(event.key) - 1
            if 0 <= index < len(self.request.options):
                event.stop()
                self.dismiss(self.request.options[index].get("optionId"))
                return
        if event.key == "y":
            allow = self.request.first_of_kind("allow_once", "allow_always")
            if allow:
                event.stop()
                self.dismiss(allow.get("optionId"))
        if event.key == "n":
            reject = self.request.first_of_kind("reject_once", "reject_always")
            if reject:
                event.stop()
                self.dismiss(reject.get("optionId"))


_FORM_BINDINGS = [
    Binding("up", "focus_previous", "Previous field", show=False, priority=True),
    Binding("down", "focus_next", "Next field", show=False, priority=True),
    Binding("tab", "focus_next", "Next field", show=False, priority=True),
    Binding("shift+tab", "focus_previous", "Previous field", show=False, priority=True),
    Binding("enter", "accept_form", "Save", show=False, priority=True),
]


def _field_text(kind: str, value: Any) -> str:
    """Render a raw value as the text a row edits or displays."""
    if kind in {"array", "multi"} and isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


class _FieldRow(Horizontal):
    """One form line: indicator, label, and an inline value.

    Text kinds mount an ``Input`` with no border so the row reads as a line.
    Choice and boolean kinds render a plain value and react to Left/Right.
    """

    can_focus = True

    def __init__(self, field: dict[str, Any]) -> None:
        super().__init__(classes="field-row")
        self.field = field
        self.field_name = str(field.get("name") or "")
        self.kind = str(field.get("kind") or "string")
        self.title = str(field.get("title") or self.field_name)
        self.required = bool(field.get("required"))
        self._choices: list[tuple[str, str]] = [
            (str(value), str(label)) for value, label in (field.get("choices") or [])
        ]
        default = field.get("default")
        self._input: Input | None = None
        self._silent: str | None = None
        self.on_changed: Callable[[_FieldRow], None] | None = None
        if self.kind == "boolean":
            self._flag = bool(default)
        elif self.kind == "select":
            self._selection = "" if default is None else str(default)
        else:
            placeholder = "comma-separated values" if self.kind in {"array", "multi"} else "(none)"
            self._input = Input(
                _field_text(self.kind, default),
                password=self.kind == "password",
                type=(
                    "integer"
                    if self.kind == "integer"
                    else "number"
                    if self.kind == "number"
                    else "text"
                ),
                placeholder=placeholder,
            )
        self._view = Static(self._display(), markup=False, classes="field-value")

    def compose(self) -> ComposeResult:
        yield Static("›", classes="field-gutter")
        yield Static(
            self.title + (" *" if self.required else ""),
            markup=False,
            classes="field-name",
        )
        yield self._input if self._input is not None else self._view

    def _display(self) -> str:
        if self.kind == "boolean":
            return "on" if self._flag else "off"
        if self.kind == "select":
            for value, label in self._choices:
                if value == self._selection:
                    return label
            return self._selection or "(unset)"
        return _field_text(self.kind, self._input.value if self._input else "")

    def _refresh_view(self) -> None:
        if self._view.is_mounted:
            self._view.update(self._display())

    def value(self) -> Any:
        if self.kind == "boolean":
            return self._flag
        if self.kind == "select":
            return self._selection
        return self._input.value if self._input is not None else ""

    def set_value(self, value: Any) -> None:
        if self.kind == "boolean":
            self._flag = bool(value)
        elif self.kind == "select":
            self._selection = "" if value is None else str(value)
        elif self._input is not None:
            text = _field_text(self.kind, value)
            if self._input.value != text:
                self._silent = text
                self._input.value = text
        self._refresh_view()

    def set_choices(self, choices: list[tuple[str, str]], *, selected: str = "") -> None:
        self._choices = list(choices)
        self._selection = selected
        self._refresh_view()

    def focus_target(self) -> Any:
        return self._input if self._input is not None else self

    def _step(self, direction: int) -> None:
        if not self._choices:
            return
        values = [value for value, _label in self._choices]
        try:
            index = values.index(self._selection)
        except ValueError:
            index = -1 if direction > 0 else 0
        self._selection = values[(index + direction) % len(values)]
        self._refresh_view()
        self._notify()

    def _notify(self) -> None:
        if self.on_changed is not None:
            self.on_changed(self)

    def _note_focus(self) -> None:
        note = getattr(self.screen, "note_focused_row", None)
        if note is not None:
            note(self)

    def on_focus(self, event: Any) -> None:
        self._note_focus()

    def on_descendant_focus(self, event: Any) -> None:
        self._note_focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self._silent is not None and event.value == self._silent:
            self._silent = None
            return
        self._notify()

    def on_key(self, event: Any) -> None:
        key = str(event.key)
        if self.kind == "boolean":
            if key == "space":
                self._flag = not self._flag
            elif key == "left":
                self._flag = False
            elif key == "right":
                self._flag = True
            else:
                return
            self._refresh_view()
            self._notify()
            event.stop()
        elif self.kind == "select" and key in {"left", "right"}:
            self._step(1 if key == "right" else -1)
            event.stop()


class _FormMixin:
    """Shared row list, navigation, and submit wiring for form-style dialogs."""

    _rows: list[_FieldRow]
    _rows_by_name: dict[str, _FieldRow]
    _active: _FieldRow | None
    _help: Static
    _error: Static
    _on_change: Callable[[_FieldRow, dict[str, _FieldRow]], None] | None
    accept_label: str = "save"

    def _setup_rows(
        self,
        fields: list[dict[str, Any]],
        on_change: Callable[[_FieldRow, dict[str, _FieldRow]], None] | None = None,
    ) -> None:
        self._rows = []
        self._rows_by_name = {}
        self._active = None
        self._on_change = on_change
        self._help = Static("", markup=False, classes="field-focus-help")
        for field in fields:
            row = _FieldRow(field)
            row.on_changed = self._row_changed
            self._rows.append(row)
            self._rows_by_name[row.field_name] = row

    def _row_changed(self, row: _FieldRow) -> None:
        if self._on_change is not None:
            self._on_change(row, self._rows_by_name)

    def form_hint(self) -> str:
        return f"↑/↓ move · ←/→ change · Enter {self.accept_label} · Esc cancel"

    def form_body(self) -> ComposeResult:
        """Rows, validation error, focused-field help, and the key hint."""
        with VerticalScroll(classes="form-scroll"):
            if not self._rows:
                yield Static(
                    "This schema has no editable fields.",
                    markup=False,
                    classes="dialog-help",
                )
            for row in self._rows:
                yield row
        yield self._error
        yield self._help
        yield Static(self.form_hint(), markup=False, classes="dialog-hint")

    def on_mount(self) -> None:
        self.focus_row(0)

    def note_focused_row(self, row: _FieldRow) -> None:
        self._active = row
        self._help.update(str(row.field.get("description") or ""))

    def focus_row(self, index: int) -> None:
        if not self._rows:
            return
        index = max(0, min(index, len(self._rows) - 1))
        self._rows[index].focus_target().focus()

    def action_focus_next(self) -> None:
        self._move_row(1)

    def action_focus_previous(self) -> None:
        self._move_row(-1)

    def _move_row(self, delta: int) -> None:
        if not self._rows:
            return
        index = self._rows.index(self._active) if self._active in self._rows else 0
        self.focus_row(index + delta)


class FormDialog(_FormMixin, _DialogBase):
    """Schema-driven form: arrow between rows, edit inline, Enter saves."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        *_FORM_BINDINGS,
    ]

    def __init__(
        self,
        title: str,
        subtitle: str,
        fields: list[dict[str, Any]],
        *,
        accept_label: str = "Apply",
        explanation: str = "",
        current: dict[str, Any] | None = None,
        validate: Callable[[dict[str, Any]], str | None] | None = None,
        on_change: Callable[[_FieldRow, dict[str, _FieldRow]], None] | None = None,
    ) -> None:
        """Optionally validate collected values, returning an error to keep the form open."""
        super().__init__(title, subtitle)
        self.fields = [
            {**field, "default": current[field["name"]]}
            if current and field["name"] in current
            else field
            for field in fields
        ]
        self.accept_label = accept_label.lower()
        self.explanation = explanation
        self._validate = validate
        self._setup_rows(self.fields, on_change)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            if self.explanation:
                yield Static(self.explanation, markup=False, classes="dialog-help")
            yield from self.form_body()

    def action_accept_form(self) -> None:
        values, error = _collect_fields(self.fields, self._rows_by_name)
        if not error and values is not None and self._validate is not None:
            error = self._validate(values)
        if error:
            self.show_error(error)
            return
        self.dismiss(values)


def _collect_fields(
    fields: list[dict[str, Any]], rows: dict[str, _FieldRow]
) -> tuple[dict[str, Any] | None, str | None]:
    values: dict[str, Any] = {}
    for field in fields:
        row = rows[field["name"]]
        try:
            value = coerce_value(field, row.value())
        except (TypeError, ValueError):
            return None, f"{field['title']} needs a valid {field.get('kind')} value."
        if field.get("required") and (
            value is None or value == [] or (isinstance(value, str) and not value.strip())
        ):
            return None, f"{field['title']} is required."
        if value not in (None, ""):
            values[field["name"]] = value
    return values, None


class ElicitationDialog(_FormMixin, _DialogBase):
    """Structured elicitation from an agent: form mode or URL mode."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        *_FORM_BINDINGS,
        Binding("d", "decline", "Decline", show=False),
    ]

    accept_label = "accept"

    def __init__(self, request: ElicitationRequest) -> None:
        super().__init__("Agent question", request.message)
        self.request = request
        self._fields = build_form_fields(request.schema, {}) if request.kind == "form" else []
        self._setup_rows(self._fields)

    def form_hint(self) -> str:
        return "↑/↓ move · ←/→ change · Enter accept · d decline · Esc cancel"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            if self.request.kind == "url":
                yield Static(
                    "The agent asks you to complete an external step. This client does not open a "
                    "browser; open the address yourself if you accept.",
                    markup=False,
                    classes="dialog-help",
                )
                yield Static(
                    self.request.url or "(no URL supplied)", markup=False, classes="url-box"
                )
                yield self._error
                yield Static(self.form_hint(), markup=False, classes="dialog-hint")
            else:
                yield Static(
                    "Answer the fields the agent requested. Required fields are marked *.",
                    markup=False,
                    classes="dialog-help",
                )
                yield from self.form_body()

    def action_decline(self) -> None:
        self.dismiss(ElicitationAnswer(action="decline"))

    def action_accept_form(self) -> None:
        if self.request.kind == "url":
            self.dismiss(ElicitationAnswer(action="accept"))
            return
        values, error = _collect_fields(self._fields, self._rows_by_name)
        if error:
            self.show_error(error)
            return
        self.dismiss(ElicitationAnswer(action="accept", content=values or {}))


class ConfirmDialog(_DialogBase):
    """Yes/no confirmation answered from the keyboard."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("y", "yes", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        Binding("enter", "yes", "Yes", show=False, priority=True),
    ]

    def __init__(self, title: str, message: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__(title, message)
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            yield self._error
            yield Static(
                f"y or Enter {self.confirm_label.lower()} · n or Esc cancel",
                markup=False,
                classes="dialog-hint",
            )

    def action_yes(self) -> None:
        self.dismiss(True)


class ChoiceDialog(_DialogBase):
    """Pick one value from a list (model, mode, config select)."""

    def __init__(
        self,
        title: str,
        subtitle: str,
        choices: list[dict[str, str]],
        *,
        current: str | None = None,
    ) -> None:
        super().__init__(title, subtitle)
        self.choices = choices
        self.current = current
        self._list = OptionList(id="choice-list", markup=False)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            for index, choice in enumerate(self.choices):
                current = "  · current" if choice.get("value") == self.current else ""
                text = f"{choice.get('name', choice.get('value', ''))}{current}"
                if choice.get("description"):
                    text += f"\n   {choice['description']}"
                self._list.add_option(Option(text, id=choice.get("value", "")))
            yield self._list
            yield self._error
            yield Static("Esc close", markup=False, classes="dialog-hint")

    def on_mount(self) -> None:
        self._list.focus()
        if self.choices:
            index = next(
                (i for i, choice in enumerate(self.choices) if choice.get("value") == self.current),
                0,
            )
            self._list.highlighted = index

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)


class TranscriptSearchDialog(_DialogBase):
    """Search retained conversation messages and return the chosen message index."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("down", "focus_results", "Results", show=False),
    ]

    def __init__(self, entries: list[tuple[str, str]], query: str = "") -> None:
        super().__init__(
            "Find in conversation", "Searches messages currently retained in the view."
        )
        self.entries = entries
        self._filter = Input(value=query, placeholder="Find text…", id="transcript-filter")
        self._list = OptionList(id="transcript-results", markup=False)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield from self.dialog_header()
            yield self._filter
            yield self._list
            yield self._error
            yield Static("↓ results · Enter jump · Esc close", classes="dialog-hint")

    def on_mount(self) -> None:
        self._filter.focus()
        self._render_results()

    def _render_results(self) -> None:
        self._list.clear_options()
        needle = self._filter.value.casefold()
        count = 0
        for index, (role, text) in enumerate(self.entries):
            location = text.casefold().find(needle)
            if location < 0:
                continue
            start = max(0, location - 35)
            snippet = " ".join(text[start : start + 160].split())
            self._list.add_option(Option(f"{role}: {snippet}", id=str(index)))
            count += 1
        self._error.update(f"{count} matching messages")
        if count:
            self._list.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._render_results()

    def action_focus_results(self) -> None:
        self._list.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self._list.option_count:
            option = self._list.get_option_at_index(self._list.highlighted or 0)
            self.dismiss(int(str(option.id)))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_id is not None:
            self.dismiss(int(event.option_id))


class CommandPaletteDialog(_DialogBase):
    """Local commands and agent-advertised commands in separate sections."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("down", "focus_list", "Next", show=False),
    ]

    def __init__(
        self,
        local_commands: Sequence[LocalCommand],
        agent_commands: list[dict[str, Any]],
        *,
        collision_names: set[str] | None = None,
        plugin_commands: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            "Commands",
            "Local commands run in the terminal. Plugin commands are user-only; agent "
            "commands are sent to the agent.",
        )
        self.local_commands = local_commands
        self.agent_commands = agent_commands
        self.plugin_commands = plugin_commands or []
        self.collisions = collision_names or set()
        self._filter = Input(placeholder="Filter commands", id="command-filter")
        self._list = OptionList(id="command-list", markup=False)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield from self.dialog_header()
            yield self._filter
            yield self._list
            yield Static(
                "↑/↓ move · /agent NAME for agent collisions",
                markup=False,
                classes="dialog-hint",
            )

    def on_mount(self) -> None:
        self._filter.focus()
        self._rebuild("")

    def on_input_changed(self, event: Input.Changed) -> None:
        self._rebuild(event.value)

    def _rebuild(self, needle: str) -> None:
        needle = needle.strip().lower()
        self._list.clear_options()
        local = [
            command
            for command in self.local_commands
            if not command.hidden and command.matches(needle)
        ]
        if local:
            self._list.add_option(Option("Local commands", id="__local_header", disabled=True))
            for command in local:
                self._list.add_option(
                    Option(command_suggestion_text(command), id=f"local:{command.name}")
                )
        plugins = [
            command
            for command in self.plugin_commands
            if needle in str(command.get("display", "")).lower()
            or needle in str(command.get("description", "")).lower()
        ]
        if plugins:
            self._list.add_option(Option("Plugin commands", id="__plugin_header", disabled=True))
            for command in plugins[:100]:
                self._list.add_option(
                    Option(
                        f"/{command.get('display', '')}   {command.get('description', '')}".rstrip(),
                        id=f"plugin:{command.get('display', '')}",
                    )
                )
        agent = [
            command
            for command in self.agent_commands
            if needle in str(command.get("name", "")).lower()
            or needle in str(command.get("description", "")).lower()
        ]
        if agent:
            self._list.add_option(Option("Agent commands", id="__agent_header", disabled=True))
            for command in agent[:100]:
                name = str(command.get("name", ""))
                description = str(command.get("description", ""))
                suffix = "   (use /agent NAME on collision)" if name in self.collisions else ""
                self._list.add_option(
                    Option(f"/{name}   {description}{suffix}", id=f"agent:{name}")
                )
        if not local and not agent and not plugins:
            self._list.add_option(Option("No matching commands", id="__none", disabled=True))
        if self._list.option_count:
            self._list.highlighted = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        index = self._list.highlighted
        if index is not None and 0 <= index < self._list.option_count:
            option = self._list.get_option_at_index(index)
            if option.id and not option.disabled:
                self.dismiss(str(option.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id and not str(event.option_id).startswith("__"):
            self.dismiss(event.option_id)

    def action_focus_list(self) -> None:
        self._list.focus()


class InfoDialog(_DialogBase):
    """Read-only Markdown overview with a single close action."""

    def __init__(self, title: str, body: str, subtitle: str = "") -> None:
        super().__init__(title, subtitle)
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield from self.dialog_header()
            yield Markdown(self.body)
            yield Static("Esc close", markup=False, classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one(Vertical).focus()


class HelpDialog(_DialogBase):
    """Keyboard map, local commands and agent commands."""

    def __init__(self, body: str) -> None:
        super().__init__("Help")
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield from self.dialog_header()
            yield Markdown(self.body)
            yield Static("Esc close", markup=False, classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one(Vertical).focus()


class SettingsDialog(_FormMixin, _DialogBase):
    """Terminal preferences; applies immediately and can be persisted."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        *_FORM_BINDINGS,
    ]

    accept_label = "apply"

    def __init__(self, settings: Settings, *, can_persist: bool) -> None:
        super().__init__("Settings", "These preferences live in the client profile file.")
        self.settings = settings
        self.can_persist = can_persist
        themes = ("harness", "textual-dark", "textual-light", "ansi-dark", "ansi-light")
        permission_labels = {
            "ask": "ask every time",
            "reads": "allow reads, ask for the rest",
            "all": "allow all tools",
        }
        self._setup_rows(
            [
                {
                    "name": "theme",
                    "title": "Theme",
                    "kind": "select",
                    "choices": [(name, name) for name in themes],
                    "default": settings.theme if settings.theme in themes else "harness",
                },
                {
                    "name": "expand",
                    "title": "Expand tool details",
                    "kind": "boolean",
                    "default": settings.auto_expand_tools,
                },
                {
                    "name": "thoughts",
                    "title": "Expand agent reasoning",
                    "kind": "boolean",
                    "default": settings.show_thoughts,
                },
                {
                    "name": "permission",
                    "title": "Tool approvals",
                    "kind": "select",
                    "choices": [(mode, permission_labels[mode]) for mode in PERMISSION_MODES],
                    "default": settings.permission_mode
                    if settings.permission_mode in PERMISSION_MODES
                    else "ask",
                    "description": (
                        "Ask shows every request. Allow reads auto-approves read, search and "
                        "think calls. Allow all is explicit and is not a sandbox."
                    ),
                },
            ]
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            yield Static(
                "Saved to the profile file when it exists."
                if self.can_persist
                else "No profile file yet; changes apply for this run only.",
                markup=False,
                classes="dialog-help",
            )
            yield from self.form_body()

    def action_accept_form(self) -> None:
        rows = self._rows_by_name
        self.dismiss(
            Settings(
                theme=str(rows["theme"].value()),
                auto_expand_tools=bool(rows["expand"].value()),
                show_thoughts=bool(rows["thoughts"].value()),
                permission_mode=str(rows["permission"].value()),
            )
        )
