"""Edit the local model settings with selectable Pydantic AI providers and models."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual.widgets import Input, Select

from harness.config import (
    EFFORT_DEFAULT,
    EFFORT_OFF,
    THINKING_LEVELS,
    LocalSettings,
    thinking_from_settings,
    with_thinking,
)

from ..local import (
    local_model_catalog,
    local_thinking_support,
    store_api_key,
    stored_api_key_ref,
)
from .dialogs import FormDialog, _collect_fields


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


def effort_choices(provider: str, model: str) -> list[dict[str, str]]:
    """ChoiceDialog payload for the effort step; the dialog marks the current value."""
    support = local_thinking_support(provider, model)
    return [
        {"value": value, "name": EFFORT_LABELS[value][0], "description": EFFORT_LABELS[value][1]}
        for value in effort_values(support)
    ]

#: Used only when ``harness-local`` is unavailable; the runtime normally supplies
#: the provider list from the pinned Pydantic AI version.
_FALLBACK_PROVIDERS = (
    "deepseek",
    "openai",
    "openai-chat",
    "openai-responses",
    "anthropic",
    "google",
    "google-cloud",
    "ollama",
    "bedrock",
    "azure",
    "groq",
    "mistral",
    "cohere",
    "xai",
    "zai",
    "openrouter",
    "litellm",
    "vllm",
)


def _field(
    name: str,
    title: str,
    *,
    description: str = "",
    kind: str = "text",
    default: Any = "",
    choices: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "kind": kind,
        "choices": choices or [],
        "required": False,
        "default": default,
    }


class LocalModelDialog(FormDialog):
    """Choose a known provider/model or override either with free text."""

    def __init__(self, settings: LocalSettings, *, accept_label: str = "Save and connect") -> None:
        self.settings = settings
        self.catalog = local_model_catalog()
        self._model_choices: dict[str, tuple[str, ...]] = self.catalog
        self._current_provider = settings.provider

        providers = list(self.catalog) or list(_FALLBACK_PROVIDERS)
        current_provider = settings.provider
        if current_provider and current_provider not in providers:
            providers.insert(0, current_provider)
        provider_known = current_provider in providers
        provider_value = current_provider if provider_known else Select.NULL
        provider_custom = ""

        selected_provider = current_provider if provider_known else ""
        models = self._models_for(selected_provider)
        model_known = bool(settings.model) and settings.model in models
        model_value = (
            settings.model if model_known else ("" if not models else Select.NULL)
        )
        model_custom = ""
        model_options = self._model_options(selected_provider)

        fields = [
            _field(
                "provider",
                "Provider",
                description=(
                    "Known Pydantic AI providers. Use the override below for a provider "
                    "that is not listed."
                ),
                kind="select",
                default=provider_value,
                choices=[(provider, provider) for provider in providers],
            ),
            _field(
                "provider_custom",
                "Override provider (optional)",
                description="Takes precedence over the provider list.",
                default=provider_custom,
            ),
            _field(
                "model",
                "Model",
                description=(
                    "Known models for the selected provider. Use the override below for "
                    "any other model id."
                ),
                kind="select",
                default=model_value,
                choices=model_options,
            ),
            _field(
                "model_custom",
                "Override model (optional)",
                description="Takes precedence over the model list.",
                default=model_custom,
            ),
            _field(
                "effort",
                "Reasoning effort",
                description=(
                    "Unified thinking setting. Ignored by models without reasoning support; "
                    "Default leaves the provider's choice in place."
                ),
                kind="select",
                default=thinking_from_settings(settings) or EFFORT_DEFAULT,
                choices=[
                    (value, EFFORT_LABELS[value][0])
                    for value in effort_values(
                        local_thinking_support(settings.provider, settings.model)
                    )
                ],
            ),
            _field(
                "api_key",
                "API key (optional)",
                description=(
                    "Paste the key here. It is stored in your OS keyring and never written "
                    "to the TOML file. Leave blank to use the environment variable below or "
                    "the provider's default variables."
                ),
                kind="password",
            ),
            _field(
                "base_url",
                "Base URL",
                description=(
                    "Optional endpoint URL. For custom OpenAI-compatible APIs, use the "
                    "openai-chat provider."
                ),
                default=settings.base_url,
            ),
            _field(
                "api_key_env",
                "API key environment variable",
                description=(
                    "Optional variable name, for example DEEPSEEK_API_KEY. This is a name, "
                    "not the key itself."
                ),
                default=settings.api_key_env,
            ),
        ]
        super().__init__(
            "Local model",
            f"Save to {settings.config_file}",
            fields,
            accept_label=accept_label,
            explanation=(
                "Runs file tools and commands on this machine, with approval prompts. "
                "HARNESS_LOCAL_* environment variables override saved settings."
            ),
        )

    def _models_for(self, provider: str) -> tuple[str, ...]:
        models = list(self._model_choices.get(provider, ()))
        current = self.settings.model
        if provider == self.settings.provider and current and current not in models:
            # Preserve a configured custom model when its provider has no known list.
            models.insert(0, current)
        return tuple(models)

    def _model_options(self, provider: str) -> list[tuple[str, str]]:
        models = self._models_for(provider)
        if not models:
            return [("No presets — enter a model override below", "")]
        return [(model, model) for model in models]

    def _refresh_models(self, provider: str) -> None:
        select = self._widgets.get("model")
        if not isinstance(select, Select):
            return
        models = self._models_for(provider)
        current = select.value if isinstance(select.value, str) else ""
        select.set_options(self._model_options(provider))
        if current and current in models:
            select.value = current
        else:
            select.value = "" if not models else Select.NULL

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select is not self._widgets.get("provider"):
            return
        provider = event.value if isinstance(event.value, str) else ""
        if provider == self._current_provider:
            self._refresh_models(provider)
            return
        self._current_provider = provider
        self._reset_provider_fields()
        self._refresh_models(provider)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is not self._widgets.get("provider_custom"):
            return
        self._reset_provider_fields(include_custom=False)
        provider = event.value.strip()
        if not provider:
            selected = self._widgets.get("provider")
            value = selected.value if isinstance(selected, Select) else ""
            provider = value if isinstance(value, str) else ""
        self._refresh_models(provider)

    def _reset_provider_fields(self, *, include_custom: bool = True) -> None:
        names = ["model_custom", "api_key", "api_key_env", "base_url"]
        if include_custom:
            names.append("provider_custom")
        for name in names:
            widget = self._widgets.get(name)
            if isinstance(widget, Input):
                widget.value = ""

    def action_accept_form(self) -> None:
        values, error = _collect_fields(self.fields, self._widgets)
        if error:
            self.show_error(error)
            return
        provider = str(values.pop("provider_custom", "") or "").strip()
        provider = provider or str(values.get("provider") or "").strip()
        model = str(values.pop("model_custom", "") or "").strip()
        model = model or str(values.get("model") or "").strip()
        effort = str(values.pop("effort", "") or EFFORT_DEFAULT).strip()
        api_key = str(values.pop("api_key", "") or "").strip()
        api_key_env = str(values.get("api_key_env") or "").strip()
        if not provider:
            self.show_error("Provider is required.")
            return
        if not model:
            self.show_error("Model is required.")
            return
        if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
            self.show_error(
                "The API key environment variable must be a variable name such as "
                "DEEPSEEK_API_KEY, not the key itself. Paste the key into API key."
            )
            return
        if provider != self.settings.provider and not api_key:
            from_env = bool(api_key_env and os.environ.get(api_key_env))
            if not from_env and stored_api_key_ref(provider, api_key_env) is None:
                self.show_error(
                    "A new provider needs an API key. Paste it above or name an "
                    "environment variable that already holds it."
                )
                return
        values["provider"] = provider
        values["model"] = model
        values.setdefault("base_url", "")
        values.setdefault("api_key_env", "")
        try:
            configured = LocalSettings(
                config_file=None, **{**self.settings.model_dump(), **values}
            )
            configured = with_thinking(configured, effort)
            configured.require_model()
            path = configured.save(self.settings.config_file)
        except (ValueError, TypeError, OSError) as exc:
            self.show_error(str(exc))
            return
        if api_key:
            api_key_ref = (
                store_api_key(configured.provider, configured.api_key_env, api_key)
                or api_key
            )
        else:
            api_key_ref = stored_api_key_ref(
                configured.provider, configured.api_key_env
            )
        self.dismiss(
            LocalModelResult(
                path=path,
                provider=configured.provider,
                model=configured.model,
                api_key_ref=api_key_ref,
                api_key_changed=bool(api_key),
                effort=effort,
            )
        )
