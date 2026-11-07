"""Model, reasoning effort, mode, and standard config-option workflow."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from acp import RequestError

from hrns.config import (
    EFFORT_DEFAULT,
    HrnsConfig,
    is_local_hrns,
    thinking_from_settings,
    with_thinking,
)

from ...errors import HrnsClientError, describe_rpc_error
from ...local import (
    local_credential_available,
    local_model_catalog,
    local_target,
    local_thinking_support,
)
from ..agent_providers import AgentProviderRequest, AgentProvidersDialog, models_by_provider
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
        option = next(
            (item for item in screen.session.config_options if item.get("id") == option_id), {}
        )
        if option.get("type") == "boolean":
            screen._change_standard_option(option_id)
            return
        choices = option_values(option)
        if not choices:
            screen._notice_soon(
                f"{option.get('name', option_id)} has no selectable values.", "warning"
            )
            return
        values = [choice["value"] for choice in choices]
        current = option.get("currentValue")
        value = stepped_value(values, None if current is None else str(current), direction)
        if value is not None:
            screen._set_standard_value(option_id, value)

    async def change_standard_option(self, option_id: str) -> None:
        screen = self.screen
        option = next(
            (item for item in screen.session.config_options if item.get("id") == option_id), {}
        )
        if option.get("type") == "boolean":
            try:
                options = await screen.session.set_config_option(
                    option_id, not bool(option.get("currentValue"))
                )
            except HrnsClientError as exc:
                await screen._notice(exc.message, "error")
                return
            screen.session.remember(tuple(options))
            screen.chrome.update_chrome()
            return
        choices = option_values(option)
        if not choices:
            await screen._notice(
                f"{option.get('name', option_id)} has no selectable values.", "warning"
            )
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

    async def set_standard_value(self, option_id: str, value: str) -> bool:
        screen = self.screen
        try:
            options = await screen.session.set_config_option(option_id, value)
        except HrnsClientError as exc:
            await screen._notice(exc.message, "error")
            return False
        screen.session.remember(tuple(options))
        screen.chrome.update_chrome()
        return True

    # ------------------------------------------------------------- local model

    def local_settings(self) -> HrnsConfig:
        screen = self.screen
        target = screen.target
        args = (
            target.profile.args
            if is_local_hrns(target.profile if target is not None else None)
            else []
        )
        config = Path(args[1]) if len(args) == 2 and args[0] == "--config" else None
        return HrnsConfig(config_file=config) if config else HrnsConfig()

    async def show_models(self) -> None:
        """Local provider models, or the remote agent's advertised models."""
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

    async def set_model(self, target: str) -> None:
        """Set a model directly, without opening the picker or a dialog."""
        target = target.strip()
        if not target:
            await self.show_models()
            return
        screen = self.screen
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        if screen.target.kind == "local" and is_local_hrns(screen.target.profile):
            try:
                settings = self.local_settings()
            except (ValueError, OSError) as exc:
                await screen._notice(f"Local settings are unavailable: {exc}", "error")
                return
            if not settings.providers:
                await screen._notice(
                    "No local providers are configured yet. Add one with /providers.",
                    "warning",
                )
                return
            try:
                provider, model = self._resolve_local_model(settings, target)
            except HrnsClientError as exc:
                await screen._notice(exc.message, "error")
                return
            await self.save_model_selection(settings, provider, model, None, touch_thinking=False)
            return
        await self._set_agent_model(target)

    @staticmethod
    def _resolve_local_model(settings: HrnsConfig, target: str) -> tuple[str, str]:
        """Resolve ``model`` or ``provider:model`` against the local configuration."""
        provider, separator, model = target.partition(":")
        if separator:
            provider = provider.strip()
            model = model.strip()
            if not provider or not model:
                raise HrnsClientError("Use provider:model, for example openai:gpt-5.")
            if provider not in settings.providers:
                available = ", ".join(sorted(settings.providers)) or "none"
                raise HrnsClientError(
                    f"Unknown local provider '{provider}'. Configured providers: {available}."
                )
            return provider, model
        folded = target.casefold()
        current = settings.provider
        if current:
            for known in settings.models_for(current):
                if known.casefold() == folded:
                    return current, known
        matches = [
            (name, known)
            for name in settings.providers
            for known in settings.models_for(name)
            if known.casefold() == folded
        ]
        unique = list(dict.fromkeys(matches))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            choices = ", ".join(f"{name}:{known}" for name, known in unique)
            raise HrnsClientError(f"Model '{target}' is ambiguous; use one of: {choices}.")
        if current:
            return current, target
        if len(settings.providers) == 1:
            return next(iter(settings.providers)), target
        available = ", ".join(sorted(settings.providers)) or "none"
        raise HrnsClientError(
            f"No local provider is selected. Use provider:model. Configured providers: {available}."
        )

    async def _set_agent_model(self, target: str) -> None:
        """Set a remote agent's advertised model option, or its mode as a fallback."""
        screen = self.screen
        option = model_option(screen.session.config_options)
        if option is not None:
            values = option_values(option)
            if values:
                try:
                    value = self._match_choice(values, target, "model")
                except HrnsClientError as exc:
                    await screen._notice(exc.message, "error")
                    return
                option_id = str(option.get("id") or "model")
                if await self.set_standard_value(option_id, value):
                    await screen._notice(f"Model: {value}", "success")
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
            try:
                value = self._match_choice(choices, target, "mode")
            except HrnsClientError as exc:
                await screen._notice(exc.message, "error")
                return
            await self.set_mode(value)
            return
        await screen._notice(
            "This agent advertises neither a model config option nor session modes.", "warning"
        )

    @staticmethod
    def _match_choice(choices: list[dict[str, str]], target: str, label: str) -> str:
        """Match a value or display name, allowing the model part of ``provider:model``."""
        folded = target.casefold()

        def unique(values: list[str]) -> str | None:
            deduped = list(dict.fromkeys(values))
            return deduped[0] if len(deduped) == 1 else None

        for values in (
            [item["value"] for item in choices if item["value"] == target],
            [item["value"] for item in choices if item["name"].casefold() == folded],
            [item["value"] for item in choices if item["value"].casefold() == folded],
            [
                item["value"]
                for item in choices
                if item["value"].rpartition(":")[2].casefold() == folded
            ],
        ):
            match = unique(values)
            if match is not None:
                return match
        available = ", ".join(item["value"] for item in choices[:10])
        if len(choices) > 10:
            available += ", …"
        raise HrnsClientError(f"Unknown {label} '{target}'. Available: {available}.")

    def models_picked(
        self, settings: HrnsConfig, result: ModelPick | CustomModelRequest | None
    ) -> None:
        """React to the model picker: switch model, or add a custom id first."""
        if isinstance(result, ModelPick):
            self.select_local_model(settings, result.provider, result.model)
        elif isinstance(result, CustomModelRequest):
            self.add_custom_model(settings, result.provider, result.model)

    def open_providers(self, settings: HrnsConfig | None = None) -> None:
        """Open the provider table, reloading the settings when none were passed."""
        screen = self.screen
        try:
            current = settings if settings is not None else self.local_settings()
        except (ValueError, OSError) as exc:
            screen.app.notify(f"Local settings are unavailable: {exc}", severity="error")
            return
        screen.app.push_screen(ProvidersDialog(current), self.providers_saved)

    async def show_providers(self) -> None:
        """Local provider entries, or the connected agent's provider routing.

        ``hrns-local`` reads its entries from the config file, so editing that file is the
        operation that matters for a local connection. Any other agent that advertises the
        ``providers`` capability gets its own in-memory routing editor instead.
        """
        screen = self.screen
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        if screen.target.kind == "local" and is_local_hrns(screen.target.profile):
            self.open_providers()
            return
        if not self.agent_supports_providers():
            await screen._notice(
                "This agent does not accept provider configuration; it does not advertise "
                "the ACP providers capability.",
                "warning",
            )
            return
        await self.open_agent_providers()

    def agent_supports_providers(self) -> bool:
        """Whether the connected agent advertises ``agentCapabilities.providers``."""
        capabilities = getattr(self.screen.client.initialized, "agent_capabilities", None)
        return getattr(capabilities, "providers", None) is not None

    async def open_agent_providers(self) -> None:
        """List the agent's providers and offer to route or disable one."""
        screen = self.screen
        try:
            listing = await screen.client.connection.list_providers()
        except RequestError as exc:
            await screen._notice(describe_rpc_error("providers/list", exc), "error")
            return
        except HrnsClientError as exc:
            await screen._notice(exc.message, "error")
            return
        screen.app.push_screen(
            AgentProvidersDialog(
                list(listing.providers),
                models=models_by_provider(screen.session.config_options),
            ),
            self.agent_providers_closed,
        )

    def agent_providers_closed(self, request: AgentProviderRequest | None) -> None:
        """Apply the choice the table returned, then show the agent's new state."""
        if request is None:
            return
        self.screen.run_worker(
            self.apply_agent_provider(request),
            group="providers",
            exclusive=True,
            exit_on_error=False,
        )

    async def apply_agent_provider(self, request: AgentProviderRequest) -> None:
        screen = self.screen
        try:
            if request.kind == "disable":
                await screen.client.connection.disable_provider(request.provider_id)
            else:
                await screen.client.connection.set_provider(
                    request.provider_id,
                    request.api_type,
                    request.base_url,
                    request.headers or None,
                )
        except RequestError as exc:
            method = f"providers/{request.kind}"
            await screen._notice(describe_rpc_error(method, exc), "error")
        except HrnsClientError as exc:
            await screen._notice(exc.message, "error")
            return
        await self.open_agent_providers()

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

    def add_custom_model(self, settings: HrnsConfig, provider: str, initial: str = "") -> None:
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

    def select_local_model(self, settings: HrnsConfig, provider: str, model: str) -> None:
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
        settings: HrnsConfig,
        provider: str,
        model: str,
        *,
        current: str | None,
        on_select: Any,
        choices: list[dict[str, str]] | None = None,
    ) -> None:
        """Offer the effort ladder; the server's ACP option wins over the local catalog."""
        self.screen.app.push_screen(
            ChoiceDialog(
                "Reasoning effort",
                f"{provider}:{model}. Applies to the next message.",
                choices or effort_choices(settings.provider_id(provider), model),
                current=current or EFFORT_DEFAULT,
            ),
            lambda value: on_select(value) if value else None,
        )

    async def save_model_selection(
        self,
        settings: HrnsConfig,
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
        """Offer the session's effort ladder, whichever transport the agent uses.

        An agent that advertises an ``effort`` config option is authoritative: the server
        decides which levels exist and applies the choice before each request. Only
        ``hrns-local`` also remembers it in the config file, because that is where its
        policy lives; every other target — including a remote ACP server — uses the plain
        config-option path.
        """
        screen = self.screen
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        if screen.target is not None and is_local_hrns(screen.target.profile):
            await self._show_local_effort()
            return
        option = effort_option(screen.session.config_options)
        if option is None:
            await screen._notice(
                "This agent does not advertise a reasoning-effort option.", "warning"
            )
            return
        await self.change_standard_option(str(option.get("id") or "effort"))

    async def _show_local_effort(self) -> None:
        """Persist the effort for the next ``hrns-local`` launch, then apply it here."""
        screen = self.screen
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
            choices=option_values(option) if option is not None else None,
            on_select=lambda effort: screen._set_local_effort_worker(settings, effort),
        )

    async def set_local_effort(self, settings: HrnsConfig, effort: str) -> None:
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
        """Show only the model ids the remote agent advertised over ACP."""
        screen = self.screen
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the reconnected client. Start a new session first.",
                "warning",
            )
            return
        option = model_option(screen.session.config_options)
        values = option_values(option) if option is not None else []
        if not values:
            await screen._notice("This agent does not advertise any models.", "warning")
            return
        choices = [{**value, "name": model_label(value)} for value in values]

        def apply(value: str | None) -> None:
            if value is not None and option is not None:
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
                choices,
                current=str(option.get("currentValue")) if option is not None else None,
            ),
            apply,
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


def model_label(value: dict[str, str]) -> str:
    """A model's display name, with the provider it is tagged with.

    The agent tags each value with ``_meta.providerId``; when the name already reads
    ``provider:model`` the tag adds nothing, so it is only appended for ids that do not
    carry the provider.
    """
    name = value["name"]
    provider = value.get("provider") or ""
    if not provider or name.startswith(f"{provider}:"):
        return name
    return f"{name} — {provider}"
