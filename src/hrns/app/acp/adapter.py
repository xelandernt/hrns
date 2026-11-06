"""Adapt a Pydantic AI agent to the Agent Client Protocol (ACP) agent interface.

This is our own implementation of the adapter, derived from the design of
``pydantic-ai-harness``'s experimental ACP adapter. It now lives in ``hrns`` so the
adapter can implement the full ACP 1.x method table without subclassing or patching a
dependency that still targets ACP 0.x.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from inspect import isawaitable
from typing import Generic, Literal, cast
from uuid import uuid4

import acp
import anyio
from acp import schema
from acp.interfaces import Client
from pydantic_ai import (
    DeferredToolRequests,
    DeferredToolResults,
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
from pydantic_ai.output import OutputDataT
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset, AgentToolset, FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits

from hrns.capabilities import TodoStore, TodoUpdated
from hrns.capabilities.todo import _bind_todo_store

from . import updates
from .content import PromptContentBlock, prompt_blocks_to_user_content
from .models import ModelChoice, ModelResolver, ResolvedModel
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
    DocumentState,
    McpServers,
    SessionConfigFunc,
    SessionState,
    SessionUpdate,
    apply_content_changes,
)
from .store import (
    DeletableSessionStore,
    ListableSessionStore,
    SessionStore,
    StoredSession,
)

# Version advertised to the client when the caller does not supply one.
DEFAULT_VERSION = "0.1.0"

_logger = logging.getLogger(__name__)
_MODEL_CONFIG_ID = "model"


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


class _TurnCancelled(Exception):
    """Raised internally to unwind a turn the client cancelled mid-flight."""


@dataclass(kw_only=True)
class _TurnState:
    """Per-turn state shared by the stream handlers."""

    conn: Client
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


class PydanticAIACPAgent(acp.Agent, Generic[AgentDepsT, OutputDataT]):
    """An ACP agent backed by a Pydantic AI ``Agent``.

    Each ACP session is an independent conversation keyed by session ID. Sessions are
    streamed as ``session/update`` notifications and tool calls that require approval
    become ACP permission requests.

    ``session/list`` and ``session/delete`` are supported when the configured store
    implements :class:`ListableSessionStore` and :class:`DeletableSessionStore`.
    ``model_resolver`` ties the ``model`` session config option to the ``providers/*``
    methods: it supplies the model list, resolves a selection for a run, and owns the
    provider routing a client may change. Without it the adapter advertises neither.
    MCP-over-ACP and NES are explicitly unsupported.
    """

    def __init__(
        self,
        agent: AbstractAgent[AgentDepsT, OutputDataT],
        *,
        deps: AgentDepsT = None,
        name: str | None = None,
        version: str = DEFAULT_VERSION,
        session_config: SessionConfigFunc[AgentDepsT] | None = None,
        permission_policy: PermissionPolicy | None = None,
        prompt_capabilities: schema.PromptCapabilities | None = None,
        mcp_capabilities: schema.McpCapabilities | None = None,
        tool_presenter: ToolCallPresenter | None = None,
        session_store: SessionStore | None = None,
        model_resolver: ModelResolver | None = None,
        usage_limits: UsageLimits | None = None,
    ) -> None:
        self._agent = agent
        self._deps = deps
        self._name = name or agent.name or "pydantic-ai-agent"
        self._version = version
        self._session_config = session_config
        self._permission_policy = permission_policy or default_permission_scope
        self._prompt_capabilities = prompt_capabilities or schema.PromptCapabilities()
        if mcp_capabilities is not None and session_config is None:
            raise ValueError(
                "mcp_capabilities requires a session_config that connects the MCP servers"
            )
        self._mcp_capabilities = mcp_capabilities or schema.McpCapabilities()
        self._tool_presenter = tool_presenter or default_coding_presenter
        self._session_store = session_store
        self._model_resolver = model_resolver
        self._usage_limits = (
            usage_limits if usage_limits is not None else UsageLimits(request_limit=None)
        )
        self._client_capabilities: schema.ClientCapabilities | None = None
        self._conn: Client | None = None
        self._sessions: dict[str, SessionState[AgentDepsT]] = {}

    def on_connect(self, conn: Client) -> None:
        """Capture the outbound connection used to stream updates and request permission."""
        self._conn = conn

    @property
    def _listable_store(self) -> ListableSessionStore | None:
        store = self._session_store
        return store if isinstance(store, ListableSessionStore) else None

    @property
    def _deletable_store(self) -> DeletableSessionStore | None:
        store = self._session_store
        return store if isinstance(store, DeletableSessionStore) else None

    def _model_choices(self) -> Sequence[ModelChoice]:
        """The models advertised in the ``model`` config option, in priority order."""
        resolver = self._model_resolver
        return resolver.list_models() if resolver is not None else ()

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: object,
    ) -> schema.InitializeResponse:
        """Negotiate the protocol version and advertise the agent's capabilities."""
        self._client_capabilities = client_capabilities
        if 1 <= protocol_version <= acp.PROTOCOL_VERSION:
            negotiated_version = protocol_version
        else:
            negotiated_version = acp.PROTOCOL_VERSION
        session_capabilities = schema.SessionCapabilities(close=schema.SessionCloseCapabilities())
        if self._listable_store is not None:
            session_capabilities.list = schema.SessionListCapabilities()
            if self._deletable_store is not None:
                session_capabilities.delete = schema.SessionDeleteCapabilities()
        return schema.InitializeResponse(
            protocol_version=negotiated_version,
            agent_capabilities=schema.AgentCapabilities(
                load_session=self._session_store is not None,
                prompt_capabilities=self._prompt_capabilities,
                mcp_capabilities=self._mcp_capabilities,
                providers=(
                    schema.ProvidersCapabilities() if self._model_resolver is not None else None
                ),
                session_capabilities=session_capabilities,
            ),
            agent_info=schema.Implementation(name=self._name, version=self._version),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: McpServers = None,
        **kwargs: object,
    ) -> schema.NewSessionResponse:
        """Start a new session with an empty conversation history."""
        session_id = uuid4().hex
        config = await self._build_config(session_id, cwd, mcp_servers)
        choices = self._model_choices()
        default_model = choices[0].model_id if choices else None
        state = SessionState(session_id=session_id, config=config, cwd=cwd, model=default_model)
        self._sessions[session_id] = state
        if (store := self._listable_store) is not None:
            await store.remember(session_id, cwd=cwd)
        await self._persist(state)
        return schema.NewSessionResponse(
            session_id=session_id, config_options=self._model_config_options(state.model)
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: McpServers = None,
        additional_directories: list[str] | None = None,
        **kwargs: object,
    ) -> schema.LoadSessionResponse | None:
        """Reopen a stored session, restoring history and replaying the transcript."""
        if self._session_store is None:
            raise acp.RequestError.method_not_found("session/load")
        if self._conn is None:  # pragma: no cover - on_connect always runs before load_session
            raise RuntimeError("load_session called before on_connect()")
        try:
            stored = await self._session_store.load(session_id)
        except Exception as exc:
            raise acp.RequestError.internal_error(
                {"session_id": session_id, "reason": "stored session could not be read"}
            ) from exc
        if stored is None:
            raise acp.RequestError.invalid_params(
                {"session_id": session_id, "reason": "no stored session with this id"}
            )
        prior = self._sessions.pop(session_id, None)
        if prior is not None:
            await self._cancel_active_turn(prior)
        config = await self._build_config(session_id, cwd, mcp_servers)
        state = SessionState(
            session_id=session_id,
            config=config,
            cwd=cwd,
            history=list(stored.messages),
            transcript=list(stored.updates),
            model=stored.model,
            todos=TodoStore(items=stored.todos),
        )
        self._sessions[session_id] = state
        if (store := self._listable_store) is not None:
            await store.remember(session_id, cwd=cwd)
        for update in stored.updates:
            await self._conn.session_update(session_id=session_id, update=update)
        return schema.LoadSessionResponse(config_options=self._model_config_options(state.model))

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **kwargs: object,
    ) -> schema.ListSessionsResponse:
        """Return one page of stored sessions and the next page's cursor."""
        store = self._listable_store
        if store is None:
            raise acp.RequestError.method_not_found("session/list")
        try:
            summaries, next_cursor = await store.list_sessions(cwd=cwd, cursor=cursor)
        except ValueError as exc:
            raise acp.RequestError.invalid_params({"cursor": cursor, "reason": str(exc)}) from exc
        return schema.ListSessionsResponse(
            sessions=[
                schema.SessionInfo(
                    session_id=summary.session_id,
                    cwd=summary.cwd,
                    title=summary.title,
                    updated_at=(
                        summary.updated_at.isoformat() if summary.updated_at is not None else None
                    ),
                )
                for summary in summaries
            ],
            next_cursor=next_cursor,
        )

    async def delete_session(
        self,
        session_id: str,
        **kwargs: object,
    ) -> schema.DeleteSessionResponse:
        """Delete a session from the store, discarding any live state first."""
        store = self._deletable_store
        if store is None or self._listable_store is None:
            raise acp.RequestError.method_not_found("session/delete")
        await self.close_session(session_id)
        await store.delete(session_id)
        return schema.DeleteSessionResponse()

    async def _persist(self, state: SessionState[AgentDepsT]) -> None:
        """Save a session's committed state, if a store is configured."""
        if self._session_store is None:
            return
        try:
            await self._session_store.save(
                state.session_id,
                StoredSession(
                    messages=list(state.history),
                    updates=list(state.transcript),
                    model=state.model,
                    todos=state.todos.items,
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

    def _model_config_options(
        self, current_model_id: str | None
    ) -> list[schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean] | None:
        option = self._model_option(current_model_id)
        return None if option is None else [option]

    def session_history(self, session_id: str) -> list[ModelMessage] | None:
        """A snapshot of a resident session's committed model history."""
        state = self._sessions.get(session_id)
        return list(state.history) if state is not None else None

    def session_model_option(self, session_id: str) -> schema.SessionConfigOptionSelect | None:
        """The model config option for a resident session."""
        state = self._sessions.get(session_id)
        return self._model_option(state.model) if state is not None else None

    async def _build_config(
        self, session_id: str, cwd: str, mcp_servers: McpServers
    ) -> AcpSessionConfig[AgentDepsT]:
        if self._session_config is None:
            if mcp_servers:
                raise acp.RequestError.invalid_params(
                    {
                        "reason": "this agent does not connect MCP servers; provide a "
                        "session_config that turns mcp_servers into toolsets to use them",
                        "mcp_server_count": len(mcp_servers),
                    }
                )
            return AcpSessionConfig(deps=self._deps)
        if self._conn is None:  # pragma: no cover - on_connect always runs before session setup
            raise RuntimeError("_build_config called before on_connect()")
        session = AcpSession(
            cwd=cwd,
            mcp_servers=mcp_servers or [],
            client_capabilities=self._client_capabilities,
            client=self._conn,
            session_id=session_id,
        )
        result = self._session_config(session)
        return await result if isawaitable(result) else result

    async def prompt(
        self,
        session_id: str,
        prompt: list[PromptContentBlock],
        **kwargs: object,
    ) -> schema.PromptResponse:
        """Run one user turn, streaming the agent's output to the client."""
        if self._conn is None:  # pragma: no cover
            raise RuntimeError("prompt() called before on_connect()")
        state = self._sessions.get(session_id)
        if state is None:
            raise acp.RequestError.invalid_params({"session_id": session_id})

        user_content = prompt_blocks_to_user_content(prompt)
        async with state.lock:
            if self._sessions.get(session_id) is not state:
                raise acp.RequestError.invalid_params(
                    {
                        "session_id": session_id,
                        "reason": "session was closed while the prompt was queued",
                    }
                )
            state.cancel_requested = False
            turn = asyncio.ensure_future(self._run_turn(state, prompt, user_content))
            state.active_turn = turn
            try:
                return await asyncio.shield(turn)
            except _TurnCancelled:
                return schema.PromptResponse(stop_reason="cancelled")
            except asyncio.CancelledError:
                if turn.done() and state.cancel_requested:
                    return schema.PromptResponse(stop_reason="cancelled")
                if state.cancel_requested:
                    with contextlib.suppress(asyncio.CancelledError, _TurnCancelled):
                        await asyncio.shield(turn)
                    raise
                turn.cancel()
                with contextlib.suppress(asyncio.CancelledError, _TurnCancelled):
                    await turn
                raise
            finally:
                state.active_turn = None
                state.cancel_requested = False

    async def cancel(self, session_id: str, **kwargs: object) -> None:
        """Cancel the in-flight turn for a session, if any."""
        state = self._sessions.get(session_id)
        if state is None:
            return
        state.cancel_requested = True
        if state.active_turn is not None and not state.active_turn.done():
            state.active_turn.cancel()

    async def close_session(
        self, session_id: str, **kwargs: object
    ) -> schema.CloseSessionResponse | None:
        """Cancel any in-flight turn and discard all state for the session."""
        state = self._sessions.pop(session_id, None)
        if state is not None:
            await self._cancel_active_turn(state)
        return schema.CloseSessionResponse()

    async def _cancel_active_turn(self, state: SessionState[AgentDepsT]) -> None:
        turn = state.active_turn
        if turn is not None and not turn.done():
            state.cancel_requested = True
            turn.cancel()
            try:
                await asyncio.shield(turn)
            except (asyncio.CancelledError, _TurnCancelled):
                if not turn.done():
                    raise

    async def _run_turn(
        self,
        state: SessionState[AgentDepsT],
        prompt: list[PromptContentBlock],
        user_content: list[UserContent],
    ) -> schema.PromptResponse:
        conn = self._conn
        assert conn is not None
        history = state.history
        config = state.config
        usage = RunUsage()
        stop_reason: schema.StopReason = "end_turn"
        deferred_results: DeferredToolResults | None = None
        run_input: list[UserContent] | None = user_content
        output_type = [self._agent.output_type, DeferredToolRequests]
        turn = _TurnState(
            conn=conn,
            session_id=state.session_id,
            cwd=state.cwd,
            approval_names=self._approval_tool_names(config),
            known_non_approval_names=self._known_non_approval_names(config),
        )
        turn.updates.extend(updates.update_user_message(block) for block in prompt)

        captured: list[ModelMessage] = []
        last_response: ModelResponse | None = None
        try:
            while True:
                result = None
                resolved_model = self._resolve_run_model(state.model)
                with _bind_todo_store(state.todos), capture_run_messages() as captured:
                    async with self._agent.run_stream_events(
                        run_input,
                        message_history=history,
                        deferred_tool_results=deferred_results,
                        output_type=output_type,
                        deps=config.deps,
                        capabilities=config.capabilities,
                        toolsets=config.toolsets,
                        workspace=config.workspace,
                        model=resolved_model.model if resolved_model is not None else None,
                        model_settings=(
                            resolved_model.settings if resolved_model is not None else None
                        ),
                        usage_limits=self._usage_limits,
                    ) as stream:
                        event: AgentStreamEvent | AgentRunResultEvent[object]
                        async for event in stream:
                            if isinstance(event, AgentRunResultEvent):
                                result = event.result
                            else:
                                await self._emit_event(turn, event)

                assert result is not None, "run_stream_events always yields a final result event"
                history = result.all_messages()
                usage += result.usage
                last_response = result.response
                output = result.output
                if isinstance(output, DeferredToolRequests):
                    if not output.approvals and not output.calls:  # pragma: no cover
                        break
                    deferred_results = await self._resolve_approvals(turn, state, output)
                    run_input = None
                    continue
                if output is not None and not isinstance(output, str):
                    await self._emit_text(turn, json.dumps(jsonable(output)), thought=False)
                stop_reason = _finish_reason_to_stop_reason(result.response.finish_reason)
                break
        except (asyncio.CancelledError, _TurnCancelled):
            with anyio.CancelScope(shield=True):
                await self._fail_outstanding_tool_calls(turn)
                await self._persist_interrupted_todos(state, turn)
            raise
        except UsageLimitExceeded as exc:
            await self._fail_outstanding_tool_calls(turn, record=True)
            if captured:
                earlier_runs = {message.run_id for message in history}
                for message in captured:
                    if isinstance(message, ModelResponse) and message.run_id not in earlier_runs:
                        usage.incr(message.usage)
                history = _committable_history(captured, turn.started)
            stop_reason = _usage_limit_stop_reason(exc)
        except Exception:
            await self._fail_outstanding_tool_calls(turn)
            await self._persist_interrupted_todos(state, turn)
            raise

        if usage.cost is not None:
            state.cost = (state.cost or Decimal(0)) + usage.cost
        await self._emit_usage(turn, state, response=last_response)

        state.history = history
        if self._session_store is not None:
            state.transcript.extend(turn.updates)
            try:
                await self._persist(state)
            except asyncio.CancelledError:
                _logger.warning(
                    "persisting ACP session %s was cancelled; durable state is now behind",
                    state.session_id,
                )
                stop_reason = "cancelled"
        return schema.PromptResponse(stop_reason=stop_reason, usage=_to_acp_usage(usage))

    async def _persist_interrupted_todos(
        self, state: SessionState[AgentDepsT], turn: _TurnState
    ) -> None:
        """Retain published todo changes even when the turn does not commit history."""
        plans = [update for update in turn.updates if isinstance(update, schema.AgentPlanUpdate)]
        if self._session_store is not None and plans:
            state.transcript.append(plans[-1])
            await self._persist(state)

    async def _fail_outstanding_tool_calls(self, turn: _TurnState, *, record: bool = False) -> None:
        for tool_call_id in turn.started - turn.resulted:
            update = updates.update_tool_call(tool_call_id=tool_call_id, status="failed")
            if record:
                turn.updates.append(update)
            with contextlib.suppress(Exception):
                await turn.conn.session_update(session_id=turn.session_id, update=update)

    async def _send_update(self, turn: _TurnState, update: SessionUpdate) -> None:
        turn.updates.append(update)
        await turn.conn.session_update(session_id=turn.session_id, update=update)

    async def _emit_usage(
        self,
        turn: _TurnState,
        state: SessionState[AgentDepsT],
        *,
        response: ModelResponse | None,
    ) -> None:
        """Publish the usage gauge clients render as context percentage and session cost.

        ``PromptResponse.usage`` carries only per-turn token totals and has no room for
        either figure, so an agent that stays silent here leaves the client's readout
        blank. The gauge is deliberately not recorded in the transcript: replaying a
        stale percentage into a freshly loaded session would be wrong.
        """
        if response is None:
            return
        resolved = self._resolve_run_model(state.model)
        size = _context_window(resolved.model if resolved is not None else self._agent.model)
        used = _context_tokens(response)
        if size is None or used <= 0:
            return
        await turn.conn.session_update(
            session_id=turn.session_id,
            update=schema.UsageUpdate(
                used=used,
                size=size,
                cost=(
                    schema.Cost(amount=float(state.cost), currency="USD")
                    if state.cost is not None
                    else None
                ),
            ),
        )

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
        if requests.calls:
            names = sorted({call.tool_name for call in requests.calls})
            raise acp.RequestError.internal_error(
                {
                    "reason": "external tool execution is not supported by the ACP adapter",
                    "tools": names,
                }
            )

        results = DeferredToolResults()
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
                toolsets.append(cast(AbstractToolset[AgentDepsT], contributed))
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

    async def _request_permission(
        self, turn: _TurnState, state: SessionState[AgentDepsT], call: ToolCallPart, scope: Hashable
    ) -> bool | ToolDenied:
        fields = self._tool_call_fields(call, turn.cwd, default_kind="execute")
        response = await turn.conn.request_permission(
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
                schema.PermissionOption(kind="allow_once", name="Allow", option_id="allow_once"),
                schema.PermissionOption(
                    kind="allow_always", name="Always allow", option_id="allow_always"
                ),
                schema.PermissionOption(kind="reject_once", name="Reject", option_id="reject_once"),
                schema.PermissionOption(
                    kind="reject_always", name="Always reject", option_id="reject_always"
                ),
            ],
        )
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

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: object
    ) -> schema.SetSessionModeResponse | None:
        """Modes are not implemented by this adapter."""
        raise acp.RequestError.method_not_found("session/set_mode")

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: object
    ) -> schema.SetSessionConfigOptionResponse | None:
        """Switch the session's model through ACP's session config option surface."""
        state = self._sessions.get(session_id)
        if state is None:
            raise acp.RequestError.invalid_params({"session_id": session_id})
        if config_id != _MODEL_CONFIG_ID:
            raise acp.RequestError.invalid_params(
                {"config_id": config_id, "reason": "unknown config option"}
            )
        if not isinstance(value, str) or value not in {
            choice.model_id for choice in self._model_choices()
        }:
            raise acp.RequestError.invalid_params(
                {"model_id": value, "reason": "not an advertised model"}
            )
        state.model = value
        await self._persist(state)
        options = self._model_config_options(state.model)
        assert options is not None
        return schema.SetSessionConfigOptionResponse(config_options=options)

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: McpServers = None,
        **kwargs: object,
    ) -> schema.ForkSessionResponse:
        """Forking is not implemented by this adapter."""
        raise acp.RequestError.method_not_found("session/fork")

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: McpServers = None,
        **kwargs: object,
    ) -> schema.ResumeSessionResponse:
        """Resuming is not implemented by this adapter; use ``session/load``."""
        raise acp.RequestError.method_not_found("session/resume")

    async def authenticate(
        self, method_id: str, **kwargs: object
    ) -> schema.AuthenticateResponse | None:
        """No authentication is required, so this is a no-op."""
        return None

    async def logout(self, **kwargs: object) -> schema.LogoutResponse:
        """No authentication is configured, so there is nothing to log out of."""
        return schema.LogoutResponse()

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
        state = self._sessions.get(session_id)
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
        state = self._sessions.get(session_id)
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
        state = self._sessions.get(session_id)
        if state is not None:
            state.documents.pop(uri, None)

    async def did_save(self, session_id: str, uri: str, **kwargs: object) -> None:
        """Mark a tracked document as saved."""
        state = self._sessions.get(session_id)
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
        state = self._sessions.get(session_id)
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

    async def list_providers(self, **kwargs: object) -> schema.ListProvidersResponse:
        """Report the providers a client may route and their current non-secret routing."""
        resolver = self._model_resolver
        if resolver is None:
            raise acp.RequestError.method_not_found("providers/list")
        return schema.ListProvidersResponse(
            providers=[
                schema.ProviderInfo(
                    provider_id=provider.provider_id,
                    supported=list(provider.supported),
                    required=provider.required,
                    current=(
                        schema.ProviderCurrentConfig(
                            api_type=provider.current.api_type,
                            base_url=provider.current.base_url,
                        )
                        if provider.current is not None
                        else None
                    ),
                )
                for provider in resolver.list_providers()
            ]
        )

    async def set_provider(
        self,
        provider_id: str,
        api_type: Literal["anthropic", "openai", "azure", "vertex", "bedrock"] | str,
        base_url: str,
        headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> schema.SetProviderResponse:
        """Replace one provider's routing with the client's own endpoint."""
        resolver = self._model_resolver
        if resolver is None:
            raise acp.RequestError.method_not_found("providers/set")
        try:
            resolver.set_provider(provider_id, api_type, base_url, headers or {})
        except ValueError as exc:
            raise acp.RequestError.invalid_params(
                {"provider_id": provider_id, "reason": str(exc)}
            ) from exc
        return schema.SetProviderResponse()

    async def disable_provider(
        self, provider_id: str, **kwargs: object
    ) -> schema.DisableProviderResponse:
        """Stop routing through one provider, unless the resolver marks it required."""
        resolver = self._model_resolver
        if resolver is None:
            raise acp.RequestError.method_not_found("providers/disable")
        try:
            resolver.disable_provider(provider_id)
        except ValueError as exc:
            raise acp.RequestError.invalid_params(
                {"provider_id": provider_id, "reason": str(exc)}
            ) from exc
        return schema.DisableProviderResponse()

    # --- Explicitly unsupported ACP 1.x features ------------------------------------
    # These capabilities are not advertised in ``initialize``. The SDK router still
    # dispatches the methods, so answer with the rejection that matches the advertisement.

    async def mcp_message(
        self,
        connection_id: str,
        method: str,
        params: dict[str, object] | None = None,
        **kwargs: object,
    ) -> object:
        """Unsupported: this adapter has no MCP-over-ACP binding."""
        raise acp.RequestError.method_not_found("mcp/message")

    async def notify_mcp(
        self,
        connection_id: str,
        method: str,
        params: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        """Unsupported: MCP notifications are ignored."""
        return None

    async def start_nes(
        self,
        workspace_uri: str | None = None,
        workspace_folders: list[schema.WorkspaceFolder] | None = None,
        repository: schema.NesRepository | None = None,
        **kwargs: object,
    ) -> schema.StartNesResponse:
        """Unsupported: this adapter has no next-edit-suggestion service."""
        raise acp.RequestError.method_not_found("nes/start")

    async def suggest_nes(
        self,
        session_id: str,
        uri: str,
        version: int,
        position: schema.Position,
        trigger_kind: Literal["automatic", "diagnostic", "manual"],
        selection: schema.Range | None = None,
        context: schema.NesSuggestContext | None = None,
        **kwargs: object,
    ) -> schema.SuggestNesResponse:
        """Unsupported: this adapter has no next-edit-suggestion service."""
        raise acp.RequestError.method_not_found("nes/suggest")

    async def close_nes(self, session_id: str, **kwargs: object) -> schema.CloseNesResponse:
        """Unsupported: this adapter has no next-edit-suggestion service."""
        raise acp.RequestError.method_not_found("nes/close")

    async def accept_nes(self, session_id: str, id: str, **kwargs: object) -> None:  # noqa: A002
        """Unsupported: NES acceptance notifications are ignored."""
        return None

    async def reject_nes(
        self,
        session_id: str,
        id: str,  # noqa: A002
        reason: Literal["rejected", "ignored", "replaced", "cancelled"] | None = None,
        **kwargs: object,
    ) -> None:
        """Unsupported: NES rejection notifications are ignored."""
        return None

    async def ext_method(self, method: str, params: dict[str, object]) -> dict[str, object]:
        """This adapter exposes no extension methods."""
        raise acp.RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, object]) -> None:
        """This adapter exposes no extension notifications."""
        return None
