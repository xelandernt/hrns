"""Model/setting configuration, skills resolution, and document tracking for SessionRuntime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Generic

import acp
from acp import schema
from pydantic_ai.messages import UserContent
from pydantic_ai.output import OutputDataT
from pydantic_ai.settings import ModelSettings, merge_model_settings
from pydantic_ai.tools import AgentDepsT, RunContext

from hrns.server.capabilities import SkillInfo, Skills

from . import updates
from .content import PromptContentBlock
from .models import ModelChoice, ModelResolver, ModelSetupError, ResolvedModel, SettingOption
from .providers import ProviderDisabled
from .runtime_types import (
    _MODEL_CONFIG_ID,
    _SKILL_TOOL_PREFIX,
    SKILLS_META_KEY,
    ConfigOptions,
    _skill_references,
    _TurnState,
)
from .session import (
    DocumentState,
    SessionState,
    SessionUpdate,
    apply_content_changes,
)

if TYPE_CHECKING:
    from pydantic_ai.agent import AbstractAgent


class _SessionConfigMixin(Generic[AgentDepsT, OutputDataT]):
    _agent: AbstractAgent[AgentDepsT, OutputDataT]
    _model_resolver: ModelResolver | None
    _state: SessionState[AgentDepsT] | None
    _skills: Skills[AgentDepsT] | None
    _remember_model: Callable[[str], Awaitable[None]] | None

    @property
    def state(self) -> SessionState[AgentDepsT]: ...

    async def _persist(self, state: SessionState[AgentDepsT]) -> None: ...

    async def _send_update(self, turn: _TurnState, update: SessionUpdate) -> None: ...

    async def _deliver_update(self, update: SessionUpdate) -> None: ...

    def _model_choices(self) -> Sequence[ModelChoice]:
        """The models advertised in the ``model`` config option, in priority order."""
        resolver = self._model_resolver
        return resolver.list_models() if resolver is not None else ()

    async def _skills_meta(self) -> dict[str, object] | None:
        """The ``session/new``/``session/load`` ``_meta`` carrying the skill catalog."""
        skills = self._skills
        if skills is None:
            return None
        entries = [info.to_meta() for info in await skills.metadata()]
        return {SKILLS_META_KEY: entries} if entries else None

    async def _resolve_skills(self, prompt: Sequence[PromptContentBlock]) -> tuple[SkillInfo, ...]:
        """Validate the skill references a prompt carries, or reject the prompt."""
        ids = _skill_references(prompt)
        if not ids:
            return ()
        skills = self._skills
        if skills is None:
            raise acp.RequestError.invalid_params({"reason": "this deployment exposes no skills"})
        catalog = await skills.metadata()
        by_id = {info.id: info for info in catalog}
        resolved: list[SkillInfo] = []
        for skill_id in ids:
            info = by_id.get(skill_id)
            if info is None:
                available = ", ".join(by_id) or "none"
                raise acp.RequestError.invalid_params(
                    {
                        "skill": skill_id,
                        "reason": f"unknown skill; available: {available}",
                    }
                )
            if not info.user_invocable:
                raise acp.RequestError.invalid_params(
                    {"skill": skill_id, "reason": "this skill is not user-invocable"}
                )
            resolved.append(info)
        return tuple(resolved)

    async def _with_skill_instructions(
        self,
        state: SessionState[AgentDepsT],
        content: list[UserContent],
        skills: Sequence[SkillInfo],
    ) -> list[UserContent]:
        capability = self._skills
        if not skills or capability is None:
            return content
        state.invoked_skills.update(info.id for info in skills)
        injected = [
            f'<skill_content name="{info.id}">\n'
            f"{await capability.instructions(info.id)}\n"
            "</skill_content>"
            for info in skills
        ]
        return [*injected, *content]

    async def _announce_skills(self, turn: _TurnState, skills: Sequence[SkillInfo]) -> None:
        """Show one completed tool call per skill the person invoked, as a transcript marker."""
        for info in skills:
            await self._send_update(
                turn,
                updates.start_tool_call(
                    tool_call_id=f"{_SKILL_TOOL_PREFIX}{info.id}",
                    title=f"Loaded skill: {info.name}",
                    kind="other",
                    status="completed",
                    raw_input={"id": info.id},
                ),
            )

    def _model_option(
        self, current_model_id: str | None
    ) -> schema.SessionConfigOptionSelect | None:
        choices = self._model_choices()
        if current_model_id is None or not choices:
            return None
        setup_message = self._model_setup_message(current_model_id, choices)
        meta: dict[str, object] = {}
        if setup_message:
            meta["hrns/prompt/setup"] = setup_message
        if self._model_resolver is not None:
            meta["hrns/model/providers"] = [
                entry.provider_id for entry in self._model_resolver.list_providers()
            ]
        return schema.SessionConfigOptionSelect(
            id=_MODEL_CONFIG_ID,
            name="Model",
            type="select",
            current_value=current_model_id,
            field_meta=meta or None,
            options=[
                schema.SessionConfigSelectOption(
                    value=choice.model_id,
                    name=choice.name or choice.model_id,
                    field_meta={
                        "providerId": choice.provider_id,
                        **({"hrns/model/preferred": True} if choice.preferred_for_provider else {}),
                        **(
                            {
                                "hrns/model/context_group": choice.context_group,
                                "hrns/model/context_size": choice.context_size,
                            }
                            if choice.context_group and choice.context_size
                            else {}
                        ),
                    },
                )
                for choice in choices
            ],
        )

    @staticmethod
    def _model_setup_message(model_id: str | None, choices: Sequence[ModelChoice]) -> str | None:
        return next(
            (choice.setup_message for choice in choices if choice.model_id == model_id), None
        )

    def check_prompt_setup(self) -> None:
        model_id = self.state.model
        if model_id is None:
            if self._agent.model is None:
                raise acp.RequestError(-32600, "Set up a provider and model to send messages.")
        elif message := self._model_setup_message(model_id, self._model_choices()):
            raise acp.RequestError(-32000, message)
        try:
            self._resolver_settings(self.state)
        except ValueError as exc:
            raise acp.RequestError(-32602, str(exc)) from exc

    def _config_options(self, state: SessionState[AgentDepsT]) -> ConfigOptions | None:
        """The complete config-option state ACP returns after every change.

        The model option comes first, then the resolver's setting options, so clients that
        prioritize by array order surface the model selection most prominently.
        """
        options: ConfigOptions = []
        model = self._model_option(state.model)
        if model is not None:
            options.append(model)
        options.extend(self._setting_options(state))
        return options or None

    def _setting_options(
        self, state: SessionState[AgentDepsT]
    ) -> list[schema.SessionConfigOptionSelect]:
        """The resolver's options for this session's model, with setting selections applied."""
        resolver = self._model_resolver
        if resolver is None or state.model is None:
            return []
        rendered: list[schema.SessionConfigOptionSelect] = []
        for option in resolver.list_options(state.model):
            values = {choice.value for choice in option.choices}
            current = state.setting_selections.get(option.id, option.default)
            if current not in values:
                current = option.default
            rendered.append(
                schema.SessionConfigOptionSelect(
                    id=option.id,
                    name=option.name,
                    description=option.description,
                    category=option.category,
                    current_value=current,
                    options=[
                        schema.SessionConfigSelectOption(
                            value=choice.value,
                            name=choice.name,
                            description=choice.description,
                            field_meta=dict(choice.meta) or None,
                        )
                        for choice in option.choices
                    ],
                    field_meta=dict(option.meta) or None,
                )
            )
        return rendered

    def _setting_option(
        self, state: SessionState[AgentDepsT], config_id: str
    ) -> SettingOption | None:
        """The resolver option with this id, if the current model advertises it."""
        resolver = self._model_resolver
        if resolver is None or state.model is None:
            return None
        return next(
            (option for option in resolver.list_options(state.model) if option.id == config_id),
            None,
        )

    def _reconcile_setting_selections(self, state: SessionState[AgentDepsT]) -> bool:
        """Drop setting selections the session's current model no longer offers.

        A model switch or a provider reroute can remove a value. Dropping it makes the
        option fall back to its default instead of sending a setting the resolver refuses.
        """
        resolver = self._model_resolver
        if resolver is None or state.model is None:
            return False
        advertised = {
            option.id: {choice.value for choice in option.choices}
            for option in resolver.list_options(state.model)
        }
        changed = False
        for config_id in list(state.setting_selections):
            if state.setting_selections[config_id] not in advertised.get(config_id, set[str]()):
                del state.setting_selections[config_id]
                changed = True
        return changed

    def _resolver_settings(self, state: SessionState[AgentDepsT]) -> ModelSettings:
        """The settings delta the session's setting selections currently imply."""
        resolver = self._model_resolver
        if resolver is None or state.model is None:
            return ModelSettings()
        return resolver.resolve_settings(state.model, dict(state.setting_selections))

    def _run_settings(
        self, state: SessionState[AgentDepsT], resolved: ResolvedModel | None
    ) -> Callable[[RunContext[AgentDepsT]], ModelSettings]:
        """Model settings resolved before every request, so a change lands mid-turn.

        Pydantic AI resolves a callable ``model_settings`` layer once per model request.
        Reading the session's live setting selections here is what makes ``session/set_config_option``
        apply to the next request of a running turn instead of only the next turn; the
        model's routing settings stay the base, so the client cannot drop them.
        """
        base = resolved.settings if resolved is not None else None
        resolver = self._model_resolver
        model_id = state.model
        if resolver is None or model_id is None:
            return lambda _run_context: base or ModelSettings()
        initial_options = tuple(resolver.list_options(model_id))
        selected = self._resolver_settings(state)

        def settings(_run_context: RunContext[AgentDepsT]) -> ModelSettings:
            nonlocal selected
            # The run still holds its original model if the session changes routes.
            if (
                state.model == model_id
                and tuple(resolver.list_options(model_id)) == initial_options
            ):
                selected = self._resolver_settings(state)
            return merge_model_settings(base, selected) or ModelSettings()

        return settings

    def _reconcile_model(self, state: SessionState[AgentDepsT]) -> bool:
        if self._model_resolver is None:
            return False
        choices = self._model_choices()
        if any(choice.model_id == state.model for choice in choices):
            return False
        next_model = choices[0].model_id if choices else None
        if state.model == next_model:
            return False
        state.model = next_model
        return True

    async def _push_option_changes(self, before: ConfigOptions | None) -> None:
        state = self.state
        model_changed = self._reconcile_model(state)
        settings_changed = self._reconcile_setting_selections(state)
        if model_changed or settings_changed:
            await self._persist(state)
        after = self._config_options(state)
        if after != before:
            await self._deliver_update(schema.ConfigOptionUpdate(config_options=after or []))

    def _resolve_run_model(self, model_id: str | None) -> ResolvedModel | None:
        if model_id is None:
            return None
        resolver = self._model_resolver
        if resolver is None:
            return ResolvedModel(model=model_id)
        try:
            return resolver.resolve(model_id)
        except ProviderDisabled as exc:
            raise acp.RequestError.invalid_params(
                {"model_id": model_id, "reason": str(exc)}
            ) from exc
        except ModelSetupError as exc:
            raise acp.RequestError(-32602, str(exc)) from exc

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        *,
        remember_model: Callable[[str], Awaitable[None]] | None = None,
        **kwargs: object,
    ) -> schema.SetSessionConfigOptionResponse | None:
        """Change the session's model or one of the resolver's settings.

        The advertised options are the contract: a value outside them is rejected rather
        than clamped. The response carries the complete option state because a model switch
        can change which settings exist and which values they offer.
        """
        state = self.state if session_id == self.state.session_id else None
        if state is None:
            raise acp.RequestError.invalid_params({"session_id": session_id})
        if not isinstance(value, str):
            raise acp.RequestError.invalid_params(
                {"config_id": config_id, "reason": "only select options are supported"}
            )
        if config_id == _MODEL_CONFIG_ID:
            if value not in {choice.model_id for choice in self._model_choices()}:
                raise acp.RequestError.invalid_params(
                    {"model_id": value, "reason": "not an advertised model"}
                )
            if remember_model is not None:
                await remember_model(value)
            state.model = value
            self._reconcile_setting_selections(state)
        else:
            option = self._setting_option(state, config_id)
            model = state.model
            if option is None or model is None:
                raise acp.RequestError(
                    -32602,
                    f"Config option {config_id!r} cannot be set to {value!r} for {model!r}.",
                )
            if value not in {choice.value for choice in option.choices}:
                choices = ", ".join(choice.value for choice in option.choices)
                raise acp.RequestError(
                    -32602,
                    f"{option.name} {value!r} is unsupported for {model!r}. Choose {choices}.",
                )
            candidate = {**state.setting_selections, config_id: value}
            resolver = self._model_resolver
            assert resolver is not None  # _setting_option only returns options from it
            try:
                # Validate combinations of otherwise advertised values.
                resolver.resolve_settings(model, candidate)
            except ValueError as exc:
                raise acp.RequestError(-32602, str(exc), {"config_id": config_id}) from exc
            state.setting_selections = candidate
        await self._persist(state)
        options = self._config_options(state)
        return schema.SetSessionConfigOptionResponse(config_options=options or [])

    # --- Document synchronization (unstable ACP) -------------------------------------
    # The state is tracked per session so a future NES implementation has a correct view
    # of the client's open documents. Nothing else consumes it yet.

    async def did_open(
        self,
        session_id: str,
        uri: str,
        language_id: str,
        version: int,
        text: str,
        **kwargs: object,
    ) -> None:
        """Record a document the client opened."""
        state = self.state if session_id == self.state.session_id else None
        if state is None:
            return
        state.documents[uri] = DocumentState(
            uri=uri, language_id=language_id, version=version, text=text
        )

    async def did_change(
        self,
        session_id: str,
        uri: str,
        version: int,
        content_changes: list[schema.TextDocumentContentChangeEvent],
        **kwargs: object,
    ) -> None:
        """Apply the client's document edits to the tracked text."""
        state = self.state if session_id == self.state.session_id else None
        if state is None:
            return
        document = state.documents.get(uri)
        if document is None:
            return
        document.text = apply_content_changes(document.text, content_changes)
        document.version = version
        document.saved = False

    async def did_close(self, session_id: str, uri: str, **kwargs: object) -> None:
        """Forget a document the client closed."""
        state = self.state if session_id == self.state.session_id else None
        if state is not None:
            state.documents.pop(uri, None)

    async def did_save(self, session_id: str, uri: str, **kwargs: object) -> None:
        """Mark a tracked document as saved."""
        state = self.state if session_id == self.state.session_id else None
        if state is None:
            return
        document = state.documents.get(uri)
        if document is not None:
            document.saved = True

    async def did_focus(
        self,
        session_id: str,
        uri: str,
        version: int,
        position: schema.Position,
        visible_range: schema.Range,
        **kwargs: object,
    ) -> None:
        """Record where the client's cursor and viewport are."""
        state = self.state if session_id == self.state.session_id else None
        if state is None:
            return
        document = state.documents.get(uri)
        if document is None:
            return
        document.version = version
        document.focus = position
        document.visible_range = visible_range
