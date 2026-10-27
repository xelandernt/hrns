"""Edit the local model settings with selectable Pydantic AI providers and models."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual.binding import Binding

from hrns.config import (
    EFFORT_DEFAULT,
    EFFORT_OFF,
    GITHUB_COPILOT_CLIENT_ID,
    THINKING_LEVELS,
    LocalSettings,
    keyring_account,
    thinking_from_settings,
    with_thinking,
)

from ..local import (
    local_login_providers,
    local_model_catalog,
    local_thinking_support,
    store_api_key,
    stored_api_key_ref,
)
from ..login import LoginResult, LoginTarget
from .dialogs import FormDialog, _collect_fields
from .login import DeviceLoginDialog


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


#: Used only when ``hrns-local`` is unavailable; the runtime normally supplies
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
    placeholder: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "kind": kind,
        "choices": choices or [],
        "required": False,
        "default": default,
        "placeholder": placeholder,
    }


class LocalModelDialog(FormDialog):
    """Choose a provider and model by typing, either from the known ids or free-form."""

    BINDINGS = [
        *FormDialog.BINDINGS,
        Binding("ctrl+g", "sign_in", "Sign in", show=False, priority=True),
    ]

    def __init__(self, settings: LocalSettings, *, accept_label: str = "Save and connect") -> None:
        self.settings = settings
        self.catalog = local_model_catalog()
        self._model_choices: dict[str, tuple[str, ...]] = dict(self.catalog)
        self._login_providers = local_login_providers()

        providers = list(self.catalog) or list(_FALLBACK_PROVIDERS)
        current_provider = settings.provider
        if current_provider and current_provider not in providers:
            providers.insert(0, current_provider)

        model_options = self._model_options(current_provider)

        fields = [
            _field(
                "provider",
                "Provider",
                description=(
                    "Known Pydantic AI providers. Type to filter; any other id is used as-is."
                ),
                kind="combobox",
                default=current_provider,
                choices=[(provider, provider) for provider in providers],
            ),
            _field(
                "model",
                "Model",
                description=self._model_description(current_provider),
                kind="combobox",
                default=settings.model,
                choices=model_options,
                placeholder=self._model_placeholder(current_provider),
            ),
            _field(
                "copilot_client_id",
                "Copilot client ID",
                description=(
                    "GitHub OAuth app used by Ctrl+G. GitHub Copilot's public client lists the "
                    "current catalog; a token from another app only sees legacy model ids."
                ),
                default=settings.copilot_client_id or GITHUB_COPILOT_CLIENT_ID,
                placeholder=GITHUB_COPILOT_CLIENT_ID,
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
            explanation=self._explanation(),
            on_change=self._field_changed,
        )
        self._show_copilot_client(current_provider)

    def _show_copilot_client(self, provider: str) -> None:
        """Only the account-login provider has an OAuth client to choose."""
        row = self._rows_by_name.get("copilot_client_id")
        if row is not None:
            row.display = provider == "github-copilot"

    def _explanation(self) -> str:
        text = (
            "Runs file tools and commands on this machine, with approval prompts. "
            "HRNS_LOCAL_* environment variables override saved settings."
        )
        if not self.catalog:
            text += (
                " The hrns-local catalog could not be read, so no provider or model ids "
                "are suggested; type them directly."
            )
        return text

    def form_hint(self) -> str:
        hint = super().form_hint()
        if not self._login_providers:
            return hint
        return f"{hint} · Ctrl+G sign in"

    def _form_value(self, name: str) -> str:
        row = self._rows_by_name.get(name)
        return str(row.value() or "").strip() if row is not None else ""

    def action_sign_in(self) -> None:
        """Start an account login for the provider typed in the form."""
        provider = self._form_value("provider")
        if provider not in self._login_providers:
            known = ", ".join(sorted(self._login_providers)) or "none"
            self.show_error(
                f"{provider or 'This provider'} has no account login. Providers with one: {known}."
            )
            return
        api_key_env = self._form_value("api_key_env")
        if provider != self.settings.provider:
            # Endpoint settings typed here belong to this provider only once saved.
            api_key_env = ""
        self.app.push_screen(
            DeviceLoginDialog(
                LoginTarget(
                    provider,
                    keyring_account(provider, api_key_env),
                    client_id=self._form_value("copilot_client_id"),
                )
            ),
            self._signed_in,
        )

    def _signed_in(self, result: LoginResult | None) -> None:
        if result is None:
            return
        self.show_error("")
        if result.models:
            # The provider just told us which ids this account may use: offer them now
            # instead of making the user type an id from memory.
            self._model_choices[result.provider] = result.models
            self._refresh_models(result.provider)
        self._help.update(
            f"Signed in to {result.provider}. Token stored in the OS keyring; "
            "pick a model and save."
        )

    def _models_for(self, provider: str) -> tuple[str, ...]:
        return tuple(self._model_choices.get(provider, ()))

    def _model_options(self, provider: str) -> list[tuple[str, str]]:
        return [(model, model) for model in self._models_for(provider)]

    def _model_description(self, provider: str) -> str:
        if self._models_for(provider):
            return f"Known models for {provider}. Type to filter; any other model id is used as-is."
        where = f" for {provider}" if provider else ""
        return f"No known model ids{where}. Type the model id to use."

    def _model_placeholder(self, provider: str) -> str:
        return "type to filter" if self._models_for(provider) else "type a model id"

    def _refresh_models(self, provider: str) -> None:
        row = self._rows_by_name.get("model")
        if row is None:
            return
        row.set_choices(self._model_options(provider))
        row.set_placeholder(self._model_placeholder(provider))
        row.field["description"] = self._model_description(provider)
        if self._active is row:
            self._help.update(self._model_description(provider))

    def _field_changed(self, row: Any, rows: dict[str, Any]) -> None:
        """Follow the provider as it is typed: retarget the model suggestions."""
        if row.field_name != "provider":
            return
        provider = str(row.value() or "").strip()
        self._refresh_models(provider)
        self._show_copilot_client(provider)
        model_row = rows.get("model")
        if model_row is None or provider == self.settings.provider:
            return
        if provider not in self._model_choices:
            # A free-form provider has no catalog to check the model against.
            return
        current = str(model_row.value() or "").strip()
        if current and current not in self._models_for(provider):
            # The old model belongs to the previous provider; start clean.
            model_row.set_value("")

    def action_accept_form(self) -> None:
        values, error = _collect_fields(self.fields, self._rows_by_name)
        if error or values is None:
            self.show_error(error or "The form has no values.")
            return
        provider = str(values.get("provider") or "").strip()
        model = str(values.get("model") or "").strip()
        copilot_client_id = str(values.pop("copilot_client_id", "") or "").strip()
        if provider != "github-copilot":
            # Keep the saved app when another provider is selected.
            copilot_client_id = self.settings.copilot_client_id
        elif copilot_client_id and not re.fullmatch(r"[A-Za-z0-9._-]+", copilot_client_id):
            self.show_error(
                "The Copilot client ID may only contain letters, digits, dots, hyphens, "
                "and underscores."
            )
            return
        values["copilot_client_id"] = copilot_client_id
        effort = str(values.pop("effort", "") or EFFORT_DEFAULT).strip()
        api_key = str(values.pop("api_key", "") or "").strip()
        api_key_env = str(values.get("api_key_env") or "").strip()
        base_url = str(values.get("base_url") or "").strip()
        if provider != self.settings.provider:
            # Endpoint settings that were inherited rather than retyped belong to
            # the previous provider; keep only what this form was changed to.
            if api_key_env == self.settings.api_key_env:
                api_key_env = ""
            if base_url == self.settings.base_url:
                base_url = ""
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
                    "A new provider needs an API key. Paste it above, name an "
                    "environment variable that already holds it, or press Ctrl+G to sign in."
                )
                return
        values["provider"] = provider
        values["model"] = model
        values["base_url"] = base_url
        values["api_key_env"] = api_key_env
        try:
            configured = LocalSettings(config_file=None, **{**self.settings.model_dump(), **values})
            configured = with_thinking(configured, effort)
            configured.require_model()
            path = configured.save(self.settings.config_file)
        except (ValueError, TypeError, OSError) as exc:
            self.show_error(str(exc))
            return
        if api_key:
            api_key_ref = (
                store_api_key(configured.provider, configured.api_key_env, api_key) or api_key
            )
        else:
            api_key_ref = stored_api_key_ref(configured.provider, configured.api_key_env)
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
