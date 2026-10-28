"""Local model selection: the cross-provider picker, custom ids, and effort helpers.

Providers are configured separately in ``/providers``; this module only chooses which
``name:model`` id the session uses and remembers extra ids an entry should offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from hrns.config import (
    EFFORT_DEFAULT,
    EFFORT_OFF,
    THINKING_LEVELS,
    LocalSettings,
    ProviderEntry,
)

from ..local import local_model_catalog, local_thinking_support
from .dialogs import FormDialog, _DialogBase

__all__ = [
    "CustomModelDialog",
    "CustomModelRequest",
    "EFFORT_LABELS",
    "LocalModelResult",
    "ModelPick",
    "ModelPickerDialog",
    "effort_choices",
    "effort_label",
    "effort_values",
    "save_custom_model",
]


@dataclass(frozen=True, slots=True)
class LocalModelResult:
    """Saved local model settings plus the identity needed by the caller."""

    path: Path
    provider: str
    model: str
    api_key_ref: str | None = None
    api_key_changed: bool = False
    #: ACP/TUI effort value to apply live; ``None`` means "leave it alone".
    effort: str | None = None


@dataclass(frozen=True, slots=True)
class ModelPick:
    """One model of one provider entry, chosen in the picker."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class CustomModelRequest:
    """The picker's "add a custom model" action for one entry."""

    provider: str
    #: Suggested model id: the filter text when nothing matched, else empty.
    model: str = ""


@dataclass(frozen=True, slots=True)
class _Row:
    """What one picker row stands for: a provider header, a model, or the add action."""

    provider: str
    model: str = ""
    add: bool = False
    #: For the add action, the filter text to offer as the new model id.
    prefill: str = ""


#: Codex-style labels and one-line explanations for the unified thinking ladder.
EFFORT_LABELS: dict[str, tuple[str, str]] = {
    EFFORT_DEFAULT: ("Default", "Let the provider choose its default reasoning effort."),
    EFFORT_OFF: ("Off", "Disable reasoning when the model allows it."),
    "minimal": ("Minimal", "Fastest responses with the least reasoning."),
    "low": ("Low", "Light reasoning for straightforward work."),
    "medium": ("Medium", "Balanced reasoning for everyday work."),
    "high": ("High", "Deeper reasoning for harder work."),
    "xhigh": ("XHigh", "Maximum reasoning; highest cost and latency."),
}


def effort_label(value: str | None) -> str:
    """Display name for an effort value; ``None``/``"default"`` read as Default."""
    key = value or EFFORT_DEFAULT
    return EFFORT_LABELS.get(key, (key, ""))[0]


def effort_values(support: str | None) -> tuple[str, ...]:
    """Values offered for a model; always-on models cannot disable reasoning."""
    values = [EFFORT_DEFAULT]
    if support != "always":
        values.append(EFFORT_OFF)
    values.extend(THINKING_LEVELS)
    return tuple(values)


def effort_choices(provider_id: str, model: str) -> list[dict[str, str]]:
    """ChoiceDialog payload for the effort step; the dialog marks the current value."""
    support = local_thinking_support(provider_id, model)
    return [
        {"value": value, "name": EFFORT_LABELS[value][0], "description": EFFORT_LABELS[value][1]}
        for value in effort_values(support)
    ]


def save_custom_model(
    settings: LocalSettings, provider: str, model: str
) -> tuple[Path, LocalSettings]:
    """Remember a model id for one provider entry and return the saved settings."""
    entry = settings.require_entry(provider)
    updated = ProviderEntry(
        type=entry.type,
        base_url=entry.base_url,
        api_key_env=entry.api_key_env,
        copilot_client_id=entry.copilot_client_id,
        custom_models=[*entry.custom_models, model],
        options=dict(entry.options),
    )
    providers = {**settings.providers, provider: updated}
    configured = settings.model_copy(update={"providers": providers})
    path = configured.save(settings.config_file)
    return path, configured


class ModelPickerDialog(_DialogBase):
    """Every configured provider's models in one filterable, grouped list."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("a", "add_model", "Add custom model", show=False),
    ]

    def __init__(self, settings: LocalSettings, *, subtitle: str = "") -> None:
        super().__init__("Models", subtitle or "The selection applies to the next message.")
        self.settings = settings
        self.catalog = local_model_catalog()
        self._rows: dict[str, _Row] = {}
        self._filter = Input(placeholder="Filter providers and models…", id="model-filter")
        self._list = OptionList(id="model-options", markup=False)
        self._options: list[Option] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield from self.dialog_header()
            yield self._filter
            yield self._list
            yield self._error
            yield Static(
                "Type to filter · ↑/↓ move · Enter select · Esc close",
                markup=False,
                classes="dialog-hint",
            )

    def on_mount(self) -> None:
        self._fill("")
        self._filter.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self._filter:
            self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter takes the highlighted row, the way a picker should."""
        if event.input is not self._filter:
            return
        event.stop()
        index = self._list.highlighted
        if index is not None and 0 <= index < len(self._options):
            option = self._options[index]
            if not option.disabled:
                self._choose(str(option.id or ""))

    def on_key(self, event: events.Key) -> None:
        """Up/Down move the highlight while the filter keeps keyboard focus."""
        if event.key in ("up", "down"):
            if self._move_highlight(1 if event.key == "down" else -1):
                event.stop()
            return
        super().on_key(event)

    def _fill(self, needle: str) -> None:
        """Rebuild the grouped list; the active entry's group stays on top."""
        self._list.clear_options()
        self._rows = {}
        self._options = []
        folded = needle.strip().casefold()
        current = self.settings.active_target()
        names = sorted(
            self.settings.providers,
            key=lambda name: (name != self.settings.provider, name.casefold()),
        )
        for name in names:
            entry = self.settings.providers[name]
            models = [
                model
                for model in self.settings.models_for(name, self.catalog)
                if not folded or folded in model.casefold() or folded in name.casefold()
            ]
            # A filtered-out group is hidden; an entry with no models at all stays so
            # its add action remains reachable for a hand-typed id.
            if not models and folded:
                continue
            header_id = f"__header__{name}"
            self._options.append(
                Option(
                    f"── {name} · {entry.provider_id(name)}",
                    id=header_id,
                    disabled=True,
                )
            )
            self._rows[header_id] = _Row(name)
            for model in models:
                option_id = f"{name}:{model}"
                marker = "  · current" if option_id == current else ""
                self._options.append(Option(f"  {model}{marker}", id=option_id))
                self._rows[option_id] = _Row(name, model)
            self._add_action(name)
        if not self._options:
            self._empty_rows(needle)
        for option in self._options:
            self._list.add_option(option)
        highlighted = next(
            (
                index
                for index, option in enumerate(self._options)
                if option.id == current and not option.disabled
            ),
            None,
        )
        if highlighted is None:
            highlighted = next(
                (index for index, option in enumerate(self._options) if not option.disabled),
                None,
            )
        self._list.highlighted = highlighted

    def _add_action(
        self, provider: str, label: str = "  + Add a custom model…", *, prefill: str = ""
    ) -> None:
        option_id = f"__add__{provider}"
        self._options.append(Option(label, id=option_id))
        self._rows[option_id] = _Row(provider, add=True, prefill=prefill)

    def _empty_rows(self, needle: str) -> None:
        """Nothing matched: keep a way to turn what was typed into a custom id."""
        provider = self._highlighted_provider()
        self._options.append(Option("  no models match this filter", id="__empty__", disabled=True))
        if not provider:
            return
        wanted = needle.strip()
        label = (
            f"  + Add {wanted!r} to {provider}…"
            if wanted
            else f"  + Add a custom model to {provider}…"
        )
        self._add_action(provider, label, prefill=wanted)

    def _choose(self, option_id: str) -> None:
        row = self._rows.get(option_id)
        if row is None:
            return
        if row.add:
            self.dismiss(CustomModelRequest(provider=row.provider, model=row.prefill))
        elif row.model:
            self.dismiss(ModelPick(provider=row.provider, model=row.model))

    def _move_highlight(self, step: int) -> bool:
        total = len(self._options)
        if total == 0:
            return False
        start = self._list.highlighted or 0
        for offset in range(1, total + 1):
            index = (start + step * offset) % total
            if not self._options[index].disabled:
                self._list.highlighted = index
                return True
        return False

    def _highlighted_provider(self) -> str:
        """The entry the highlighted row belongs to; used by the add action."""
        highlighted = self._list.highlighted
        if highlighted is not None and 0 <= highlighted < len(self._options):
            row = self._rows.get(str(self._options[highlighted].id or ""))
            if row is not None:
                return row.provider
        if self.settings.provider:
            return self.settings.provider
        return next(iter(self.settings.providers), "")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._choose(str(event.option_id or ""))

    def action_add_model(self) -> None:
        provider = self._highlighted_provider()
        if not provider:
            self.show_error("Add a provider first with /providers.")
            return
        self.dismiss(CustomModelRequest(provider=provider))


class CustomModelDialog(FormDialog):
    """Type a model id that the catalog does not know for this entry."""

    def __init__(self, provider: str, provider_id: str, initial: str = "") -> None:
        fields: list[dict[str, Any]] = [
            {
                "name": "model",
                "title": "Model id",
                "description": (
                    f"The id {provider} ({provider_id}) expects, for example a self-hosted "
                    "or newly released model. It is remembered for this entry only."
                ),
                "kind": "text",
                "choices": [],
                "required": False,
                "default": "",
                "placeholder": "model id",
            }
        ]
        super().__init__(
            "Custom model",
            f"Add a model id to {provider}.",
            fields,
            accept_label="Add",
            current={"model": initial} if initial else None,
            validate=self._validate_model,
        )

    @staticmethod
    def _validate_model(values: dict[str, Any]) -> str | None:
        model = str(values.get("model") or "").strip()
        if not model:
            return "A model id is required."
        if ":" in model:
            return "A model id must not contain ':'."
        return None
