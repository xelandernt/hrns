"""Tool approval and ask_user elicitation bridging for SessionRuntime."""

from __future__ import annotations

import logging
from collections.abc import Hashable, Sequence
from typing import TYPE_CHECKING, Generic

import acp
from acp import schema
from acp.interfaces import Client
from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.output import OutputDataT
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import (
    AbstractToolset,
    AgentToolset,
    FunctionToolset,
)
from pydantic_ai_harness.ask_user import AskUserRequest as HarnessAskUserRequest

from . import updates
from .elicitation import (
    ASK_USER_UNAVAILABLE,
    ask_user_message,
    ask_user_response,
    ask_user_tool_result,
    build_ask_user_schema,
)
from .interactions import Interactions
from .permission import PermissionPolicy, ToolCallPermission
from .presentation import ToolCallPresenter
from .runtime_types import _ASK_USER_TOOL, _ToolCallFields, _TurnCancelled, _TurnState
from .serialize import bounded_jsonable
from .session import AcpSessionConfig, SessionState, SessionUpdate

if TYPE_CHECKING:
    from pydantic_ai.agent import AbstractAgent

_logger = logging.getLogger(__name__)


class _SessionApprovalsMixin(Generic[AgentDepsT, OutputDataT]):
    _agent: AbstractAgent[AgentDepsT, OutputDataT]
    _tool_presenter: ToolCallPresenter
    _permission_policy: PermissionPolicy
    interactions: Interactions
    _conn: Client | None
    _client_capabilities: schema.ClientCapabilities | None
    attachment_toolsets: Sequence[AbstractToolset[AgentDepsT]]

    @property
    def state(self) -> SessionState[AgentDepsT]: ...

    def _tool_call_fields(
        self, call: ToolCallPart, cwd: str, *, default_kind: schema.ToolKind
    ) -> _ToolCallFields: ...

    async def _send_update(self, turn: _TurnState, update: SessionUpdate) -> None: ...

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
