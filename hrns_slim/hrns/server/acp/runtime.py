"""Session-owned native execution, transcript rendering, and model configuration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from inspect import isawaitable
from typing import Generic, Protocol, TypeAlias, TypeVar
from uuid import uuid4

import acp
import anyio
from acp import schema
from acp.interfaces import Client
from pydantic_ai import (
    DeferredToolRequests,
    DeferredToolResults,
    RunCancelled,
    ToolDenied,
    UsageLimitExceeded,
    capture_run_messages,
)
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.messages import (
    AgentStreamEvent,
    FinishReason,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelResponse,
    OutputToolCallEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
)
from pydantic_ai.models import Model
from pydantic_ai.output import OutputDataT, OutputSpec
from pydantic_ai.run import AgentRunResult, AgentRunResultEvent
from pydantic_ai.settings import ModelSettings, merge_model_settings
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.toolsets import (
    AbstractToolset,
    AgentToolset,
    CombinedToolset,
    DynamicToolset,
    FunctionToolset,
)
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.ask_user import AskUserRequest as HarnessAskUserRequest

from hrns.server.capabilities import (
    SkillInfo,
    TodoStore,
    TodoUpdated,
    bind_invoked_skills,
    find_skills,
)
from hrns.server.capabilities.todo import _bind_todo_store
from hrns.shared.acp_mcp import AcpChannel, McpOverAcpCaller

from . import updates
from .content import PromptContentBlock, prompt_blocks_to_user_content
from .elicitation import (
    ASK_USER_UNAVAILABLE,
    ask_user_message,
    ask_user_response,
    ask_user_tool_result,
    build_ask_user_schema,
)
from .interactions import Interactions
from .models import ModelChoice, ModelResolver, ResolvedModel, SettingOption
from .permission import PermissionPolicy, ToolCallPermission, default_permission_scope
from .presentation import (
    ToolCallContent,
    ToolCallPresentation,
    ToolCallPresenter,
    absolutize,
    default_coding_presenter,
)
from .providers import ProviderDisabled
from .serialize import bounded_jsonable, chunk_text, jsonable
from .session import (
    AcpSession,
    AcpSessionConfig,
    AttachmentConfigFunc,
    DocumentState,
    McpServers,
    SessionConfigFunc,
    SessionContext,
    SessionState,
    SessionUpdate,
    apply_content_changes,
)
from .store import (
    ListableSessionStore,
    SessionStore,
    StoredSession,
    title_from_updates,
)
from .toolsets import AttachmentToolset

_logger = logging.getLogger(__name__)
_MODEL_CONFIG_ID = "model"
_ASK_USER_TOOL = "ask_user_question"

#: The ``session/new`` and ``session/load`` response ``_meta`` key carrying the skill catalog.
SKILLS_META_KEY = "hrns/skills"

#: Tool-call id prefix for the transcript marker of a skill the person invoked.
_SKILL_TOOL_PREFIX = "hrns-skill:"

#: One complete ACP config-option list, as ``session/new`` and ``session/set_config_option`` return it.
ConfigOptions: TypeAlias = list[
    schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean
]


def _skill_references(prompt: Sequence[PromptContentBlock]) -> tuple[str, ...]:
    """Skill ids a client attached to the prompt blocks, in first-seen order.

    A person's ``$skill`` reference travels in the text block's ``_meta`` under
    :data:`SKILLS_META_KEY`: a list of ``{"id": ..., "mention": {...}}`` objects. The
    mention is display information for the client; the server acts on the id and does
    not read the prompt text.
    """
    ids: list[str] = []
    for block in prompt:
        meta = block.field_meta
        if not meta:
            continue
        payload = meta.get(SKILLS_META_KEY)
        if not isinstance(payload, list):
            continue
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            skill_id = entry.get("id")
            if isinstance(skill_id, str) and skill_id and skill_id not in ids:
                ids.append(skill_id)
    return tuple(ids)


def _finish_reason_to_stop_reason(finish_reason: FinishReason | None) -> schema.StopReason:
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "content_filter":
        return "refusal"
    return "end_turn"


_USAGE_LIMIT_STOP_REASONS: dict[str, schema.StopReason] = {
    "request_limit": "max_turn_requests",
    "tool_calls_limit": "max_turn_requests",
    "input_tokens_limit": "max_tokens",
    "output_tokens_limit": "max_tokens",
    "total_tokens_limit": "max_tokens",
    "per_request_input_tokens_limit": "max_tokens",
    "cost_limit": "max_turn_requests",
}

_USAGE_LIMIT_NAME = re.compile(rf"\b({'|'.join(_USAGE_LIMIT_STOP_REASONS)})\b")


def _usage_limit_stop_reason(exc: UsageLimitExceeded) -> schema.StopReason:
    match = _USAGE_LIMIT_NAME.search(str(exc))
    return _USAGE_LIMIT_STOP_REASONS[match.group(1)] if match else "max_turn_requests"


def _committable_history(messages: list[ModelMessage], ran_calls: set[str]) -> list[ModelMessage]:
    committed = list(messages)
    while committed:
        last = committed[-1]
        if isinstance(last, ModelResponse):
            if last.tool_calls and not any(
                call.tool_call_id in ran_calls for call in last.tool_calls
            ):
                committed.pop()
                continue
            break
        if any(
            isinstance(part, ToolReturnPart | RetryPromptPart) and part.tool_name
            for part in last.parts
        ):
            break
        previous = committed[-2] if len(committed) > 1 else None
        if (
            last.state == "interrupted"
            and isinstance(previous, ModelResponse)
            and any(call.tool_call_id in ran_calls for call in previous.tool_calls)
        ):
            break
        committed.pop()
    return committed


def _to_acp_usage(usage: RunUsage) -> schema.Usage:
    return schema.Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cached_read_tokens=usage.cache_read_tokens,
        cached_write_tokens=usage.cache_write_tokens,
    )


def _context_window(model: Model | str | None) -> int | None:
    """The model's context window, when its profile knows one."""
    if isinstance(model, Model):
        window = model.profile.get("context_window")
        return window if window else None
    return None


def _last_response(messages: Sequence[ModelMessage]) -> ModelResponse | None:
    """The newest model response among a run's messages."""
    return next(
        (message for message in reversed(messages) if isinstance(message, ModelResponse)), None
    )


def _context_tokens(response: ModelResponse) -> int:
    """Tokens the last request left in the context window.

    Cache reads and writes are part of the prompt, and the response itself becomes
    context for the next request, so all four counts belong in the gauge.
    """
    usage = response.usage
    return (
        usage.input_tokens
        + usage.cache_read_tokens
        + usage.cache_write_tokens
        + usage.output_tokens
    )


ObservedOutputT = TypeVar("ObservedOutputT")


class RunObserver(Protocol):
    async def consume(
        self,
        stream: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[ObservedOutputT]],
        history: list[ModelMessage],
        user_content: list[UserContent] | None,
        emit: Callable[[AgentStreamEvent], Awaitable[None]],
        run_id: str,
    ) -> AgentRunResult[ObservedOutputT]: ...

    def state_changed(self, state: dict[str, object]) -> None: ...

    def close(self) -> None: ...

    def interrupted(self, messages: list[ModelMessage]) -> None: ...


class _LiveUsage(Protocol):
    """A run handle's usage and messages while it is still streaming."""

    @property
    def usage(self) -> RunUsage: ...

    def all_messages(self) -> list[ModelMessage]: ...


class _TurnCancelled(Exception):
    """Raised internally to unwind a turn the client cancelled mid-flight."""


@dataclass(kw_only=True)
class _TurnState:
    """Per-turn state shared by the stream handlers."""

    session_id: str
    cwd: str
    approval_names: frozenset[str]
    known_non_approval_names: frozenset[str] = frozenset()
    started: set[str] = field(default_factory=set[str])
    denied: set[str] = field(default_factory=set[str])
    resulted: set[str] = field(default_factory=set[str])
    announced_status: dict[str, schema.ToolCallStatus] = field(
        default_factory=dict[str, schema.ToolCallStatus]
    )
    updates: list[SessionUpdate] = field(default_factory=list[SessionUpdate])


@dataclass(frozen=True, kw_only=True)
class _ToolCallFields:
    title: str
    kind: schema.ToolKind | None
    content: list[ToolCallContent] | None
    locations: list[schema.ToolCallLocation] | None


class SessionRuntime(Generic[AgentDepsT, OutputDataT]):
    """Execute one conversation independently of transport request tasks."""

    def __init__(
        self,
        agent: AbstractAgent[AgentDepsT, OutputDataT],
        *,
        deps: AgentDepsT = None,
        session_config: SessionConfigFunc[AgentDepsT] | None = None,
        attachment_config: AttachmentConfigFunc[AgentDepsT] | None = None,
        agent_id: str = "agent",
        permission_policy: PermissionPolicy | None = None,
        tool_presenter: ToolCallPresenter | None = None,
        session_store: SessionStore,
        model_resolver: ModelResolver | None = None,
        usage_limits: UsageLimits | None = None,
    ) -> None:
        self._agent = agent
        self.last_output: OutputDataT | None = None
        self._deps = deps
        self._session_config = session_config
        self._attachment_config = attachment_config
        self.agent_id = agent_id
        self.observer: RunObserver | None = None
        self.on_state: Callable[[], None] = lambda: None
        self.live_updates: list[SessionUpdate] = []
        self.interactions = Interactions()
        self._delivery: asyncio.Task[None] | None = None
        self._outbox: asyncio.Queue[SessionUpdate | dict[str, object]] | None = None
        self._attachment_lock = asyncio.Lock()
        self._attachment_generation = 0
        self.prompt_origin: Client | None = None
        self.attachment_toolsets: Sequence[AbstractToolset[AgentDepsT]] = ()
        self._permission_policy = permission_policy or default_permission_scope
        self._tool_presenter = tool_presenter or default_coding_presenter
        self._session_store = session_store
        self._persist_lock = asyncio.Lock()
        self._model_resolver = model_resolver
        self._usage_limits = (
            usage_limits if usage_limits is not None else UsageLimits(request_limit=None)
        )
        self._skills = find_skills(agent)
        self._client_capabilities: schema.ClientCapabilities | None = None
        self._conn: Client | None = None
        self._mcp_caller: McpOverAcpCaller | None = None
        self._mcp_channel: AcpChannel | None = None
        self._state: SessionState[AgentDepsT] | None = None
        #: The session title the attached client has been told, derived from the transcript.
        self._title: str | None = None
        #: The last usage gauge sent, so a turn that changes nothing does not repeat it.
        self._usage_update: schema.UsageUpdate | None = None

    @property
    def _listable_store(self) -> ListableSessionStore | None:
        store = self._session_store
        return store if isinstance(store, ListableSessionStore) else None

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

    async def create(self, session_id: str, cwd: str) -> None:
        config = await self._build_config(session_id, cwd)
        choices = self._model_choices()
        self._state = SessionState(
            session_id=session_id,
            config=config,
            cwd=cwd,
            model=choices[0].model_id if choices else None,
        )
        if (store := self._listable_store) is not None:
            await store.remember(session_id, cwd=cwd)
        await self._persist(self.state)

    async def restore(self, session_id: str, stored: StoredSession) -> None:
        config = await self._build_config(session_id, stored.cwd)
        self._state = SessionState(
            session_id=session_id,
            config=config,
            cwd=stored.cwd,
            history=list(stored.messages),
            transcript=list(stored.updates),
            model=stored.model,
            setting_selections=dict(stored.setting_selections),
            todos=TodoStore(items=stored.todos),
            invoked_skills=set(stored.skills),
        )
        self._title = title_from_updates(self.state.transcript)
        if self._reconcile_setting_selections(self.state):
            await self._persist(self.state)

    async def _persist(self, state: SessionState[AgentDepsT]) -> None:
        """Save a session's committed state, if a store is configured."""
        async with self._persist_lock:
            try:
                await self._session_store.save(
                    state.session_id,
                    StoredSession(
                        cwd=state.cwd,
                        messages=list(state.history),
                        updates=list(state.transcript),
                        model=state.model,
                        setting_selections=dict(state.setting_selections),
                        todos=state.todos.items,
                        skills=tuple(sorted(state.invoked_skills)),
                    ),
                )
            except Exception:
                _logger.exception(
                    "failed to persist ACP session %s; durable state is now behind",
                    state.session_id,
                )

    def _model_option(
        self, current_model_id: str | None
    ) -> schema.SessionConfigOptionSelect | None:
        choices = self._model_choices()
        if current_model_id is None or not choices:
            return None
        return schema.SessionConfigOptionSelect(
            id=_MODEL_CONFIG_ID,
            name="Model",
            type="select",
            current_value=current_model_id,
            options=[
                schema.SessionConfigSelectOption(
                    value=choice.model_id,
                    name=choice.name or choice.model_id,
                    field_meta={"providerId": choice.provider_id},
                )
                for choice in choices
            ],
        )

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

        def settings(_run_context: RunContext[AgentDepsT]) -> ModelSettings:
            return merge_model_settings(base, self._resolver_settings(state)) or ModelSettings()

        return settings

    async def _push_option_changes(self, before: ConfigOptions | None) -> None:
        state = self.state
        if self._reconcile_setting_selections(state):
            await self._persist(state)
        after = self._config_options(state)
        if after is not None and after != before:
            await self._deliver_update(schema.ConfigOptionUpdate(config_options=after))

    async def _build_config(self, session_id: str, cwd: str) -> AcpSessionConfig[AgentDepsT]:
        if self._session_config is None:
            return AcpSessionConfig(deps=self._deps)
        result = self._session_config(
            SessionContext(agent_id=self.agent_id, session_id=session_id, cwd=cwd)
        )
        return await result if isawaitable(result) else result

    @property
    def state(self) -> SessionState[AgentDepsT]:
        if self._state is None:
            raise RuntimeError("session has not been configured")
        return self._state

    async def attach(
        self,
        conn: Client,
        capabilities: schema.ClientCapabilities | None,
        mcp_servers: McpServers,
        caller: McpOverAcpCaller,
        channel: AcpChannel | None,
    ) -> None:
        async with self._attachment_lock:
            await self._attach(conn, capabilities, mcp_servers, caller, channel)

    async def _attach(
        self,
        conn: Client,
        capabilities: schema.ClientCapabilities | None,
        mcp_servers: McpServers,
        caller: McpOverAcpCaller,
        channel: AcpChannel | None,
    ) -> None:
        if self._conn is not None and self._conn is not conn:
            raise acp.RequestError.invalid_params({"reason": "session already has an ACP client"})
        reconnect = self._conn is None
        if reconnect:
            self._attachment_generation += 1
        self._conn = conn
        self._mcp_caller = caller
        self._mcp_channel = channel
        self._client_capabilities = capabilities
        try:
            if mcp_servers and self._attachment_config is None:
                raise acp.RequestError.invalid_params({"reason": "client MCP is not enabled"})
            prepared_toolsets: Sequence[AbstractToolset[AgentDepsT]] = ()
            if self._attachment_config is not None:
                setup = AcpSession(
                    cwd=self.state.cwd,
                    session_id=self.state.session_id,
                    client=conn,
                    client_capabilities=capabilities,
                    mcp_servers=mcp_servers or [],
                    mcp_caller=self._mcp_caller,
                    mcp_channel=self._mcp_channel,
                )
                result = self._attachment_config(setup)
                config = await result if isawaitable(result) else result
                generation = self._attachment_generation
                prepared_toolsets = tuple(
                    AttachmentToolset(
                        toolset,
                        lambda: self._conn is conn and self._attachment_generation == generation,
                    )
                    for toolset in config.toolsets or ()
                )
            await self._replay_attachment(conn, prepared_toolsets, reconnect=reconnect)
        except BaseException:
            self.detach()
            raise

    async def _replay_attachment(
        self,
        conn: Client,
        prepared_toolsets: Sequence[AbstractToolset[AgentDepsT]],
        *,
        reconnect: bool,
    ) -> None:
        delivery = self._delivery
        replay = [*self.state.transcript, *self.live_updates]
        outbox: asyncio.Queue[SessionUpdate | dict[str, object]] = asyncio.Queue()
        self._outbox = outbox
        if delivery is not None:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)

        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def deliver() -> None:
            try:
                for update in replay:
                    await conn.session_update(session_id=self.state.session_id, update=update)
                if self._title is not None:
                    await conn.session_update(
                        session_id=self.state.session_id,
                        update=schema.SessionInfoUpdate(title=self._title),
                    )
                if self._conn is not conn:
                    raise ConnectionError("attachment ended during replay")
                self.attachment_toolsets = prepared_toolsets
                ready.set_result(None)
                if reconnect:
                    self.interactions.attach(conn)
                self.on_state()
                while True:
                    update = await outbox.get()
                    if isinstance(update, dict):
                        await conn.ext_notification("hrns/session/state", update)
                    else:
                        await conn.session_update(session_id=self.state.session_id, update=update)
            except Exception as exc:
                if not ready.done():
                    ready.set_exception(exc)
                self.detach()
            finally:
                if not ready.done():
                    ready.set_exception(ConnectionError("attachment ended during replay"))

        self._delivery = asyncio.create_task(deliver())
        await ready

    def detach(self) -> None:
        self._attachment_generation += 1
        self.interactions.detach()
        self._conn = None
        self._client_capabilities = None
        self._outbox = None
        self.attachment_toolsets = ()
        self.state.documents.clear()
        delivery, self._delivery = self._delivery, None
        if delivery is not None and delivery is not asyncio.current_task():
            delivery.cancel()

    def publish_state(self, state: dict[str, object]) -> None:
        self._enqueue(state)
        if self.observer is not None:
            self.observer.state_changed(state)

    def _enqueue(self, update: SessionUpdate | dict[str, object]) -> None:
        if self._outbox is not None:
            self._outbox.put_nowait(update)

    async def _deliver_update(self, update: SessionUpdate) -> None:
        self._enqueue(update)

    def _client_toolset(self, ctx: RunContext[AgentDepsT]) -> AbstractToolset[AgentDepsT] | None:
        return CombinedToolset(list(self.attachment_toolsets)) if self.attachment_toolsets else None

    async def execute(self, prompt: list[PromptContentBlock]) -> schema.PromptResponse:
        skills = await self._resolve_skills(prompt)
        try:
            return await self._run_turn(
                self.state, prompt, prompt_blocks_to_user_content(prompt), skills
            )
        except _TurnCancelled:
            return schema.PromptResponse(stop_reason="cancelled")
        finally:
            self.live_updates = []

    async def _run_turn(
        self,
        state: SessionState[AgentDepsT],
        prompt: list[PromptContentBlock],
        user_content: list[UserContent],
        skills: Sequence[SkillInfo] = (),
    ) -> schema.PromptResponse:
        history = state.history
        config = state.config
        usage = RunUsage()
        stop_reason: schema.StopReason = "end_turn"
        deferred_results: DeferredToolResults | None = None
        run_input: list[UserContent] | None = user_content
        output_type: OutputSpec[OutputDataT | DeferredToolRequests] = [
            self._agent.output_type,
            DeferredToolRequests,
        ]
        turn = _TurnState(
            session_id=state.session_id,
            cwd=state.cwd,
            approval_names=self._approval_tool_names(config),
            known_non_approval_names=self._known_non_approval_names(config),
        )
        self.live_updates = turn.updates
        for block in prompt:
            update = updates.update_user_message(block)
            if self._conn is self.prompt_origin and self.prompt_origin is not None:
                turn.updates.append(update)
            else:
                await self._send_update(turn, update)
        capability = self._skills
        if skills and capability is not None:
            # Add invoked instructions as user content and keep the skills active
            # for the session so their bundled-file tools are available.
            state.invoked_skills.update(info.id for info in skills)
            injected = [
                f'<skill_content name="{info.id}">\n'
                f"{await capability.instructions(info.id)}\n"
                "</skill_content>"
                for info in skills
            ]
            user_content = [*injected, *user_content]
            run_input = user_content
            await self._announce_skills(turn, skills)

        captured: list[ModelMessage] = []
        last_response: ModelResponse | None = None
        try:
            while True:
                result = None
                native_run_id = uuid4().hex
                resolved_model = self._resolve_run_model(state.model)
                with (
                    _bind_todo_store(state.todos),
                    bind_invoked_skills(state.invoked_skills),
                    capture_run_messages() as captured,
                ):
                    async with self._agent.run_stream_events(
                        run_input,
                        message_history=history,
                        conversation_id=state.session_id,
                        run_id=native_run_id,
                        deferred_tool_results=deferred_results,
                        output_type=output_type,
                        deps=config.deps,
                        capabilities=config.capabilities,
                        toolsets=[
                            *(config.toolsets or ()),
                            DynamicToolset(self._client_toolset, per_run_step=True),
                        ],
                        workspace=config.workspace,
                        model=resolved_model.model if resolved_model is not None else None,
                        model_settings=self._run_settings(state, resolved_model),
                        usage_limits=self._usage_limits,
                    ) as stream:

                        async def emit(event: AgentStreamEvent) -> None:
                            await self._emit_event(turn, event)
                            # A tool call means its request completed, so the gauge can
                            # follow the turn instead of waiting for it to end.
                            if isinstance(event, (FunctionToolCallEvent, OutputToolCallEvent)):
                                await self._emit_live_usage(state, stream)

                        if self.observer is not None:
                            result = await self.observer.consume(
                                stream, history, run_input, emit, native_run_id
                            )
                        else:
                            async for event in stream:
                                if isinstance(event, AgentRunResultEvent):
                                    result = event.result
                                else:
                                    await emit(event)

                assert result is not None, "run_stream_events always yields a final result event"
                history = result.all_messages()
                usage += result.usage
                last_response = result.response
                output = result.output
                if isinstance(output, DeferredToolRequests):
                    if not output.approvals and not output.calls:  # pragma: no cover
                        break
                    self.interactions.waiting = True
                    self.on_state()
                    try:
                        deferred_results = await self._resolve_approvals(turn, state, output)
                    finally:
                        self.interactions.waiting = False
                        self.on_state()
                    run_input = None
                    continue
                if output is not None and not isinstance(output, str):
                    await self._emit_text(turn, json.dumps(jsonable(output)), thought=False)
                self.last_output = output
                stop_reason = _finish_reason_to_stop_reason(result.response.finish_reason)
                break
        except (asyncio.CancelledError, _TurnCancelled) as exc:
            if self.observer is not None:
                interrupted = RunCancelled.from_cancellation(exc)
                self.observer.interrupted(
                    interrupted.all_messages() if interrupted is not None else captured
                )
            with anyio.CancelScope(shield=True):
                await self._fail_outstanding_tool_calls(turn)
                await self._persist_interrupted_todos(state, turn)
            raise
        except UsageLimitExceeded as exc:
            if self.observer is not None:
                self.observer.interrupted(captured)
            await self._fail_outstanding_tool_calls(turn, record=True)
            if captured:
                earlier_runs = {message.run_id for message in history}
                for message in captured:
                    if isinstance(message, ModelResponse) and message.run_id not in earlier_runs:
                        usage.incr(message.usage)
                history = _committable_history(captured, turn.started)
            stop_reason = _usage_limit_stop_reason(exc)
        except Exception:
            if self.observer is not None:
                self.observer.interrupted(captured)
            await self._fail_outstanding_tool_calls(turn)
            await self._persist_interrupted_todos(state, turn)
            raise

        if usage.cost is not None:
            state.cost = (state.cost or Decimal(0)) + usage.cost
        await self._emit_usage(state, response=last_response)

        state.history = history
        state.transcript.extend(turn.updates)
        self.live_updates = []
        await self._announce_title(state)
        try:
            await self._persist(state)
        except asyncio.CancelledError:
            _logger.warning(
                "persisting ACP session %s was cancelled; durable state is now behind",
                state.session_id,
            )
            stop_reason = "cancelled"
        return schema.PromptResponse(stop_reason=stop_reason, usage=_to_acp_usage(usage))

    async def _announce_title(self, state: SessionState[AgentDepsT]) -> None:
        """Name the session from its first user text, the same way ``session/list`` does.

        A client that only sees ``session/list`` for stored sessions would otherwise keep
        showing the session id in its status band while the list shows a title. The update
        is not recorded in the transcript: the title is a function of it, so a fresh
        runtime derives the same name when it restores and announces it on attach.
        """
        title = title_from_updates(state.transcript)
        if title is None or title == self._title:
            return
        self._title = title
        await self._deliver_update(schema.SessionInfoUpdate(title=title))

    async def _persist_interrupted_todos(
        self, state: SessionState[AgentDepsT], turn: _TurnState
    ) -> None:
        """Retain published todo changes even when the turn does not commit history."""
        plans = [update for update in turn.updates if isinstance(update, schema.AgentPlanUpdate)]
        if plans:
            state.transcript.append(plans[-1])
            self.live_updates = []
            await self._persist(state)

    async def _fail_outstanding_tool_calls(self, turn: _TurnState, *, record: bool = False) -> None:
        for tool_call_id in turn.started - turn.resulted:
            update = updates.update_tool_call(tool_call_id=tool_call_id, status="failed")
            if record:
                turn.updates.append(update)
            with contextlib.suppress(Exception):
                await self._deliver_update(update)

    async def _send_update(self, turn: _TurnState, update: SessionUpdate) -> None:
        turn.updates.append(update)
        await self._deliver_update(update)

    async def _emit_usage(
        self,
        state: SessionState[AgentDepsT],
        *,
        response: ModelResponse | None,
        spent: Decimal | None = None,
    ) -> None:
        """Publish the usage gauge clients render as context percentage and session cost.

        ``PromptResponse.usage`` carries only per-turn token totals and has no room for
        either figure, so an agent that stays silent here leaves the client's readout
        blank. The gauge is deliberately not recorded in the transcript: replaying a
        stale percentage into a freshly loaded session would be wrong. ``spent`` is the
        session total to report, defaulting to the committed one; a caller following a
        running turn passes the committed total plus the run so far. Repeating a gauge
        the client already has tells it nothing, so an unchanged one is dropped.
        """
        if response is None:
            return
        resolved = self._resolve_run_model(state.model)
        size = _context_window(resolved.model if resolved is not None else self._agent.model)
        used = _context_tokens(response)
        if size is None or used <= 0:
            return
        total = state.cost if spent is None else spent
        update = schema.UsageUpdate(
            used=used,
            size=size,
            cost=(schema.Cost(amount=float(total), currency="USD") if total is not None else None),
        )
        if update == self._usage_update:
            return
        self._usage_update = update
        await self._deliver_update(update)

    async def _emit_live_usage(self, state: SessionState[AgentDepsT], run: _LiveUsage) -> None:
        """Follow a running turn with the gauge as soon as a request has completed.

        Waiting for the prompt to return leaves the readout empty or stuck on the previous
        turn for the whole run, which is the longest stretch a person watches it. The run
        handle reports the requests finished so far; a custom ``AbstractAgent`` need not
        support that live view, and the end of the turn still reports the gauge then.
        """
        try:
            usage = run.usage
            response = _last_response(run.all_messages())
        except Exception:
            return
        spent = state.cost
        if usage.cost is not None:
            spent = (spent or Decimal(0)) + usage.cost
        await self._emit_usage(state, response=response, spent=spent)

    async def _emit_text(self, turn: _TurnState, text: str, *, thought: bool) -> None:
        for chunk in chunk_text(text):
            update = (
                updates.update_agent_thought_text(chunk)
                if thought
                else updates.update_agent_message_text(chunk)
            )
            await self._send_update(turn, update)

    def _tool_call_fields(
        self, call: ToolCallPart, cwd: str, *, default_kind: schema.ToolKind | None
    ) -> _ToolCallFields:
        presentation = self._tool_presenter(call)
        presentation = (
            absolutize(presentation, cwd) if presentation is not None else ToolCallPresentation()
        )
        return _ToolCallFields(
            title=presentation.title or call.tool_name,
            kind=presentation.kind or default_kind,
            content=list(presentation.content) or None,
            locations=list(presentation.locations) or None,
        )

    async def _emit_event(self, turn: _TurnState, event: AgentStreamEvent) -> None:
        if isinstance(event, TodoUpdated):
            await self._send_update(
                turn,
                schema.AgentPlanUpdate(
                    session_update="plan",
                    entries=[
                        schema.PlanEntry(
                            content=item.content, priority=item.priority, status=item.status
                        )
                        for item in event.items
                    ],
                ),
            )
        elif isinstance(event, PartStartEvent):
            part = event.part
            if isinstance(part, TextPart) and part.content:
                await self._emit_text(turn, part.content, thought=False)
            elif isinstance(part, ThinkingPart) and part.content:
                await self._emit_text(turn, part.content, thought=True)
        elif isinstance(event, PartDeltaEvent):
            delta = event.delta
            if isinstance(delta, TextPartDelta) and delta.content_delta:
                await self._emit_text(turn, delta.content_delta, thought=False)
            elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
                await self._emit_text(turn, delta.content_delta, thought=True)
        elif isinstance(event, FunctionToolCallEvent):
            call = event.part
            if call.tool_call_id in turn.started:
                return
            turn.started.add(call.tool_call_id)
            fields = self._tool_call_fields(call, turn.cwd, default_kind=None)
            status: schema.ToolCallStatus = (
                "pending" if call.tool_name in turn.approval_names else "in_progress"
            )
            turn.announced_status[call.tool_call_id] = status
            await self._send_update(
                turn,
                updates.start_tool_call(
                    tool_call_id=call.tool_call_id,
                    title=fields.title,
                    kind=fields.kind,
                    status=status,
                    content=fields.content,
                    locations=fields.locations,
                    raw_input=bounded_jsonable(call.args),
                ),
            )
        elif isinstance(event, FunctionToolResultEvent):
            part = event.part
            if isinstance(part, RetryPromptPart):
                status, raw_output = "failed", part.model_response()
            elif part.tool_call_id in turn.denied:
                status, raw_output = "failed", part.content
            else:
                status, raw_output = "completed", part.content
            turn.resulted.add(part.tool_call_id)
            await self._send_update(
                turn,
                updates.update_tool_call(
                    tool_call_id=part.tool_call_id,
                    status=status,
                    raw_output=bounded_jsonable(raw_output),
                ),
            )

    async def _resolve_approvals(
        self, turn: _TurnState, state: SessionState[AgentDepsT], requests: DeferredToolRequests
    ) -> DeferredToolResults:
        results = DeferredToolResults()
        if requests.calls:
            results.calls.update(await self._resolve_calls(turn, requests.calls))
        for call in requests.approvals:
            if (
                turn.announced_status.get(call.tool_call_id) == "in_progress"
                and call.tool_name not in turn.known_non_approval_names
            ):
                await self._update_status(turn, call.tool_call_id, "pending")
            scope = self._permission_policy(
                ToolCallPermission(
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    args=call.args_as_dict(),
                )
            )
            if scope in state.always_allow:
                results.approvals[call.tool_call_id] = True
                await self._update_status(turn, call.tool_call_id, "in_progress")
                continue
            if scope in state.always_reject:
                results.approvals[call.tool_call_id] = ToolDenied("Rejected by the client.")
                turn.denied.add(call.tool_call_id)
                continue
            decision = await self._request_permission(turn, state, call, scope)
            results.approvals[call.tool_call_id] = decision
            if isinstance(decision, ToolDenied):
                turn.denied.add(call.tool_call_id)
            else:
                await self._update_status(turn, call.tool_call_id, "in_progress")
        return results

    async def _update_status(
        self, turn: _TurnState, tool_call_id: str, status: schema.ToolCallStatus
    ) -> None:
        await self._send_update(
            turn, updates.update_tool_call(tool_call_id=tool_call_id, status=status)
        )

    def _static_toolsets(
        self, config: AcpSessionConfig[AgentDepsT]
    ) -> list[AbstractToolset[AgentDepsT]]:
        toolsets: list[AbstractToolset[AgentDepsT]] = [
            *self._agent.toolsets,
            *(config.toolsets or []),
        ]
        for capability in config.capabilities or ():
            contributed: AgentToolset[AgentDepsT] | None = capability.get_toolset()
            if isinstance(contributed, AbstractToolset):
                toolsets.append(contributed)
        return toolsets

    def _approval_tool_names(self, config: AcpSessionConfig[AgentDepsT]) -> frozenset[str]:
        names: set[str] = set()
        for toolset in self._static_toolsets(config):
            if isinstance(toolset, FunctionToolset):
                names.update(name for name, tool in toolset.tools.items() if tool.requires_approval)
        return frozenset(names)

    def _known_non_approval_names(self, config: AcpSessionConfig[AgentDepsT]) -> frozenset[str]:
        names: set[str] = set()
        for toolset in self._static_toolsets(config):
            if isinstance(toolset, FunctionToolset):
                names.update(
                    name for name, tool in toolset.tools.items() if not tool.requires_approval
                )
        return frozenset(names)

    def _supports_form_elicitation(self) -> bool:
        capabilities = self._client_capabilities
        elicitation = capabilities.elicitation if capabilities is not None else None
        return elicitation is not None and elicitation.form is not None

    async def _resolve_calls(
        self, turn: _TurnState, calls: Sequence[ToolCallPart]
    ) -> dict[str, object]:
        """Answer deferred tool calls the adapter knows how to service."""
        resolved: dict[str, object] = {}
        for call in calls:
            if call.tool_name != _ASK_USER_TOOL:
                raise acp.RequestError.internal_error(
                    {
                        "reason": "external tool execution is not supported by the ACP adapter",
                        "tools": sorted({call.tool_name for call in calls}),
                    }
                )
            resolved[call.tool_call_id] = await self._ask_user(turn, call)
        return resolved

    async def _ask_user(self, turn: _TurnState, call: ToolCallPart) -> object:
        """Put one ``ask_user_question`` call to the client as a form elicitation."""
        request = HarnessAskUserRequest.from_tool_call(call)

        await self._update_status(turn, call.tool_call_id, "pending")
        try:

            async def ask(conn: Client) -> schema.CreateElicitationResponse:
                if not self._supports_form_elicitation():
                    raise acp.RequestError.method_not_found("elicitation/create")
                return await conn.create_elicitation(
                    message=ask_user_message(request.questions),
                    mode=schema.ElicitationFormSessionMode(
                        session_id=turn.session_id,
                        tool_call_id=call.tool_call_id,
                        requested_schema=build_ask_user_schema(request.questions),
                    ),
                )

            response = await self.interactions.request(ask)
        except acp.RequestError as exc:
            _logger.info("client rejected an ask-user elicitation: %s", exc)
            await self._update_status(turn, call.tool_call_id, "in_progress")
            return ASK_USER_UNAVAILABLE
        return ask_user_tool_result(request, ask_user_response(response, request))

    async def _request_permission(
        self, turn: _TurnState, state: SessionState[AgentDepsT], call: ToolCallPart, scope: Hashable
    ) -> bool | ToolDenied:
        fields = self._tool_call_fields(call, turn.cwd, default_kind="execute")

        async def ask(conn: Client) -> schema.RequestPermissionResponse:
            return await conn.request_permission(
                session_id=turn.session_id,
                tool_call=schema.ToolCallUpdate(
                    tool_call_id=call.tool_call_id,
                    title=fields.title,
                    kind=fields.kind,
                    status="pending",
                    content=fields.content,
                    locations=fields.locations,
                    raw_input=bounded_jsonable(call.args),
                ),
                options=[
                    schema.PermissionOption(
                        kind="allow_once", name="Allow", option_id="allow_once"
                    ),
                    schema.PermissionOption(
                        kind="allow_always", name="Always allow", option_id="allow_always"
                    ),
                    schema.PermissionOption(
                        kind="reject_once", name="Reject", option_id="reject_once"
                    ),
                    schema.PermissionOption(
                        kind="reject_always", name="Always reject", option_id="reject_always"
                    ),
                ],
            )

        response = await self.interactions.request(ask)
        outcome = response.outcome
        if isinstance(outcome, schema.DeniedOutcome):
            raise _TurnCancelled
        if outcome.option_id == "allow_always":
            state.always_allow.add(scope)
        elif outcome.option_id == "reject_always":
            state.always_reject.add(scope)
        if outcome.option_id in ("allow_once", "allow_always"):
            return True
        return ToolDenied("Rejected by the client.")

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

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: object
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
            state.model = value
            self._reconcile_setting_selections(state)
        else:
            option = self._setting_option(state, config_id)
            model = state.model
            if option is None or model is None:
                raise acp.RequestError.invalid_params(
                    {"config_id": config_id, "reason": "unknown config option"}
                )
            if value not in {choice.value for choice in option.choices}:
                raise acp.RequestError.invalid_params(
                    {"config_id": config_id, "reason": "not an advertised value"}
                )
            candidate = {**state.setting_selections, config_id: value}
            resolver = self._model_resolver
            assert resolver is not None  # _setting_option only returns options from it
            try:
                # Validate the whole selection map, not just this value: a resolver may
                # reject a combination even when each value is individually advertised.
                resolver.resolve_settings(model, candidate)
            except ValueError as exc:
                raise acp.RequestError.invalid_params(
                    {"config_id": config_id, "reason": str(exc)}
                ) from exc
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

    # --- Provider configuration (unstable ACP) --------------------------------------
    # The adapter translates the protocol and validates nothing itself: the model
    # resolver owns the state and the rules. A model is resolved per turn, so a new
    # route applies from the next turn; the session's model id does not change.

    # --- Explicitly unsupported ACP 1.x features ------------------------------------
    # These capabilities are not advertised in ``initialize``. The SDK router still
    # dispatches the methods, so answer with the rejection that matches the advertisement.
