"""Modal dialogs: approvals, elicitation forms, configuration, commands, settings."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    Markdown,
    OptionList,
    Select,
    Static,
)
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
                "↑/↓ choose · 1–9 quick select · Esc cancels",
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


def _field_widget(field: dict[str, Any]) -> Any:
    """Build the input widget for one schema field."""
    kind = field.get("kind")
    default = field.get("default")
    if kind == "boolean":
        return Checkbox(field["title"], value=bool(default))
    if kind == "select":
        choices = field.get("choices") or []
        options = [(label, value) for value, label in choices]
        value = default if any(item[1] == default for item in options) else Select.NULL
        return Select(options, allow_blank=True, value=value)
    if kind == "multi":
        initial = (
            ", ".join(str(item) for item in (default or []))
            if isinstance(default, list)
            else (default or "")
        )
        return Input(str(initial), placeholder="comma-separated values")
    if kind == "password":
        return Input("" if default is None else str(default), password=True)
    input_type = "integer" if kind == "integer" else "number" if kind == "number" else "text"
    return Input("" if default is None else str(default), type=input_type)


def _raw_value(widget: Any) -> Any:
    if isinstance(widget, Checkbox):
        return widget.value
    if isinstance(widget, Select):
        return None if widget.value is Select.NULL else widget.value
    return widget.value


def _collect_fields(
    fields: list[dict[str, Any]], widgets: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    values: dict[str, Any] = {}
    for field in fields:
        widget = widgets[field["name"]]
        try:
            value = coerce_value(field, _raw_value(widget))
        except (TypeError, ValueError):
            return None, f"{field['title']} needs a valid {field.get('kind')} value."
        if field.get("required") and (
            value is None or value == [] or (isinstance(value, str) and not value.strip())
        ):
            return None, f"{field['title']} is required."
        if value not in (None, ""):
            values[field["name"]] = value
    return values, None


class FormDialog(_DialogBase):
    """Build a form and submit with Enter in an input or Ctrl+Enter anywhere."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+enter", "accept_form", "Apply", show=False, priority=True),
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
    ) -> None:
        """Optionally validate collected values, returning an error to keep the form open."""
        super().__init__(title, subtitle)
        self.fields = fields
        self.accept_label = accept_label
        self.explanation = explanation
        self.current = current or {}
        self._validate = validate
        self._widgets: dict[str, Any] = {}
        self._scroll = VerticalScroll(classes="form-scroll")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            if self.explanation:
                yield Static(self.explanation, markup=False, classes="dialog-help")
            with self._scroll:
                if not self.fields:
                    yield Static(
                        "This schema has no editable fields.", markup=False, classes="dialog-help"
                    )
                for field in self.fields:
                    if field["name"] in self.current:
                        field = {**field, "default": self.current[field["name"]]}
                    yield Label(
                        field["title"] + (" *" if field.get("required") else ""),
                        classes="field-label",
                        markup=False,
                    )
                    if field.get("description"):
                        yield Static(field["description"], markup=False, classes="field-help")
                    widget = _field_widget(field)
                    self._widgets[field["name"]] = widget
                    yield widget
            yield self._error
            with Horizontal(classes="button-row"):
                yield Button(self.accept_label, id="form-accept", variant="primary")
                yield Button("Cancel", id="form-cancel")

    def on_mount(self) -> None:
        for widget in self._widgets.values():
            if isinstance(widget, (Input, Select)):
                widget.focus()
                break

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "form-accept":
            self.action_accept_form()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_accept_form()

    def action_accept_form(self) -> None:
        values, error = _collect_fields(self.fields, self._widgets)
        if not error and values is not None and self._validate is not None:
            error = self._validate(values)
        if error:
            self.show_error(error)
            return
        self.dismiss(values)


class ElicitationDialog(_DialogBase):
    """Structured elicitation from an agent: form mode or URL mode."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+enter", "accept_elicitation", "Accept", show=False, priority=True),
    ]

    def __init__(self, request: ElicitationRequest) -> None:
        super().__init__("Agent question", request.message)
        self.request = request
        self._fields = build_form_fields(request.schema, {}) if request.kind == "form" else []
        self._widgets: dict[str, Any] = {}

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
            else:
                yield Static(
                    "Answer the fields the agent requested. Required fields are marked *.",
                    markup=False,
                    classes="dialog-help",
                )
                with VerticalScroll(classes="form-scroll"):
                    for field in self._fields:
                        yield Label(
                            field["title"] + (" *" if field.get("required") else ""),
                            classes="field-label",
                            markup=False,
                        )
                        if field.get("description"):
                            yield Static(field["description"], markup=False, classes="field-help")
                        widget = _field_widget(field)
                        self._widgets[field["name"]] = widget
                        yield widget
            yield self._error
            with Horizontal(classes="button-row"):
                yield Button("Accept", id="elicit-accept", variant="primary")
                yield Button("Decline", id="elicit-decline")
                yield Button("Cancel", id="elicit-cancel")

    def on_mount(self) -> None:
        for widget in self._widgets.values():
            if isinstance(widget, (Input, Select)):
                widget.focus()
                break

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "elicit-decline":
            self.dismiss(ElicitationAnswer(action="decline"))
        elif event.button.id == "elicit-cancel":
            self.dismiss(ElicitationAnswer(action="cancel"))
        elif event.button.id == "elicit-accept":
            self.action_accept_elicitation()

    def action_accept_elicitation(self) -> None:
        if self.request.kind == "url":
            self.dismiss(ElicitationAnswer(action="accept"))
            return
        values, error = _collect_fields(self._fields, self._widgets)
        if error:
            self.show_error(error)
            return
        self.dismiss(ElicitationAnswer(action="accept", content=values or {}))


class ConfirmDialog(_DialogBase):
    """Yes/no confirmation with explicit labels."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("y", "yes", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
    ]

    def __init__(self, title: str, message: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__(title, message)
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            yield self._error
            with Horizontal(classes="button-row"):
                yield Button(self.confirm_label, id="confirm-yes", variant="error")
                yield Button("Cancel", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

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
            yield Static("Esc closes", markup=False, classes="dialog-hint")

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
    ) -> None:
        super().__init__(
            "Commands", "Local commands run in the terminal. Agent commands are sent to the agent."
        )
        self.local_commands = local_commands
        self.agent_commands = agent_commands
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
        if not local and not agent:
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
            yield Static("Esc closes", markup=False, classes="dialog-hint")

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
            yield Static("Esc closes", markup=False, classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one(Vertical).focus()


class SettingsDialog(_DialogBase):
    """Terminal preferences; applies immediately and can be persisted."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+enter", "apply_settings", "Apply", show=False, priority=True),
    ]

    def __init__(self, settings: Settings, *, can_persist: bool) -> None:
        super().__init__("Settings", "These preferences live in the client profile file.")
        self.settings = settings
        self.can_persist = can_persist
        themes = ("harness", "textual-dark", "textual-light", "ansi-dark", "ansi-light")
        self._theme = Select(
            [(name, name) for name in themes],
            value=settings.theme if settings.theme in themes else "harness",
            allow_blank=False,
            id="settings-theme",
        )
        self._expand = Checkbox(
            "Expand tool details automatically", value=settings.auto_expand_tools
        )
        self._thoughts = Checkbox(
            "Expand agent reasoning when provided", value=settings.show_thoughts
        )
        self._panel = Checkbox("Show side panel on wide terminals", value=settings.side_panel)
        permission_labels = {
            "ask": "ask every time",
            "reads": "allow reads, ask for the rest",
            "all": "allow all tools",
        }
        self._permission = Select(
            [(permission_labels[mode], mode) for mode in PERMISSION_MODES],
            value=settings.permission_mode
            if settings.permission_mode in PERMISSION_MODES
            else "ask",
            allow_blank=False,
            id="settings-permission",
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.dialog_header()
            with VerticalScroll(classes="form-scroll"):
                yield Static("Theme", classes="field-label", markup=False)
                yield self._theme
                yield self._expand
                yield self._thoughts
                yield self._panel
                yield Static("Tool approvals", classes="field-label", markup=False)
                yield self._permission
                yield Static(
                    "Ask shows every request. Allow reads auto-approves read, search and think calls. "
                    "Allow all is explicit and is not a sandbox.",
                    markup=False,
                    classes="field-help",
                )
            yield Static(
                "Saved to the profile file when it exists."
                if self.can_persist
                else "No profile file yet; changes apply for this run only.",
                markup=False,
                classes="dialog-help",
            )
            with Horizontal(classes="button-row"):
                yield Button("Apply", id="settings-apply", variant="primary")
                yield Button("Cancel", id="settings-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-apply":
            self.action_apply_settings()
        else:
            self.dismiss(None)

    def action_apply_settings(self) -> None:
        self.dismiss(
            Settings(
                theme=str(self._theme.value),
                auto_expand_tools=self._expand.value,
                show_thoughts=self._thoughts.value,
                side_panel=self._panel.value,
                permission_mode=str(self._permission.value),
            )
        )
