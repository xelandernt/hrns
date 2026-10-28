"""Model, reasoning effort, mode, and standard config-option workflow."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from hrns.config import (
    EFFORT_DEFAULT,
    LocalSettings,
    thinking_from_settings,
    with_thinking,
)

from ...errors import HrnsClientError
from ...local import (
    is_local_hrns,
    local_credential_available,
    local_model_catalog,
    local_target,
    local_thinking_support,
)
from ..choice_keys import stepped_value
from ..dialogs import ChoiceDialog
from ..local import (
    CustomModelDialog,
    CustomModelRequest,
    LocalModelResult,
    ModelPick,
    ModelPickerDialog,
    effort_choices,
    effort_label,
    save_custom_model,
)
from ..panels import SidePanel
from ..providers import ProvidersDialog, ProvidersResult
from ..state import config_option_value, effort_option, model_option, option_values

if TYPE_CHECKING:
    from .screen import ChatScreen


class ModelWorkflow:
    """Owns model/effort selection and standard ACP option edits."""

    def __init__(self, screen: ChatScreen) -> None:
        self.screen = screen

    # ------------------------------------------------------ standard options

    def step_standard_option(self, option_id: str, direction: int) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        option = panel.option(option_id) or {}
        if option.get("type") == "boolean":
            screen._change_standard_option(option_id)
            return
        choices = option_values(option)
        if not choices:
            panel.show_message(f"{option.get('name', option_id)} has no selectable values.")
            return
        values = [choice["value"] for choice in choices]
        current = option.get("currentValue")
        value = stepped_value(values, None if current is None else str(current), direction)
        if value is not None:
            screen._set_standard_value(option_id, value)

    async def change_standard_option(self, option_id: str) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        option = panel.option(option_id) or {}
        if option.get("type") == "boolean":
            try:
                options = await screen.session.set_config_option(
                    option_id, not bool(option.get("currentValue"))
                )
            except HrnsClientError as exc:
                panel.show_message(exc.message)
                return
            panel.set_standard_options(list(options))
            screen.chrome.update_chrome()
            return
        choices = option_values(option)
        if not choices:
            panel.show_message(f"{option.get('name', option_id)} has no selectable values.")
            return
        current = option.get("currentValue")

        def apply(value: str | None) -> None:
            if value is not None:
                screen._set_standard_value(option_id, value)

        screen.app.push_screen(
            ChoiceDialog(
                str(option.get("name") or option_id),
                str(option.get("description") or ""),
                choices,
                current=str(current) if current is not None else None,
            ),
            apply,
        )

    async def set_standard_value(self, option_id: str, value: str) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel).capabilities
        try:
            options = await screen.session.set_config_option(option_id, value)
        except HrnsClientError as exc:
            panel.show_message(exc.message)
            return
        panel.set_standard_options(list(options))
        screen.chrome.update_chrome()

    # ------------------------------------------------------------- local model

    def local_settings(self) -> LocalSettings:
        screen = self.screen
        target = screen.target
        args = (
            target.profile.args
            if is_local_hrns(target.profile if target is not None else None)
            else []
        )
        config = Path(args[1]) if len(args) == 2 and args[0] == "--config" else None
        return LocalSettings(config_file=config) if config else LocalSettings()

    async def show_models(self) -> None:
        """Every configured provider's models, in one list."""
        screen = self.screen
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        if screen.target.kind != "local" or not is_local_hrns(screen.target.profile):
            await self.show_model_dialog()
            return
        try:
            settings = self.local_settings()
        except (ValueError, OSError) as exc:
            await screen._notice(f"Local settings are unavailable: {exc}", "error")
            return
        if not settings.providers:
            await screen._notice(
                "No local providers are configured yet. Add one, then pick a model.",
                "warning",
            )
            self.open_providers(settings)
            return
        screen.app.push_screen(
            ModelPickerDialog(settings),
            lambda result: self.models_picked(settings, result),
        )

    def models_picked(
        self, settings: LocalSettings, result: ModelPick | CustomModelRequest | None
    ) -> None:
        """React to the model picker: switch model, or add a custom id first."""
        if isinstance(result, ModelPick):
            self.select_local_model(settings, result.provider, result.model)
        elif isinstance(result, CustomModelRequest):
            self.add_custom_model(settings, result.provider, result.model)

    def open_providers(self, settings: LocalSettings | None = None) -> None:
        """Open the provider table, reloading the settings when none were passed."""
        screen = self.screen
        try:
            current = settings if settings is not None else self.local_settings()
        except (ValueError, OSError) as exc:
            screen.app.notify(f"Local settings are unavailable: {exc}", severity="error")
            return
        screen.app.push_screen(ProvidersDialog(current), self.providers_saved)

    def providers_saved(self, result: Any) -> None:
        screen = self.screen
        if not isinstance(result, ProvidersResult):
            return
        if result.cleared:
            screen.app.notify(
                f"{result.path} no longer selects a provider. Pick a model with /models "
                "before the next message.",
                severity="warning",
            )
            return
        if result.changed:
            screen.app.notify(f"Providers saved to {result.path}")

    def add_custom_model(self, settings: LocalSettings, provider: str, initial: str = "") -> None:
        """Remember an id the catalog does not list, then switch to it."""
        screen = self.screen
        provider_id = settings.provider_id(provider)

        def apply(values: Any) -> None:
            if not isinstance(values, dict):
                return
            model = str(values.get("model") or "").strip()
            if not model:
                return
            try:
                _path, updated = save_custom_model(settings, provider, model)
            except (ValueError, OSError) as exc:
                screen.app.notify(f"Could not add the model: {exc}", severity="error")
                return
            screen.app.notify(f"Added {provider}:{model}")
            self.select_local_model(updated, provider, model)

        screen.app.push_screen(CustomModelDialog(provider, provider_id, initial), apply)

    def select_local_model(self, settings: LocalSettings, provider: str, model: str) -> None:
        """Model choice first; prompt for effort when that model can reason.

        Canceling the effort step cancels the whole change, matching Codex.
        """
        screen = self.screen
        provider_id = settings.provider_id(provider)
        support = local_thinking_support(provider_id, model)
        if support is None:
            # A model outside the catalog has unknown support: keep a manually
            # configured thinking level instead of clearing it.
            known = model in local_model_catalog().get(provider_id, ())
            screen._save_model_selection_worker(
                settings, provider, model, None, touch_thinking=known
            )
            return
        current = (
            thinking_from_settings(settings)
            if (provider, model) == (settings.provider, settings.model)
            else None
        )
        self.choose_effort(
            settings,
            provider,
            model,
            current=current,
            on_select=lambda effort: screen._save_model_selection_worker(
                settings, provider, model, effort
            ),
        )

    def choose_effort(
        self,
        settings: LocalSettings,
        provider: str,
        model: str,
        *,
        current: str | None,
        on_select: Any,
    ) -> None:
        self.screen.app.push_screen(
            ChoiceDialog(
                "Reasoning effort",
                f"{provider}:{model}. Applies to the next message.",
                effort_choices(settings.provider_id(provider), model),
                current=current or EFFORT_DEFAULT,
            ),
            lambda value: on_select(value) if value else None,
        )

    async def save_model_selection(
        self,
        settings: LocalSettings,
        provider: str,
        model: str,
        effort: str | None,
        *,
        touch_thinking: bool = True,
    ) -> None:
        screen = self.screen
        entry = settings.entry(provider)
        if entry is not None and not local_credential_available(provider, entry.api_key_env):
            await screen._notice(
                f"{provider} has no API key yet. Add one in /providers, or press Ctrl+G there "
                "to sign in.",
                "error",
            )
            return
        try:
            configured = settings.model_copy(update={"provider": provider, "model": model})
            if touch_thinking:
                configured = with_thinking(configured, effort)
            path = configured.save(settings.config_file)
        except (ValueError, OSError) as exc:
            await screen._notice(f"Could not save the model selection: {exc}", "error")
            return
        await self.apply_model_settings(
            LocalModelResult(
                path=path,
                provider=provider,
                model=model,
                api_key_ref=None,
                api_key_changed=False,
                effort=effort,
            )
        )

    async def show_effort(self) -> None:
        screen = self.screen
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        if screen.target.kind != "local" or not is_local_hrns(screen.target.profile):
            await screen._notice(
                "Reasoning effort is only available for local models.",
                "warning",
            )
            return
        try:
            settings = self.local_settings()
        except (ValueError, OSError) as exc:
            await screen._notice(f"Local settings are unavailable: {exc}", "error")
            return
        if not settings.provider or not settings.model:
            await screen._notice(
                "Configure a local model before setting reasoning effort.", "error"
            )
            return
        provider_id = settings.provider_id()
        support = local_thinking_support(provider_id, settings.model)
        if support is None:
            await screen._notice(
                f"{settings.provider}:{settings.model} does not advertise reasoning effort.",
                "warning",
            )
            return
        option = effort_option(screen.session.config_options)
        current = (
            str(option.get("currentValue"))
            if option is not None
            else (thinking_from_settings(settings) or EFFORT_DEFAULT)
        )
        self.choose_effort(
            settings,
            settings.provider,
            settings.model,
            current=current,
            on_select=lambda effort: screen._set_local_effort_worker(settings, effort),
        )

    async def set_local_effort(self, settings: LocalSettings, effort: str) -> None:
        screen = self.screen
        try:
            configured = with_thinking(settings, effort)
            path = configured.save(settings.config_file)
        except (ValueError, OSError) as exc:
            await screen._notice(f"Could not save the reasoning effort: {exc}", "error")
            return
        await self.apply_model_settings(
            LocalModelResult(
                path=path,
                provider=settings.provider,
                model=settings.model,
                api_key_ref=None,
                api_key_changed=False,
                effort=effort,
            )
        )

    async def apply_model_settings(self, result: LocalModelResult) -> None:
        """Switch the session's model in place; restart only when it cannot be switched.

        The runtime resolves ``name:model`` ids itself, so a provider change is a config
        option change like any other. The restart path stays for a runtime that started
        before the selected id existed, and the effort fallback keeps its restart.
        """
        screen = self.screen
        wanted = f"{result.provider}:{result.model}"
        option = model_option(screen.session.config_options)
        restart = option is None
        if option is not None:
            option_id = str(option.get("id") or "model")
            advertised = {item["value"] for item in option_values(option)}
            try:
                options = await screen.session.set_config_option(option_id, wanted)
            except HrnsClientError as exc:
                if wanted in advertised:
                    await screen._notice(f"Model change failed: {exc.message}", "error")
                    return
                # The running runtime never advertised this id: reload it instead.
                restart = True
            else:
                screen.session.remember(options)
                if result.effort is not None:
                    if effort_option(options) is not None:
                        try:
                            options = await screen.session.set_config_option(
                                "effort", result.effort
                            )
                        except HrnsClientError as exc:
                            await screen._notice(
                                f"Reasoning effort change failed: {exc.message}", "error"
                            )
                            return
                        screen.session.remember(options)
                    elif result.effort != EFFORT_DEFAULT:
                        # The new model may not have advertised effort until the switch
                        # landed; restart so the saved setting is read at startup.
                        restart = True
                if not restart:
                    screen.chrome.update_chrome()
                    suffix = (
                        f" · effort {effort_label(result.effort)}"
                        if result.effort is not None
                        else ""
                    )
                    await screen._notice(f"Model: {wanted}{suffix}", "success")
                    return
        try:
            target = local_target(
                result.path,
                api_key_ref=result.api_key_ref,
                cwd=screen.target.cwd,
            )
        except HrnsClientError as exc:
            await screen._notice(
                f"Saved the configuration, but it cannot be used yet: {exc.message}", "error"
            )
            return
        screen.app.target = target
        screen.target = target
        await screen._notice(
            "Restarting hrns-local so it advertises the saved model; reloading this session "
            "afterwards.",
            "warning",
        )
        await screen._reconnect()

    async def show_model_dialog(self) -> None:
        screen = self.screen
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        option = model_option(screen.session.config_options)
        if option:
            values = option_values(option)
            if values:

                def apply(value: str | None) -> None:
                    if value is not None:
                        screen._set_standard_value(option.get("id"), value)

                reasoning = effort_option(screen.session.config_options)
                subtitle = "Model selection is a standard ACP session config option."
                if reasoning:
                    subtitle = (
                        "Model selection is a standard ACP session config option. "
                        f"Current effort: {config_option_value(reasoning)}."
                    )
                screen.app.push_screen(
                    ChoiceDialog(
                        "Model",
                        subtitle,
                        values,
                        current=str(option.get("currentValue")),
                    ),
                    lambda value: apply(value),
                )
                return
        modes = (screen.session.modes or {}).get("availableModes") or []
        if modes:
            choices = [
                {
                    "value": str(mode.get("id", "")),
                    "name": str(mode.get("name", "")),
                    "description": str(mode.get("description") or ""),
                }
                for mode in modes
            ]

            def apply_mode(value: str | None) -> None:
                if value:
                    screen._set_mode(value)

            screen.app.push_screen(
                ChoiceDialog(
                    "Mode",
                    "The agent advertises session modes instead of a model option.",
                    choices,
                    current=screen.session.current_mode_id,
                ),
                lambda value: apply_mode(value),
            )
            return
        await screen._notice(
            "This agent advertises neither a model config option nor session modes.", "warning"
        )

    async def set_mode(self, mode_id: str) -> None:
        screen = self.screen
        try:
            await screen.session.set_mode(mode_id)
        except HrnsClientError as exc:
            await screen._notice(f"Mode change failed: {exc.message}", "error")
            return
        screen.chrome.update_chrome()
        await screen._notice(f"Mode set to {mode_id}.", "success")
