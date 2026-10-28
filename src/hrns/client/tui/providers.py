"""Add, edit and delete the named local provider entries.

Providers are the persistent unit of local configuration: each entry owns its endpoint,
its keyring credential and its own custom model ids. ``/models`` then picks a model
across entries, so this module never touches the active model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from hrns.config import (
    GITHUB_COPILOT_CLIENT_ID,
    LocalSettings,
    ProviderEntry,
    ProviderNameError,
    check_provider_name,
    delete_secret,
    keyring_account,
    store_api_key,
    store_secret,
    stored_api_key_ref,
    stored_secret,
)

from ..local import local_login_providers, local_model_catalog
from ..login import LoginResult, LoginTarget
from .dialogs import ConfirmDialog, FormDialog, _collect_fields, _DialogBase
from .login import DeviceLoginDialog

__all__ = [
    "ProviderFormDialog",
    "ProvidersDialog",
    "ProvidersResult",
    "provider_choices",
    "save_entries",
]

#: Used only when ``hrns-local`` is unavailable; the runtime normally supplies the list.
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

#: The type every new entry starts from, matching the previous single-provider default.
_DEFAULT_PROVIDER = "deepseek"


@dataclass(frozen=True, slots=True)
class ProvidersResult:
    """Where the entries were saved, and whether the active selection went with them."""

    path: Path
    changed: bool = False
    cleared: bool = False


@dataclass(frozen=True, slots=True)
class SavedEntry:
    """One provider entry after a successful save in the form."""

    path: Path
    name: str


def provider_choices() -> list[tuple[str, str]]:
    """Pydantic AI provider ids the runtime knows, most useful first."""
    identifiers = list(local_model_catalog())
    for name in _FALLBACK_PROVIDERS:
        if name not in identifiers:
            identifiers.append(name)
    return [(name, name) for name in identifiers]


def save_entries(
    settings: LocalSettings,
    providers: dict[str, ProviderEntry],
    *,
    provider: str,
    model: str,
) -> tuple[Path, LocalSettings]:
    """Persist an edited provider table and return the path and reloaded settings."""
    configured = settings.model_copy(
        update={"providers": providers, "provider": provider, "model": model}
    )
    path = configured.save(settings.config_file)
    return path, configured


def _credential_status(name: str, entry: ProviderEntry) -> str:
    """One word for the secret this entry would use right now."""
    if entry.api_key_env and os.environ.get(entry.api_key_env):
        return f"env {entry.api_key_env}"
    if stored_api_key_ref(name, entry.api_key_env) is not None:
        return "keyring"
    if entry.api_key_env:
        return f"missing ({entry.api_key_env})"
    return "missing"


def _carry_secret(
    old_name: str, old_entry: ProviderEntry | None, new_name: str, new_entry: ProviderEntry
) -> None:
    """Keep a stored key when an entry is renamed to a different account."""
    old_account = keyring_account(old_name, old_entry.api_key_env if old_entry else "")
    new_account = keyring_account(new_name, new_entry.api_key_env)
    if old_account == new_account:
        return
    secret = stored_secret(old_account)
    if secret is not None:
        store_secret(new_account, secret)


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


class ProviderFormDialog(FormDialog):
    """Add or edit one named provider entry."""

    BINDINGS = [
        *FormDialog.BINDINGS,
        Binding("ctrl+g", "sign_in", "Sign in", show=False, priority=True),
    ]

    def __init__(self, settings: LocalSettings, *, current: str | None = None) -> None:
        self.settings = settings
        self.current = current
        self.path: Path | None = None
        self._login_providers = local_login_providers()
        entry = settings.entry(current) if current is not None else None
        provider_id = entry.provider_id(current) if entry is not None and current else ""
        default_type = provider_id or _DEFAULT_PROVIDER
        self._last_type = default_type
        fields = [
            _field(
                "name",
                "Name",
                description=(
                    "A unique name for this entry, for example openai-work. It prefixes the "
                    "model id the session shows, so it cannot contain a colon."
                ),
                default=current or default_type,
                placeholder=default_type,
            ),
            _field(
                "type",
                "Provider",
                description=(
                    "The Pydantic AI provider this entry talks to. Several entries may share "
                    "one provider, each with its own endpoint and key."
                ),
                kind="combobox",
                default=default_type,
                choices=provider_choices(),
            ),
            _field(
                "base_url",
                "Base URL",
                description=(
                    "Optional endpoint URL. For custom OpenAI-compatible APIs, use the "
                    "openai-chat provider."
                ),
                default=entry.base_url if entry is not None else "",
            ),
            _field(
                "api_key_env",
                "API key environment variable",
                description=(
                    "Optional variable name, for example DEEPSEEK_API_KEY. This is a name, "
                    "not the key itself. Entries that name no variable keep their own keyring "
                    "entry."
                ),
                default=entry.api_key_env if entry is not None else "",
            ),
            _field(
                "api_key",
                "API key (optional)",
                description=(
                    "Paste the key here. It is stored in your OS keyring under this entry's "
                    "name and never written to the TOML file."
                ),
                kind="password",
            ),
            _field(
                "copilot_client_id",
                "Copilot client ID",
                description=(
                    "GitHub OAuth app used by Ctrl+G. GitHub Copilot's public client lists the "
                    "current catalog; a token from another app only sees legacy model ids."
                ),
                default=(entry.copilot_client_id if entry is not None else "")
                or GITHUB_COPILOT_CLIENT_ID,
                placeholder=GITHUB_COPILOT_CLIENT_ID,
            ),
        ]
        super().__init__(
            "Edit provider" if current else "Add provider",
            f"Save to {settings.config_file}",
            fields,
            accept_label="Save",
            explanation=self._explanation(),
            on_change=self._field_changed,
        )
        self._show_copilot_client(default_type)

    def _explanation(self) -> str:
        text = (
            "An entry groups one endpoint and its credential. Models are chosen separately "
            "with /models, which can also add custom model ids to an entry."
        )
        if not local_model_catalog():
            text += (
                " The hrns-local catalog could not be read, so no provider ids are suggested; "
                "type them directly."
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

    def _show_copilot_client(self, provider_id: str) -> None:
        """Only the account-login provider has an OAuth client to choose."""
        row = self._rows_by_name.get("copilot_client_id")
        if row is not None:
            row.display = provider_id == "github-copilot"

    def _field_changed(self, row: Any, rows: dict[str, Any]) -> None:
        """Follow the provider type: rename the default name and show its OAuth client."""
        if row.field_name != "type":
            return
        provider_id = str(row.value() or "").strip()
        self._show_copilot_client(provider_id)
        name_row = rows.get("name")
        if name_row is not None and provider_id:
            current = str(name_row.value() or "").strip()
            if not current or current == self._last_type:
                name_row.set_value(provider_id)
        if provider_id:
            self._last_type = provider_id

    def action_sign_in(self) -> None:
        """Start an account login for the provider type this entry uses."""
        name = self._form_value("name") or self._form_value("type")
        provider_id = self._form_value("type") or name
        if provider_id not in self._login_providers:
            known = ", ".join(sorted(self._login_providers)) or "none"
            self.show_error(
                f"{provider_id or 'This provider'} has no account login. Providers with "
                f"one: {known}."
            )
            return
        self.app.push_screen(
            DeviceLoginDialog(
                LoginTarget(
                    provider_id,
                    keyring_account(name, self._form_value("api_key_env")),
                    client_id=self._form_value("copilot_client_id"),
                )
            ),
            self._signed_in,
        )

    def _signed_in(self, result: LoginResult | None) -> None:
        if result is None:
            return
        self.show_error("")
        self._help.update(
            f"Signed in to {result.provider}. Token stored in the OS keyring under "
            f"{result.account}; save the entry, then pick a model with /models."
        )

    def action_accept_form(self) -> None:
        values, error = _collect_fields(self.fields, self._rows_by_name)
        if error or values is None:
            self.show_error(error or "The form has no values.")
            return
        try:
            name = check_provider_name(str(values.get("name") or ""))
        except ProviderNameError as exc:
            self.show_error(str(exc))
            return
        if name != self.current and name in self.settings.providers:
            self.show_error(f"A provider named {name!r} already exists. Pick a different name.")
            return
        provider_id = str(values.get("type") or "").strip()
        api_key = str(values.pop("api_key", "") or "").strip()
        copilot_client_id = str(values.pop("copilot_client_id", "") or "").strip()
        if provider_id != "github-copilot":
            # Keep GitHub's app out of entries that never log in.
            copilot_client_id = ""
        previous = self.settings.entry(self.current) if self.current else None
        try:
            entry = ProviderEntry(
                type=provider_id if provider_id != name else "",
                base_url=str(values.get("base_url") or "").strip(),
                api_key_env=str(values.get("api_key_env") or "").strip(),
                copilot_client_id=copilot_client_id,
                custom_models=list(previous.custom_models) if previous is not None else [],
                options=dict(previous.options) if previous is not None else {},
            )
            providers = dict(self.settings.providers)
            if self.current and self.current != name:
                del providers[self.current]
            providers[name] = entry
            active = self.settings.provider
            if self.current and self.current == active and self.current != name:
                # Renaming the active entry keeps it active.
                active = name
            path, _configured = save_entries(
                self.settings,
                providers,
                provider=active,
                model=self.settings.model,
            )
        except (ValueError, TypeError, OSError) as exc:
            self.show_error(str(exc))
            return
        if api_key:
            store_api_key(name, entry.api_key_env, api_key)
        if self.current and self.current != name:
            _carry_secret(self.current, previous, name, entry)
        self.path = path
        self.dismiss(SavedEntry(path=path, name=name))


class ProvidersDialog(_DialogBase):
    """The provider table: add, edit and delete named entries."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("a", "add", "Add", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("enter", "edit", "Edit", show=False, priority=True),
        Binding("d", "delete", "Delete", show=False),
    ]

    def __init__(self, settings: LocalSettings, *, subtitle: str = "") -> None:
        super().__init__("Providers", subtitle or f"Saved to {settings.config_file}")
        self.settings = settings
        self.path: Path | None = None
        self.changed = False
        self.cleared = False
        self._names: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield from self.dialog_header()
            yield self._error
            yield DataTable(id="provider-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "↑/↓ move · a add · e edit · d delete · Esc close",
                markup=False,
                classes="dialog-hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#provider-table", DataTable)
        table.add_columns("Provider", "Type", "Base URL", "Credential", "Models")
        self._fill()
        table.focus()

    def result(self) -> ProvidersResult | None:
        if self.path is None:
            return None
        return ProvidersResult(path=self.path, changed=self.changed, cleared=self.cleared)

    def action_cancel(self) -> None:
        self.dismiss(self.result())

    def _fill(self, highlight: str | None = None) -> None:
        table = self.query_one("#provider-table", DataTable)
        table.clear()
        self._names = []
        catalog = local_model_catalog()
        for name in sorted(self.settings.providers):
            entry = self.settings.providers[name]
            marker = "●" if name == self.settings.provider else " "
            table.add_row(
                f"{marker} {name}",
                entry.provider_id(name),
                entry.base_url or "—",
                _credential_status(name, entry),
                str(len(self.settings.models_for(name, catalog))),
                key=name,
            )
            self._names.append(name)
        if not self._names:
            table.add_row("  no providers yet", "", "", "", "", key="__empty__")
        if highlight and highlight in self._names:
            table.move_cursor(row=self._names.index(highlight))

    def _reload(self, highlight: str | None = None) -> None:
        if self.path is not None:
            try:
                self.settings = LocalSettings(config_file=self.path)
            except (ValueError, OSError) as exc:
                self.show_error(str(exc))
                return
        self.show_error("")
        self._fill(highlight)

    def _focused_name(self) -> str | None:
        if not self._names:
            return None
        table = self.query_one("#provider-table", DataTable)
        index = table.cursor_row
        if 0 <= index < len(self._names):
            return self._names[index]
        return None

    def action_add(self) -> None:
        self.app.push_screen(ProviderFormDialog(self.settings), self._entry_saved)

    def action_edit(self) -> None:
        name = self._focused_name()
        if name is None:
            self.show_error("There is no provider to edit yet. Press a to add one.")
            return
        self.app.push_screen(ProviderFormDialog(self.settings, current=name), self._entry_saved)

    def action_delete(self) -> None:
        name = self._focused_name()
        if name is None:
            self.show_error("There is no provider to delete yet. Press a to add one.")
            return
        active = " This is the active provider, so the model selection is cleared."
        self.app.push_screen(
            ConfirmDialog(
                "Delete provider",
                f"Delete {name} and forget its stored key?"
                + (active if name == self.settings.provider else ""),
                confirm_label="Delete",
            ),
            lambda confirmed: self._delete(name, bool(confirmed)),
        )

    def _delete(self, name: str, confirmed: bool) -> None:
        if not confirmed:
            return
        entry = self.settings.entry(name)
        providers = {key: value for key, value in self.settings.providers.items() if key != name}
        active = self.settings.provider
        model = self.settings.model
        if name == active:
            active = ""
            model = ""
            self.cleared = True
        try:
            path, _configured = save_entries(self.settings, providers, provider=active, model=model)
        except (ValueError, TypeError, OSError) as exc:
            self.show_error(str(exc))
            return
        if entry is not None and not entry.api_key_env:
            # Nothing else shares this account, so the secret goes with the entry.
            delete_secret(keyring_account(name, ""))
        self.path = path
        self.changed = True
        self._reload()

    def _entry_saved(self, result: SavedEntry | None) -> None:
        if result is None:
            return
        self.path = result.path
        self.changed = True
        if result.name == self.settings.provider:
            self.cleared = False
        self._reload(result.name)
