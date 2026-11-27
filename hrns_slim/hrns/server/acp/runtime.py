"""Session-owned native execution, transcript rendering, and model configuration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Sequence
from decimal import Decimal
from inspect import isawaitable
from typing import Generic
from uuid import uuid4

import acp
import anyio
from acp import schema
from acp.interfaces import Client
from pydantic_ai import (
    DeferredToolRequests,
    DeferredToolResults,
    RunCancelled,
    UsageLimitExceeded,
    capture_run_messages,
)
from pydantic_ai.agent import AbstractAgent, ModelRequestNode
from pydantic_ai.capabilities import AgentNode, Hooks
from pydantic_ai.exceptions import ModelConnectionError
from pydantic_ai.messages import (
    AgentStreamEvent,
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
    UserContent,
    repair_messages,
)
from pydantic_ai.output import OutputDataT, OutputSpec
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, DynamicToolset
from pydantic_ai.usage import RunUsage, UsageLimits

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
from .interactions import Interactions
from .models import ModelResolver
from .permission import PermissionPolicy, default_permission_scope
from .presentation import (
    ToolCallPresentation,
    ToolCallPresenter,
    absolutize,
    default_coding_presenter,
)
from .runtime_approvals import _SessionApprovalsMixin
from .runtime_config import _SessionConfigMixin
from .runtime_types import (
    _MODEL_CONFIG_ID,
    SKILLS_META_KEY,
    ConfigOptions,
    RunObserver,
    SteeringPrompt,
    _committable_history,
    _context_tokens,
    _context_window,
    _finish_reason_to_stop_reason,
    _last_response,
    _LiveUsage,
    _to_acp_usage,
    _ToolCallFields,
    _TurnCancelled,
    _TurnState,
    _usage_limit_stop_reason,
)
from .serialize import bounded_jsonable, bounded_shell_output, chunk_text, jsonable
from .session import (
    AcpSession,
    AcpSessionConfig,
    AttachmentConfigFunc,
    McpServers,
    SessionConfigFunc,
    SessionContext,
    SessionState,
    SessionUpdate,
)
from .store import (
    SessionPersistence,
    StoredSession,
    title_from_updates,
)
from .toolsets import AttachmentToolset

__all__ = [
    "_MODEL_CONFIG_ID",
    "ConfigOptions",
    "RunObserver",
    "SKILLS_META_KEY",
    "SessionRuntime",
    "SteeringPrompt",
]

_logger = logging.getLogger(__name__)


class SessionRuntime(
    _SessionConfigMixin[AgentDepsT, OutputDataT],
    _SessionApprovalsMixin[AgentDepsT, OutputDataT],
    Generic[AgentDepsT, OutputDataT],
):
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
        session_store: SessionPersistence,
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
        self.take_steering: Callable[[], tuple[SteeringPrompt, ...]] = lambda: ()
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
        self._usage_update: schema.UsageUpdate | schema.SessionInfoUpdate | None = None
        self._reasoning_tokens: int | None = None

    async def create(self, session_id: str, cwd: str) -> None:
        config = await self._build_config(session_id, cwd)
        choices = self._model_choices()
        self._state = SessionState(
            session_id=session_id,
            config=config,
            cwd=cwd,
            model=choices[0].model_id if choices else None,
        )
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
        model_changed = self._reconcile_model(self.state)
        settings_changed = self._reconcile_setting_selections(self.state)
        if model_changed or settings_changed:
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
            self._reasoning_tokens = None
            self._usage_update = None
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
        run_input = await self._with_skill_instructions(state, user_content, skills)
        await self._announce_skills(turn, skills)

        captured: list[ModelMessage] = []
        last_response: ModelResponse | None = None
        connection_error: ModelConnectionError | None = None

        async def inject_steering(
            ctx: RunContext[AgentDepsT], *, node: AgentNode[AgentDepsT]
        ) -> AgentNode[AgentDepsT]:
            # Keep messages in the session queue while tools are running, so a
            # cancellation before the next request leaves them available to resume.
            if not isinstance(node, ModelRequestNode):
                return node
            for pending in self.take_steering():
                invoked = await self._resolve_skills(pending.prompt)
                content = await self._with_skill_instructions(
                    state, prompt_blocks_to_user_content(pending.prompt), invoked
                )
                ctx.enqueue(*content)
                for block in pending.prompt:
                    update = updates.update_user_message(block)
                    if self._conn is pending.origin and pending.origin is not None:
                        turn.updates.append(update)
                    else:
                        await self._send_update(turn, update)
                await self._announce_skills(turn, invoked)
            return node

        steering = Hooks[AgentDepsT](before_node_run=inject_steering)
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
                        capabilities=[*(config.capabilities or ()), steering],
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
        except (ModelConnectionError, UsageLimitExceeded) as exc:
            await self._fail_outstanding_tool_calls(turn, record=True)
            if captured:
                earlier_runs = {message.run_id for message in history}
                for message in captured:
                    if isinstance(message, ModelResponse) and message.run_id not in earlier_runs:
                        usage.incr(message.usage)
            if isinstance(exc, ModelConnectionError):
                connection_error = exc
                history = captured or history
                # A connection failure can leave an empty response in the capture.
                if history and isinstance(tail := history[-1], ModelResponse):
                    if tail.state == "interrupted" and not tail.parts:
                        history = history[:-1]
                history = repair_messages(history)
                last_response = _last_response(history)
                if self.observer is not None:
                    self.observer.interrupted(history)
            else:
                if self.observer is not None:
                    self.observer.interrupted(captured)
                if captured:
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
        if connection_error is not None:
            raise connection_error
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
        """Publish context usage and estimated session cost when available.

        ACP usage updates require a context size. A session info update carries
        the estimate when the model has no known context window. During a turn,
        ``spent`` includes the committed session cost and completed requests in the run.
        """
        if response is None:
            return
        reported_reasoning = response.usage.details.get("reasoning_tokens")
        reasoning_tokens = (
            reported_reasoning if reported_reasoning and reported_reasoning > 0 else None
        )
        if reasoning_tokens != self._reasoning_tokens:
            self._reasoning_tokens = reasoning_tokens
            await self._deliver_update(
                schema.SessionInfoUpdate(
                    field_meta={"hrns/usage/reasoning_tokens": reasoning_tokens}
                )
            )
        resolved = self._resolve_run_model(state.model)
        size = _context_window(resolved.model if resolved is not None else self._agent.model)
        used = _context_tokens(response)
        total = state.cost if spent is None else spent
        amount = float(total) if total is not None else None
        update: schema.UsageUpdate | schema.SessionInfoUpdate
        if size is not None and used > 0:
            update = schema.UsageUpdate(
                used=used,
                size=size,
                cost=schema.Cost(amount=amount, currency="USD") if amount is not None else None,
                field_meta={"hrns/usage/estimated_cost": True} if amount is not None else None,
            )
        else:
            if amount is None and self._usage_update is None:
                return
            update = schema.SessionInfoUpdate(
                field_meta={"hrns/usage/cost_without_context": amount}
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
                    raw_output=(
                        bounded_shell_output(raw_output)
                        if part.tool_name == "shell" and isinstance(raw_output, str)
                        else bounded_jsonable(raw_output)
                    ),
                ),
            )
