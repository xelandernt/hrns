"""ACP hosting built on ``PydanticAIACPAgent``.

The upstream adapter owns ACP execution: prompts, streaming, approvals, cancellation,
conversation history, transcript replay, and session locks. This module only composes a
native agent per session, scopes persistence to the authenticated principal and registered
deployment, and adds the small host-owned metadata surface (list/delete/title/usage).
"""

from __future__ import annotations

import asyncio
import contextlib
import warnings
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from acp import schema
from acp.exceptions import RequestError
from acp.schema import DeleteSessionResponse
from pydantic import ValidationError
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.messages import (
    AgentStreamEvent,
    EnqueuedMessagesEvent,
    ModelRequest,
    UserPromptPart,
)
from pydantic_ai_harness.experimental import HarnessExperimentalWarning
from pydantic_ai_harness.experimental.acp import (
    PydanticAIACPAgent,
    chain_presenters,
    default_coding_presenter,
)

from hrns.config import EFFORT_DEFAULT
from hrns.protocol import (
    BindingListResponse,
    CommandCancelRequest,
    CommandInvokeRequest,
    CommandListRequest,
    CommandListResponse,
    ConfigurationUpdate,
    ExtensionAdvertisement,
    HrnsError,
    ListRequest,
    PluginListResponse,
    PresentationListRequest,
    PresentationListResponse,
    SteerRequest,
    paginate,
)

from . import acp as acp_map
from .acp_store import (
    finalize_usage,
    merge_usage,
    usage_payload,
)
from .ask_user import QuestionChannel
from .capabilities.coder._acp import coder_tool_presenter
from .command_service import CommandService
from .configuration_service import ConfigurationService
from .host_support import (
    HostBundle,
    HostPolicy,
    _ComposedAgent,
    _prompt_text,
)
from .session_service import SessionService

if TYPE_CHECKING:
    from pydantic_ai_harness.experimental.acp._adapter import _TurnState

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

__all__ = ["HrnsACPAgent"]


class HrnsACPAgent(PydanticAIACPAgent[Any, str]):
    """ACP agent whose sessions are composed native Pydantic AI agents."""

    def __init__(
        self,
        bundle: HostBundle,
        policy: HostPolicy | None = None,
        *,
        refresh_models: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        """``refresh_models`` re-advertises ids that appear after the runtime started."""
        self.bundle = bundle
        self._refresh_models = refresh_models
        self.policy = policy or HostPolicy()
        self.definition = bundle.definition
        bundle.registry.allow_local_binding_creation = self.policy.can_create_bindings
        self.store = bundle.store.scoped(self.policy.principal, self.policy.deployment_id)
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self.config = ConfigurationService(
            agent=self,
            policy=self.policy,
            definition=self.definition,
            registry=bundle.registry,
        )
        self.commands = CommandService(agent=self)
        self._client_conn: Any | None = None
        self.question_channel = QuestionChannel()
        models = tuple(self.policy.models)
        default = self.policy.default_model or (models[0] if models else None)
        self._default_model = default
        self.sessions = SessionService(
            agent=self,
            store=self.store,
            policy=self.policy,
            definition=self.definition,
            registry=bundle.registry,
        )
        super().__init__(
            cast(
                AbstractAgent[Any, str],
                _ComposedAgent(
                    name=self.definition.name or self.definition.agent_id,
                    report_usage=self.sessions.report_model_usage,
                ),
            ),
            name=self.definition.name or self.definition.agent_id,
            # ACP requires a string even when the definition has no version.
            version=self.definition.version or "",
            session_config=self.sessions.compose_session,
            tool_presenter=chain_presenters(coder_tool_presenter, default_coding_presenter),
            session_store=self.store,
            models=models or None,
            model_resolver=bundle.model_resolver,
            prompt_capabilities=schema.PromptCapabilities(embedded_context=True),
        )

    # --- connection and lifecycle ---------------------------------------

    def on_connect(self, conn: Any) -> None:
        self._client_conn = conn
        self.question_channel.bind(conn)
        super().on_connect(conn)

    async def shutdown(self) -> None:
        """Close every live session through the public adapter method, then the store."""
        for session_id in list(self.sessions.compositions):
            with contextlib.suppress(Exception):
                await self.close_session(session_id)
        self.sessions.compositions.clear()
        if self.bundle.close_store:
            with contextlib.suppress(Exception):
                await self.store.backend.close()

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse | None:
        response = await super().close_session(session_id, **kwargs)
        await self.commands.cancel_session(session_id)
        self.sessions.forget(session_id)
        self._turn_locks.pop(session_id, None)
        self.config.forget(session_id)
        self.commands.forget(session_id)
        return response

    def _turn_lock(self, session_id: str) -> asyncio.Lock:
        return self._turn_locks.setdefault(session_id, asyncio.Lock())

    # --- initialization --------------------------------------------------

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        response = await super().initialize(
            protocol_version,
            client_capabilities=(
                client_capabilities if self.policy.accepts_client_capabilities else None
            ),
            client_info=client_info,
            **kwargs,
        )
        capabilities = response.agent_capabilities or schema.AgentCapabilities()
        capabilities.load_session = True
        capabilities.session_capabilities = schema.SessionCapabilities(
            list=schema.SessionListCapabilities(),
            delete=schema.SessionDeleteCapabilities(),
            close=schema.SessionCloseCapabilities(),
        )
        advertisement = ExtensionAdvertisement(
            version=1,
            methods=list(self.policy.extension_methods()),
            local_binding_creation=self.policy.can_create_bindings,
            accepts_client_capabilities=self.policy.accepts_client_capabilities,
        )
        return response.model_copy(
            update={
                "agent_capabilities": capabilities,
                "field_meta": {"hrns": advertisement.to_wire()},
            }
        )

    # --- session lifecycle -----------------------------------------------

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        try:
            self.sessions.reject_additional_directories(additional_directories)
            self.sessions.reject_client_mcp(mcp_servers)
            self.policy.validate_cwd(cwd)
            meta = kwargs.get("hrns")
            initial = acp_map.initial_configuration_changes(meta)
            if not self.policy.can_create_bindings:
                added = [change for change in initial if getattr(change, "type", None) == "add"]
                if added:
                    raise HrnsError(
                        "POLICY_DENIED",
                        "Local binding creation is prohibited for this deployment.",
                        {"bindingIds": [change.binding_id for change in added]},
                    )
            effort = None
            if isinstance(meta, Mapping):
                value = meta.get("effort")
                if isinstance(value, str):
                    effort = value
            async with self.sessions.new_lock:
                self.sessions.pending_initial = list(initial) or None
                self.sessions.pending_effort = effort
                try:
                    response = await super().new_session(
                        cwd,
                        additional_directories=None,
                        mcp_servers=None,
                        **kwargs,
                    )
                finally:
                    self.sessions.pending_initial = None
                    self.sessions.pending_effort = None
            return response.model_copy(
                update={"config_options": self.sessions.options_for(response.session_id)}
            )
        except HrnsError as exc:
            raise acp_map.hrns_rpc_error(exc) from exc
        except RequestError:
            raise

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> schema.LoadSessionResponse | None:
        try:
            self.sessions.reject_additional_directories(additional_directories)
            self.sessions.reject_client_mcp(mcp_servers)
            record = await self.store.get(session_id)
            if record is None:
                raise RequestError.invalid_params(
                    {"sessionId": session_id, "reason": "no stored session with this id"}
                )
            stored_cwd = self.policy.validate_cwd(record.cwd)
            response = await super().load_session(
                stored_cwd,
                session_id,
                mcp_servers=None,
                additional_directories=None,
                **kwargs,
            )
        except HrnsError as exc:
            raise acp_map.hrns_rpc_error(exc) from exc
        except RequestError:
            raise
        if response is None:
            return response
        return response.model_copy(update={"config_options": self.sessions.options_for(session_id)})

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> schema.ListSessionsResponse:
        del kwargs
        return await self.sessions.list_sessions(cwd=cwd, cursor=cursor)

    async def delete_session(self, session_id: str, **kwargs: Any) -> DeleteSessionResponse | None:
        del kwargs
        return await self.sessions.delete_session(session_id)

    async def _emit_event(self, turn: _TurnState, event: AgentStreamEvent) -> None:
        """Record and render steering at delivery time, in stream order."""
        if isinstance(event, EnqueuedMessagesEvent):
            for message in event.messages:
                if not isinstance(message, ModelRequest):
                    continue
                for part in message.parts:
                    if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                        await self._send_update(
                            turn,
                            schema.UserMessageChunk(
                                session_update="user_message_chunk",
                                content=schema.TextContentBlock(type="text", text=part.content),
                            ),
                        )
            return
        await super()._emit_event(turn, event)

    # --- prompts, effort, and metadata -----------------------------------

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        async with self._turn_lock(session_id):
            # A composed session is only in memory; its first turn is what makes it durable, and
            # the prompt names it so even a turn that fails outright stays recognizable later.
            await self.sessions.ensure_persisted(session_id)
            await self.sessions.ensure_title(session_id, _prompt_text(prompt))
            self.sessions.turn_usage[session_id] = {}
            response = None
            try:
                response = await super().prompt(session_id, prompt, **kwargs)
                return response
            finally:
                channel = self.sessions.steering.get(session_id)
                if channel is not None:
                    channel.detach()
                increment = self.sessions.turn_usage.pop(session_id, {})
                if response is not None and response.usage is not None:
                    # The adapter's final totals include approval continuations and limits.
                    # Reconcile without double counting or losing measured usage on cancel.
                    # Only counters reconcile by max; the price is summed per response.
                    for name, value in usage_payload(response.usage).items():
                        if isinstance(value, int) and not isinstance(value, bool):
                            increment[name] = max(increment.get(name, 0), value)
                record = await self.store.get(session_id)
                if record is not None:
                    title = record.title
                    if not title and response is not None:
                        title = _prompt_text(prompt) or None
                    totals = merge_usage(record.usage, increment)
                    await self.store.update(
                        session_id,
                        title=title,
                        usage=totals,
                        touch=True,
                    )
                    await self.sessions.send_session_usage(session_id, finalize_usage(totals))
                    if title and title != record.title:
                        await self.sessions.send_session_title(session_id, title)

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse | None:
        if config_id == acp_map.EFFORT_OPTION_ID:
            composition = self.sessions.require_composition(session_id)
            if not isinstance(value, str):
                raise RequestError.invalid_params(
                    {"configId": config_id, "reason": "effort expects a string"}
                )
            support = composition.effort.support
            option = acp_map.effort_option(None, support=support)
            if option is None:
                raise RequestError.invalid_params(
                    {
                        "configId": config_id,
                        "reason": "the selected model has no reasoning effort option",
                    }
                )
            allowed = {
                item.value
                for item in option.options
                if isinstance(item, schema.SessionConfigSelectOption)
            }
            if value not in allowed:
                raise RequestError.invalid_params(
                    {"configId": config_id, "value": value, "options": sorted(allowed)}
                )
            composition.effort.effort = None if value == EFFORT_DEFAULT else value
            await self.sessions.persist_metadata(session_id, effort=value)
            return schema.SetSessionConfigOptionResponse(
                config_options=self.sessions.options_for(session_id)
            )
        if config_id == acp_map.MODEL_OPTION_ID and isinstance(value, str):
            self._adopt_added_models(value)
        response = await super().set_config_option(config_id, session_id, value, **kwargs)
        if config_id == acp_map.MODEL_OPTION_ID and self._default_model is not None:
            composition = self.sessions.composition(session_id)
            if composition is not None and isinstance(value, str):
                support = self.policy.effort_support(value)
                composition.effort.set_model(value, support)
                support_after = composition.effort.support
                keep = composition.effort.effort
                if keep is not None and acp_map.effort_option(keep, support=support_after) is None:
                    composition.effort.effort = None
                    keep = None
                await self.sessions.persist_metadata(session_id, effort=keep or EFFORT_DEFAULT)
        if response is None:
            return response
        return schema.SetSessionConfigOptionResponse(
            config_options=self.sessions.options_for(session_id)
        )

    def _adopt_added_models(self, model_id: str) -> None:
        """Adopt ids a config edit added after this runtime started.

        The adapter only accepts advertised ids, and the terminal adds providers and
        custom models while a session is open. The local runtime re-reads its config
        file here instead of being restarted, so switching stays live.
        """
        if self._refresh_models is None or model_id in self._models:
            return
        models = self._refresh_models()
        if models:
            self._models = models

    # --- project extension ------------------------------------------------

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if method == "hrns/session/rename":
                from hrns.protocol.models import SessionTitleUpdate

                request = SessionTitleUpdate.model_validate(params)
                if not await self.sessions.ensure_persisted(request.session_id):
                    raise HrnsError("SESSION_CLOSED", "Load the session before renaming it.")
                await self.store.update(request.session_id, title=request.title, touch=True)
                await self.sessions.send_session_title(request.session_id, request.title)
                return {"sessionId": request.session_id, "title": request.title}
            if method == "hrns/session/steer":
                request = SteerRequest.model_validate(dict(params))
                if await self.store.get(request.session_id) is None:
                    raise HrnsError("SESSION_CLOSED", "Load the session before steering it.")
                channel = self.sessions.steering.get(request.session_id)
                if channel is None or not channel.enqueue(request.text):
                    raise HrnsError(
                        "TURN_NOT_RUNNING",
                        "The turn is no longer accepting steering; send the message as a prompt.",
                    )
                return {"sessionId": request.session_id, "steered": True}
            if method == "hrns/capabilities/list":
                request = ListRequest.model_validate(dict(params))
                if not request.session_id:
                    raise HrnsError(
                        "INVALID_CONFIGURATION", "sessionId is required for a capability listing."
                    )
                composition = self.sessions.require_composition(request.session_id)
                descriptors = composition.descriptors()
                page, next_cursor = paginate(
                    descriptors, cursor=request.cursor, limit=request.limit or 50
                )
                snapshot = composition.snapshot
                return BindingListResponse(
                    items=page,
                    next_cursor=next_cursor,
                    selected_revision=snapshot.selected_revision,
                    active_revision=snapshot.active_revision,
                    policy_revision=snapshot.policy_revision,
                ).to_wire()
            if method == "hrns/plugins/list":
                if not self.policy.can_create_bindings:
                    raise RequestError.method_not_found("_hrns/plugins/list")
                request = ListRequest.model_validate(dict(params))
                page, next_cursor = paginate(
                    self.bundle.registry.catalog(),
                    cursor=request.cursor,
                    limit=request.limit or 50,
                )
                return PluginListResponse(items=page, next_cursor=next_cursor).to_wire()
            if method == "hrns/configuration/get":
                session_id = params.get("sessionId", params.get("session_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HrnsError("INVALID_CONFIGURATION", "sessionId is required.")
                composition = self.sessions.require_composition(session_id)
                return composition.snapshot.to_wire()
            if method == "hrns/configuration/update":
                request = ConfigurationUpdate.model_validate(dict(params))
                return await self.config.update(request)
            if method == "hrns/commands/list":
                request = CommandListRequest.model_validate(dict(params))
                composition = self.sessions.require_composition(request.session_id)
                page, next_cursor = paginate(
                    composition.commands.descriptors(),
                    cursor=request.cursor,
                    limit=request.limit,
                )
                return CommandListResponse(
                    commands=page,
                    selected_revision=composition.snapshot.selected_revision,
                    active_revision=composition.snapshot.active_revision,
                    supported_operations=sorted(composition.commands.supported_operations),
                    next_cursor=next_cursor,
                ).to_wire()
            if method == "hrns/commands/invoke":
                request = CommandInvokeRequest.model_validate(dict(params))
                return await self.commands.invoke(request)
            if method == "hrns/commands/cancel":
                request = CommandCancelRequest.model_validate(dict(params))
                if await self.store.get(request.session_id) is None:
                    raise HrnsError(
                        "SESSION_CLOSED", "Load the session before cancelling a command."
                    )
                return self.commands.cancel(request)
            if method == "hrns/presentation/get":
                request = PresentationListRequest.model_validate(dict(params))
                self.sessions.require_composition(request.session_id)
                snapshots = await self.sessions.presentation_snapshots(
                    request.session_id, request.tool_call_ids
                )
                return PresentationListResponse(snapshots=snapshots).to_wire()
            raise RequestError.method_not_found(f"_{method}")
        except HrnsError as exc:
            raise acp_map.hrns_rpc_error(exc) from exc
        except ValidationError as exc:
            raise acp_map.hrns_rpc_error(
                HrnsError(
                    "INVALID_CONFIGURATION",
                    "The extension request payload is invalid.",
                    {"errors": exc.errors()[:10]},
                )
            ) from exc
        except RequestError:
            raise
