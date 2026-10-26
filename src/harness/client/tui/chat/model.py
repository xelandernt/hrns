"""Model, reasoning effort, mode, and standard config-option workflow."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.config import (
    EFFORT_DEFAULT,
    LocalSettings,
    thinking_from_settings,
    with_thinking,
)

from ...errors import HarnessClientError
from ...local import local_model_catalog, local_target, local_thinking_support
from ..choice_keys import stepped_value
from ..dialogs import ChoiceDialog
from ..local import LocalModelDialog, LocalModelResult, effort_choices, effort_label
from ..panels import SidePanel
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
            except HarnessClientError as exc:
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
        except HarnessClientError as exc:
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
            if target is not None and target.profile.command == "harness-local"
            else []
        )
        config = Path(args[1]) if len(args) == 2 and args[0] == "--config" else None
        return LocalSettings(config_file=config) if config else LocalSettings()

    async def show_models(self) -> None:
        screen = self.screen
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        if screen.target.kind != "local" or screen.target.profile.command != "harness-local":
            await self.show_model_dialog()
            return
        try:
            settings = self.local_settings()
        except (ValueError, OSError) as exc:
            await screen._notice(f"Local settings are unavailable: {exc}", "error")
            return
        models = list(local_model_catalog().get(settings.provider, ()))
        if not models:
            self.open_local_model_form(settings)
            return
        if settings.model and settings.model not in models:
            models.insert(0, settings.model)
        effort = effort_label(thinking_from_settings(settings))
        current_note = (
            f"current · effort {effort}"
            if settings.model and local_thinking_support(settings.provider, settings.model)
            else "current"
        )
        choices = [
            {
                "value": model,
                "name": model,
                "description": current_note if model == settings.model else "",
            }
            for model in models
        ]
        choices.append(
            {
                "value": "__configure__",
                "name": "Configure provider or model…",
                "description": (
                    "Add a provider, paste a key, set a base URL, or type any provider or model id."
                ),
            }
        )

        def apply(value: str | None) -> None:
            if not value:
                return
            if value == "__configure__":
                self.open_local_model_form(settings)
                return
            self.select_local_model(settings, value)

        current_effort = (
            f" · effort {effort}"
            if settings.model and local_thinking_support(settings.provider, settings.model)
            else ""
        )
        screen.app.push_screen(
            ChoiceDialog(
                "Models",
                f"Provider {settings.provider}{current_effort}. "
                "The selection applies to the next message.",
                choices,
                current=settings.model,
            ),
            apply,
        )

    def open_local_model_form(self, settings: LocalSettings) -> None:
        screen = self.screen
        screen.app.push_screen(
            LocalModelDialog(settings, accept_label="Save and apply"),
            self.models_configured,
        )

    def select_local_model(self, settings: LocalSettings, model: str) -> None:
        """Model choice first; prompt for effort when that model can reason.

        Canceling the effort step cancels the whole change, matching Codex.
        """
        screen = self.screen
        support = local_thinking_support(settings.provider, model)
        if support is None:
            # A model outside the catalog has unknown support: keep a manually
            # configured thinking level instead of clearing it.
            known = model in local_model_catalog().get(settings.provider, ())
            screen._save_model_selection_worker(settings, model, None, touch_thinking=known)
            return
        current = thinking_from_settings(settings) if model == settings.model else None
        self.choose_effort(
            settings,
            model,
            current=current,
            on_select=lambda effort: screen._save_model_selection_worker(settings, model, effort),
        )

    def choose_effort(
        self,
        settings: LocalSettings,
        model: str,
        *,
        current: str | None,
        on_select: Any,
    ) -> None:
        self.screen.app.push_screen(
            ChoiceDialog(
                "Reasoning effort",
                f"{settings.provider}:{model}. Applies to the next message.",
                effort_choices(settings.provider, model),
                current=current or EFFORT_DEFAULT,
            ),
            lambda value: on_select(value) if value else None,
        )

    async def save_model_selection(
        self,
        settings: LocalSettings,
        model: str,
        effort: str | None,
        *,
        touch_thinking: bool = True,
    ) -> None:
        screen = self.screen
        try:
            configured = settings.model_copy(update={"model": model})
            if touch_thinking:
                configured = with_thinking(configured, effort)
            path = configured.save(settings.config_file)
        except (ValueError, OSError) as exc:
            await screen._notice(f"Could not save the model selection: {exc}", "error")
            return
        await self.apply_model_settings(
            LocalModelResult(
                path=path,
                provider=settings.provider,
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
        if screen.target.kind != "local" or screen.target.profile.command != "harness-local":
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
        if not settings.model:
            await screen._notice(
                "Configure a local model before setting reasoning effort.", "error"
            )
            return
        support = local_thinking_support(settings.provider, settings.model)
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

    def models_configured(self, result: Any) -> None:
        screen = self.screen
        if result is not None:
            screen._apply_model_settings_worker(result)

    async def apply_model_settings(self, result: Any) -> None:
        screen = self.screen
        if not isinstance(result, LocalModelResult):
            return
        try:
            old = self.local_settings()
        except (ValueError, OSError):
            old = None
        wanted = f"{result.provider}:{result.model}"
        option = model_option(screen.session.config_options)
        values = {item["value"] for item in option_values(option)} if option else set()
        restart = (
            old is None
            or result.provider != old.provider
            or result.api_key_changed
            or option is None
            or wanted not in values
        )
        if not restart:
            option_id = str(option.get("id") or "model")
            try:
                options = await screen.session.set_config_option(option_id, wanted)
            except HarnessClientError as exc:
                await screen._notice(f"Model change failed: {exc.message}", "error")
                return
            screen.session.remember(options)
            if result.effort is not None:
                if effort_option(options) is not None:
                    try:
                        options = await screen.session.set_config_option("effort", result.effort)
                    except HarnessClientError as exc:
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
                    f" · effort {effort_label(result.effort)}" if result.effort is not None else ""
                )
                await screen._notice(
                    f"Model: {wanted}{suffix}",
                    "success",
                )
                return
        try:
            target = local_target(
                result.path,
                api_key_ref=result.api_key_ref,
                cwd=screen.target.cwd,
            )
        except HarnessClientError as exc:
            await screen._notice(
                f"Saved the configuration, but it cannot be used yet: {exc.message}", "error"
            )
            return
        screen.app.target = target
        screen.target = target
        await screen._notice(
            "Restarting harness-local with the saved provider configuration; "
            "reloading this session afterwards.",
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
        except HarnessClientError as exc:
            await screen._notice(f"Mode change failed: {exc.message}", "error")
            return
        screen.chrome.update_chrome()
        await screen._notice(f"Mode set to {mode_id}.", "success")
