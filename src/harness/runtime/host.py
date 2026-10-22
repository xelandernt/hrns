"""ACP hosting built on ``PydanticAIACPAgent``.

The upstream adapter owns ACP execution: prompts, streaming, approvals, cancellation,
conversation history, transcript replay, and session locks. This module only composes a
native agent per session, scopes persistence to the authenticated principal and registered
deployment, and adds the small host-owned metadata surface (list/delete/title/usage).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import os
import sys
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import reduce
from pathlib import Path
from typing import Any, cast

from acp import schema
from acp.agent.router import build_agent_router
from acp.connection import Connection
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.exceptions import RequestError
from acp.meta import AGENT_METHODS, CLIENT_METHODS
from acp.schema import DeleteSessionRequest, DeleteSessionResponse
from acp.stdio import stdio_streams
from acp.utils import normalize_result, notify_model, request_model
from pydantic import ValidationError
from pydantic_ai import RunContext
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai_harness.experimental import HarnessExperimentalWarning
from pydantic_ai_harness.experimental.acp import (
    PydanticAIACPAgent,
)

from harness.config import EFFORT_DEFAULT
from harness.protocol import (
    AddChange,
    BindingListResponse,
    ConfigurationSnapshot,
    ConfigurationUpdate,
    ExtensionAdvertisement,
    HarnessError,
    ListRequest,
    PluginListResponse,
    paginate,
)

from . import acp as acp_map
from ._unavailable_tools import UnavailableTools
from .acp_store import (
    SessionRecord,
    SessionStore,
    finalize_usage,
    merge_usage,
    usage_payload,
)
from .composition import Composition, compose_definition
from .definitions import (
    AgentDefinition,
    DefinitionManifest,
    manifest_from_file,
)
from .policy import ChangeOutcome, changes_payload_hash, jsonable_config
from .registry import PluginRegistry

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

__all__ = [
    "HarnessACPAgent",
    "HostBundle",
    "HostPolicy",
    "HostSetup",
    "build_agent",
    "build_local_bundle",
    "build_router",
    "build_store",
    "load_definition",
    "main",
    "serve_agent",
]

_LOG = logging.getLogger("harness.runtime.host")

_EXTENSION_METHODS = (
    "_harness/capabilities/list",
    "_harness/plugins/list",
    "_harness/configuration/get",
    "_harness/configuration/update",
    "_harness/configuration/changed",
    "_harness/session/rename",
)


@dataclass(frozen=True, slots=True)
class HostSetup:
    """Everything the CLI (or a worker supervisor) tells the runtime builder."""

    definition: str
    store_path: str | None = None
    model: str | None = None
    models: tuple[str, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    hosted: bool = False
    principal: str = "local"
    deployment_id: str = "local"
    allowed_cwds: tuple[str, ...] | None = None
    local_binding_creation: bool = True

    @property
    def advertise_local_binding_creation(self) -> bool:
        return bool(self.local_binding_creation) and not self.hosted

    def advertised_models(self) -> tuple[str, ...]:
        values = list(dict.fromkeys(model for model in self.models if model))
        if self.model and self.model not in values:
            values.insert(0, self.model)
        return tuple(values)

    def default_model(self) -> str | None:
        values = self.advertised_models()
        return self.model or (values[0] if values else None)


@dataclass
class HostBundle:
    """The pieces a host process serves: one definition and one durable store."""

    definition: AgentDefinition
    registry: PluginRegistry
    store: SessionStore
    close_store: bool = False
    model_resolver: Callable[[str], Any] | None = None


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """Host-owned policy: identity, workspace bounds, and model selection."""

    principal: str = "local"
    deployment_id: str = "local"
    hosted: bool = False
    allowed_cwds: tuple[str, ...] | None = None
    allow_local_binding_creation: bool = True
    models: tuple[str, ...] = ()
    default_model: str | None = None
    thinking_models: Mapping[str, str] = field(default_factory=dict)
    default_effort: str | None = None
    model_settings: Mapping[str, Any] = field(default_factory=dict)
    resources: Mapping[str, Any] = field(default_factory=dict)
    default_cwd: str | None = None

    def __post_init__(self) -> None:
        roots = self.allowed_cwds
        if roots is not None:
            object.__setattr__(
                self, "allowed_cwds", tuple(acp_map.canonical_root(root) for root in roots)
            )
        if self.hosted and self.allowed_cwds is None:
            raise HarnessError(
                "DEPLOYMENT_UNAVAILABLE",
                "A hosted worker requires HARNESS_ALLOWED_CWDS (a JSON list, possibly empty).",
            )

    def fallback_cwd(self) -> str:
        return acp_map.canonical_root(self.default_cwd or os.getcwd())

    @property
    def can_create_bindings(self) -> bool:
        return self.allow_local_binding_creation and not self.hosted

    def extension_methods(self) -> tuple[str, ...]:
        if self.can_create_bindings:
            return _EXTENSION_METHODS
        return tuple(method for method in _EXTENSION_METHODS if method != "_harness/plugins/list")

    def effort_support(self, model: str | None) -> str | None:
        return self.thinking_models.get(model) if model else None

    def validate_cwd(self, cwd: Any) -> str:
        return acp_map.validate_requested_cwd(cwd, allowed=self.allowed_cwds, hosted=self.hosted)


@dataclass(frozen=True)
class SessionDeps:
    """Dependencies that let the adapter dispatch a run to this session's native agent."""

    session_id: str
    composition: Composition


@dataclass
class _UsageReporting(AbstractCapability[Any]):
    """Publish measured usage after each response, before tools continue the turn."""

    report: Callable[[ModelResponse], Awaitable[None]]

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        await self.report(response)
        return response


class _ComposedAgent:
    """A narrow agent-shaped dispatcher over one native agent per session.

    The upstream adapter owns one agent instance for a connection, while a session's
    definition factory must be constructed with that session's complete managed
    capabilities. The adapter already passes per-session ``deps`` to every run, so this
    wrapper selects the already-composed native agent from those deps. It does not run
    turns itself.
    """

    def __init__(
        self, *, name: str, report_usage: Callable[[str, ModelResponse], Awaitable[None]]
    ) -> None:
        self.name = name
        self._report_usage = report_usage
        self.output_type = str
        self.toolsets: list[Any] = []

    @staticmethod
    def _select(deps: Any) -> Any:
        if not isinstance(deps, SessionDeps):
            raise RuntimeError("ACP run without a composed session dependency")
        return deps.composition.agent

    def _capabilities(self, deps: SessionDeps) -> list[AbstractCapability[Any]]:
        async def report(response: ModelResponse) -> None:
            await self._report_usage(deps.session_id, response)

        return [*(deps.composition.config.capabilities or ()), _UsageReporting(report=report)]

    def run_stream_events(self, *args: Any, **kwargs: Any) -> Any:
        deps = kwargs.get("deps")
        composition = deps.composition if isinstance(deps, SessionDeps) else None
        target = composition.agent if composition is not None else self._select(deps)
        if isinstance(deps, SessionDeps) and composition is not None:
            kwargs["deps"] = composition.deps
            kwargs["capabilities"] = self._capabilities(deps)
            kwargs["toolsets"] = composition.config.toolsets
            kwargs["workspace"] = composition.config.workspace
        return target.run_stream_events(*args, **kwargs)

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        deps = kwargs.get("deps")
        composition = deps.composition if isinstance(deps, SessionDeps) else None
        target = composition.agent if composition is not None else self._select(deps)
        if isinstance(deps, SessionDeps) and composition is not None:
            kwargs["deps"] = composition.deps
            kwargs["capabilities"] = self._capabilities(deps)
            kwargs["toolsets"] = composition.config.toolsets
            kwargs["workspace"] = composition.config.workspace
        return await target.run(*args, **kwargs)


def _iso_millis(value: int | None) -> str | None:
    if not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


def _prompt_text(prompt: Sequence[Any]) -> str:
    parts: list[str] = []
    for block in prompt:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
            continue
        if isinstance(block, Mapping):
            value = block.get("text")
            if isinstance(value, str) and value:
                parts.append(value)
    return " ".join(" ".join(parts).split())[:80]


class HarnessACPAgent(PydanticAIACPAgent[Any, str]):
    """ACP agent whose sessions are composed native Pydantic AI agents."""

    def __init__(
        self,
        bundle: HostBundle,
        policy: HostPolicy | None = None,
    ) -> None:
        self.bundle = bundle
        self.policy = policy or HostPolicy()
        self.definition = bundle.definition
        bundle.registry.allow_local_binding_creation = self.policy.can_create_bindings
        self.store = bundle.store.scoped(self.policy.principal, self.policy.deployment_id)
        self._compositions: dict[str, Composition] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._turn_usage: dict[str, dict[str, Any]] = {}
        self._config_requests: dict[str, dict[str, tuple[str, ConfigurationSnapshot]]] = {}
        self._pending_initial: list[Any] | None = None
        self._pending_effort: str | None = None
        self._new_lock = asyncio.Lock()
        self._client_conn: Any | None = None
        models = tuple(self.policy.models)
        default = self.policy.default_model or (models[0] if models else None)
        self._default_model = default
        super().__init__(
            cast(
                AbstractAgent[Any, str],
                _ComposedAgent(
                    name=self.definition.name or self.definition.agent_id,
                    report_usage=self._report_model_usage,
                ),
            ),
            name=self.definition.name or self.definition.agent_id,
            version=self.definition.version,
            session_config=self._compose_session,
            session_store=self.store,
            models=models or None,
            model_resolver=bundle.model_resolver,
            prompt_capabilities=schema.PromptCapabilities(embedded_context=True),
        )

    # --- connection and lifecycle ---------------------------------------

    def on_connect(self, conn: Any) -> None:
        self._client_conn = conn
        super().on_connect(conn)

    async def shutdown(self) -> None:
        """Close every live session through the public adapter method, then the store."""
        for session_id in list(self._compositions):
            with contextlib.suppress(Exception):
                await self.close_session(session_id)
        self._compositions.clear()
        if self.bundle.close_store:
            with contextlib.suppress(Exception):
                await self.store.backend.close()

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse | None:
        response = await super().close_session(session_id, **kwargs)
        self._compositions.pop(session_id, None)
        self._turn_locks.pop(session_id, None)
        self._config_requests.pop(session_id, None)
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
            client_capabilities=client_capabilities,
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
        )
        return response.model_copy(
            update={
                "agent_capabilities": capabilities,
                "field_meta": {"harness": advertisement.to_wire()},
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
            self._reject_additional_directories(additional_directories)
            self._reject_client_mcp(mcp_servers)
            self.policy.validate_cwd(cwd)
            meta = kwargs.get("harness")
            initial = acp_map.initial_configuration_changes(meta)
            if not self.policy.can_create_bindings:
                added = [change for change in initial if getattr(change, "type", None) == "add"]
                if added:
                    raise HarnessError(
                        "POLICY_DENIED",
                        "Local binding creation is prohibited for this deployment.",
                        {"bindingIds": [change.binding_id for change in added]},
                    )
            effort = None
            if isinstance(meta, Mapping):
                value = meta.get("effort")
                if isinstance(value, str):
                    effort = value
            async with self._new_lock:
                self._pending_initial = list(initial) or None
                self._pending_effort = effort
                try:
                    response = await super().new_session(
                        cwd,
                        additional_directories=None,
                        mcp_servers=None,
                        **kwargs,
                    )
                finally:
                    self._pending_initial = None
                    self._pending_effort = None
            return response.model_copy(
                update={"config_options": self._options_for(response.session_id)}
            )
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
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
            self._reject_additional_directories(additional_directories)
            self._reject_client_mcp(mcp_servers)
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
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except RequestError:
            raise
        if response is None:
            return response
        return response.model_copy(update={"config_options": self._options_for(session_id)})

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> schema.ListSessionsResponse:
        cwd_filter = self.policy.validate_cwd(cwd) if cwd is not None else None
        records = await self.store.list()
        entries = [
            schema.SessionInfo(
                session_id=record.session_id,
                cwd=record.cwd,
                title=record.title,
                updated_at=_iso_millis(record.last_active_at),
            )
            for record in records
            if cwd_filter is None or acp_map.canonical_root(record.cwd) == cwd_filter
        ]
        page, next_cursor = paginate(entries, cursor=cursor, limit=50)
        return schema.ListSessionsResponse(sessions=page, next_cursor=next_cursor)

    async def delete_session(self, session_id: str, **kwargs: Any) -> DeleteSessionResponse | None:
        record = await self.store.get(session_id)
        if record is None:
            raise RequestError.invalid_params(
                {"sessionId": session_id, "reason": "no stored session with this id"}
            )
        await self.close_session(session_id)
        await self.store.delete(session_id)
        return DeleteSessionResponse()

    # --- prompts, effort, and metadata -----------------------------------

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        async with self._turn_lock(session_id):
            self._turn_usage[session_id] = {}
            response = None
            try:
                response = await super().prompt(session_id, prompt, **kwargs)
                return response
            finally:
                increment = self._turn_usage.pop(session_id, {})
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
                    await self._send_session_usage(session_id, finalize_usage(totals))
                    if title and title != record.title:
                        await self._send_session_title(session_id, title)

    async def _report_model_usage(self, session_id: str, response: ModelResponse) -> None:
        current = self._turn_usage.get(session_id)
        if current is None:
            return
        increment = usage_payload(response.usage)
        increment["requests"] = 1
        current = merge_usage(current, increment)
        self._turn_usage[session_id] = current
        record = await self.store.get(session_id)
        await self._send_session_usage(
            session_id,
            finalize_usage(merge_usage(record.usage if record is not None else {}, current)),
        )

    async def _send_session_usage(self, session_id: str, totals: Mapping[str, Any]) -> None:
        if self._client_conn is None:
            return
        with contextlib.suppress(Exception):
            await self._client_conn.session_update(
                session_id=session_id,
                update=schema.SessionInfoUpdate(
                    session_update="session_info_update",
                    field_meta={"harness": {"sessionUsage": {"scope": "session", **totals}}},
                ),
            )

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse | None:
        if config_id == acp_map.EFFORT_OPTION_ID:
            composition = self._require_composition(session_id)
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
            await self.store.update(
                session_id,
                effort=value,
                touch=True,
            )
            return schema.SetSessionConfigOptionResponse(
                config_options=self._options_for(session_id)
            )
        response = await super().set_config_option(config_id, session_id, value, **kwargs)
        if config_id == acp_map.MODEL_OPTION_ID and self._default_model is not None:
            composition = self._compositions.get(session_id)
            if composition is not None and isinstance(value, str):
                support = self.policy.effort_support(value)
                composition.effort.set_model(value, support)
                support_after = composition.effort.support
                keep = composition.effort.effort
                if keep is not None and acp_map.effort_option(keep, support=support_after) is None:
                    composition.effort.effort = None
                    keep = None
                await self.store.update(
                    session_id,
                    effort=keep or EFFORT_DEFAULT,
                    touch=True,
                )
        if response is None:
            return response
        return schema.SetSessionConfigOptionResponse(config_options=self._options_for(session_id))

    # --- project extension ------------------------------------------------

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if method == "harness/session/rename":
                from harness.protocol.models import SessionTitleUpdate

                request = SessionTitleUpdate.model_validate(params)
                if await self.store.get(request.session_id) is None:
                    raise HarnessError("SESSION_CLOSED", "Load the session before renaming it.")
                await self.store.update(request.session_id, title=request.title, touch=True)
                await self._send_session_title(request.session_id, request.title)
                return {"sessionId": request.session_id, "title": request.title}
            if method == "harness/capabilities/list":
                request = ListRequest.model_validate(dict(params))
                if not request.session_id:
                    raise HarnessError(
                        "INVALID_CONFIGURATION", "sessionId is required for a capability listing."
                    )
                composition = self._require_composition(request.session_id)
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
            if method == "harness/plugins/list":
                if not self.policy.can_create_bindings:
                    raise RequestError.method_not_found("_harness/plugins/list")
                request = ListRequest.model_validate(dict(params))
                page, next_cursor = paginate(
                    self.bundle.registry.catalog(),
                    cursor=request.cursor,
                    limit=request.limit or 50,
                )
                return PluginListResponse(items=page, next_cursor=next_cursor).to_wire()
            if method == "harness/configuration/get":
                session_id = params.get("sessionId", params.get("session_id"))
                if not isinstance(session_id, str) or not session_id:
                    raise HarnessError("INVALID_CONFIGURATION", "sessionId is required.")
                composition = self._require_composition(session_id)
                return composition.snapshot.to_wire()
            if method == "harness/configuration/update":
                request = ConfigurationUpdate.model_validate(dict(params))
                return await self._update_configuration(request)
            raise RequestError.method_not_found(f"_{method}")
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except ValidationError as exc:
            raise acp_map.harness_rpc_error(
                HarnessError(
                    "INVALID_CONFIGURATION",
                    "The extension request payload is invalid.",
                    {"errors": exc.errors()[:10]},
                )
            ) from exc
        except RequestError:
            raise

    async def _update_configuration(self, request: ConfigurationUpdate) -> dict[str, Any]:
        session_id = request.session_id
        if not self.policy.can_create_bindings and any(
            isinstance(change, AddChange) for change in request.changes
        ):
            raise HarnessError(
                "POLICY_DENIED",
                "Local binding creation is prohibited for this deployment.",
            )
        request_hash = f"{changes_payload_hash(request.changes)}|{request.expected_revision}"
        cached = self._config_requests.get(session_id, {}).get(request.request_id)
        if cached is not None:
            if cached[0] != request_hash:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    "A request id cannot be reused with a different configuration payload.",
                    {"requestId": request.request_id},
                )
            return cached[1].to_wire()
        async with self._turn_lock(session_id):
            composition = self._require_composition(session_id)
            if request.expected_revision != composition.snapshot.selected_revision:
                raise HarnessError(
                    "REVISION_CONFLICT",
                    "The configuration changed since the client's expected revision.",
                    {
                        "snapshot": composition.snapshot.to_wire(),
                        "expectedRevision": request.expected_revision,
                    },
                )
            outcome = composition.catalog.apply_changes(list(request.changes))
            if outcome.noop:
                snapshot = composition.snapshot
                self._config_requests.setdefault(session_id, {})[request.request_id] = (
                    request_hash,
                    snapshot,
                )
                return snapshot.to_wire()
            record = await self.store.get(session_id)
            if record is None:
                raise HarnessError(
                    "SESSION_CLOSED", "Load the session before updating configuration."
                )
            next_revision = composition.snapshot.selected_revision + 1
            refreshed = await compose_definition(
                self.definition,
                self.bundle.registry,
                resources=self._resources(self.policy.validate_cwd(record.cwd)),
                session_id=session_id,
                principal=self.policy.principal,
                deployment_id=self.policy.deployment_id,
                saved_bindings=self._bindings_payload(outcome),
                model_settings=self.policy.model_settings,
                effort=composition.effort.effort,
                effort_support=self.policy.effort_support(
                    composition.effort.model or self._default_model
                ),
                deps=composition.deps,
            )
            refreshed.effort.model = composition.effort.model or self._default_model
            snapshot = refreshed.catalog.snapshot(
                selected_revision=next_revision,
                active_revision=next_revision,
                application_status="applied",
            )
            refreshed.snapshot = snapshot
            refreshed.config = type(refreshed.config)(
                deps=SessionDeps(session_id=session_id, composition=composition),
                capabilities=refreshed.config.capabilities,
                toolsets=refreshed.config.toolsets,
                workspace=refreshed.config.workspace,
            )
            await self.store.update(
                session_id,
                bindings=refreshed.catalog.export_bindings(),
                touch=True,
            )
            # Preserve the Composition/SessionDeps identity used by the adapter's live
            # sessions; only its contents are replaced.
            composition.agent = refreshed.agent
            composition.catalog = refreshed.catalog
            composition.snapshot = refreshed.snapshot
            composition.resources = refreshed.resources
            composition.effort = refreshed.effort
            composition.config = refreshed.config
            composition.capabilities = refreshed.capabilities
            composition.workspace = refreshed.workspace
            composition.deps = refreshed.deps
            await self._apply_unavailable_tools(session_id, composition)
            self._config_requests.setdefault(session_id, {})[request.request_id] = (
                request_hash,
                snapshot,
            )
            await self._send_configuration_changed(snapshot)
            return snapshot.to_wire()

    def _bindings_payload(self, outcome: ChangeOutcome) -> dict[str, dict[str, Any]]:
        added = {binding_id: plugin_id for binding_id, plugin_id, _, _ in outcome.added}
        return {
            binding_id: {
                "enabled": enabled,
                "config": jsonable_config(config),
                **({"plugin_id": added[binding_id]} if binding_id in added else {}),
            }
            for binding_id, (enabled, config) in outcome.states.items()
        }

    async def _send_configuration_changed(self, snapshot: ConfigurationSnapshot) -> None:
        if self._client_conn is None:
            return
        with contextlib.suppress(Exception):
            await self._client_conn.ext_notification(
                "harness/configuration/changed", snapshot.to_wire()
            )

    async def _remembered_tool_names(self, session_id: str) -> frozenset[str]:
        """Function-tool names already present in the session's native history."""
        stored = await self.store.load(session_id)
        if stored is None:
            return frozenset()
        names: set[str] = set()
        for message in stored.messages:
            for part in getattr(message, "parts", ()):
                if isinstance(part, ToolCallPart):
                    name = getattr(part, "tool_name", None)
                    if isinstance(name, str) and name:
                        names.add(name)
        return frozenset(names)

    async def _apply_unavailable_tools(self, session_id: str, composition: Composition) -> None:
        """Keep disabled tool names callable as failures for existing conversations.

        DeepSeek emits raw DSML text instead of structured calls when a previously used
        tool disappears from the request. A stub keeps the declaration and returns a
        clear ``ToolFailed`` if the model still calls it. Active tools always win.
        """
        disabled = tuple(
            offering.binding_id
            for offering in composition.catalog.ordered_offerings()
            if not offering.enabled
        )
        capabilities = [
            capability
            for capability in (composition.config.capabilities or ())
            if not isinstance(capability, UnavailableTools)
        ]
        if disabled:
            remembered = await self._remembered_tool_names(session_id)
            if remembered:
                capabilities.insert(
                    0,
                    UnavailableTools(
                        disabled_bindings=disabled,
                        remembered_tools=remembered,
                    ),
                )
        composition.config = type(composition.config)(
            deps=composition.config.deps,
            capabilities=capabilities,
            toolsets=composition.config.toolsets,
            workspace=composition.config.workspace,
        )
        composition.capabilities = capabilities

    # --- composition ------------------------------------------------------

    def _resources(self, cwd: str) -> dict[str, Any]:
        values = dict(self.policy.resources)
        model = self._default_model
        if model is not None:
            values.setdefault("model.default", model)
        for requirement in self.definition.resources:
            if requirement.kind == "workspace":
                values.setdefault(requirement.name, cwd)
        return values

    async def _compose_session(self, session: Any) -> Any:
        record = await self.store.get(session.session_id)
        initial = None
        saved = None
        effort = self.policy.default_effort
        if record is None:
            initial = self._pending_initial
            if self._pending_effort is not None:
                effort = self._pending_effort
        else:
            saved = record.bindings
            effort = record.effort or None
            if effort == EFFORT_DEFAULT:
                effort = None
        cwd = self.policy.validate_cwd(session.cwd)
        model = (record.model if record is not None else None) or self._default_model
        support = self.policy.effort_support(model)
        composition = await compose_definition(
            self.definition,
            self.bundle.registry,
            resources=self._resources(cwd),
            session_id=session.session_id,
            principal=self.policy.principal,
            deployment_id=self.policy.deployment_id,
            initial_configuration=initial,
            saved_bindings=saved,
            model_settings=self.policy.model_settings,
            effort=effort,
            effort_support=support,
        )
        composition.effort.model = model
        self._compositions[session.session_id] = composition
        if record is None:
            await self.store.create(
                SessionRecord(
                    session_id=session.session_id,
                    principal=self.policy.principal,
                    deployment_id=self.policy.deployment_id,
                    agent_id=self.definition.agent_id,
                    definition_version=self.definition.version,
                    cwd=cwd,
                    effort=effort or EFFORT_DEFAULT,
                    bindings=composition.catalog.export_bindings(),
                )
            )
        else:
            await self.store.update(
                session.session_id,
                bindings=composition.catalog.export_bindings(),
                touch=True,
            )
        composition.config = type(composition.config)(
            deps=SessionDeps(session_id=session.session_id, composition=composition),
            capabilities=composition.config.capabilities,
            toolsets=composition.config.toolsets,
            workspace=composition.config.workspace,
        )
        await self._apply_unavailable_tools(session.session_id, composition)
        return composition.config

    def _require_composition(self, session_id: str) -> Composition:
        composition = self._compositions.get(session_id)
        if composition is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                "The session is not open in this host; load it before reading configuration.",
                {"sessionId": session_id},
            )
        return composition

    def _options_for(self, session_id: str) -> list[Any]:
        composition = self._compositions.get(session_id)
        support = composition.effort.support if composition is not None else None
        current_effort = (
            composition.effort.effort if composition is not None else None
        ) or EFFORT_DEFAULT
        options: list[Any] = []
        model = self.session_model_option(session_id)
        if model is not None:
            options.append(model)
        effort = acp_map.effort_option(current_effort, support=support)
        if effort is not None:
            options.append(effort)
        return options

    async def _send_session_title(self, session_id: str, title: str) -> None:
        if self._client_conn is None:
            return
        with contextlib.suppress(Exception):
            await self._client_conn.session_update(
                session_id=session_id,
                update=schema.SessionInfoUpdate(session_update="session_info_update", title=title),
            )

    # --- small validations ------------------------------------------------

    @staticmethod
    def _reject_additional_directories(additional_directories: Sequence[str] | None) -> None:
        if additional_directories:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                "additionalDirectories is not supported by this host.",
                {"count": len(additional_directories)},
            )

    def _reject_client_mcp(self, mcp_servers: Sequence[Any] | None) -> None:
        if not mcp_servers:
            return
        raise acp_map.mcp_forbidden_error(hosted=self.policy.hosted, servers=mcp_servers)


# ---------------------------------------------------------------------------
# Definition loading and serving
# ---------------------------------------------------------------------------


def definition_from_manifest(manifest: DefinitionManifest) -> AgentDefinition:
    """Build a runtime definition from a parsed manifest envelope."""
    return manifest.to_definition()


_REGISTRY_HOOK_NAMES = ("build_registry", "registry")


async def _await_result(awaitable: Any) -> Any:
    return await awaitable


def _coerce_definition(target: Any, *, source: str) -> AgentDefinition:
    if callable(target) and not isinstance(target, AgentDefinition | DefinitionManifest):
        target = target()
        target = asyncio.run(_await_result(target)) if inspect.isawaitable(target) else target
    if isinstance(target, DefinitionManifest):
        return definition_from_manifest(target)
    if isinstance(target, AgentDefinition):
        return target
    if isinstance(target, Mapping):
        return AgentDefinition.model_validate(dict(target))
    raise HarnessError(
        "DEPLOYMENT_UNAVAILABLE",
        f"Definition source {source!r} produced {type(target).__name__}, not an AgentDefinition.",
        {"source": source},
    )


def _registry_from_module(module: Any) -> PluginRegistry | None:
    for name in _REGISTRY_HOOK_NAMES:
        value = getattr(module, name, None)
        if value is None:
            continue
        if isinstance(value, PluginRegistry):
            return value
        if callable(value):
            try:
                candidate = value()
            except TypeError:
                continue
            except Exception:
                _LOG.exception("definition registry hook %r failed", name)
                continue
            if isinstance(candidate, PluginRegistry):
                return candidate
    return None


def _load_definition_source(source: str) -> tuple[AgentDefinition, PluginRegistry | None]:
    path = Path(source).expanduser()
    if path.exists() and path.is_file():
        return definition_from_manifest(manifest_from_file(path)), None
    module_name, separator, attribute = source.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise HarnessError(
            "DEPLOYMENT_UNAVAILABLE",
            f"Cannot import definition module {module_name!r}: {exc}",
            {"source": source},
        ) from exc
    if separator:
        try:
            target: Any = reduce(getattr, attribute.split("."), module)
        except AttributeError as exc:
            raise HarnessError(
                "DEPLOYMENT_UNAVAILABLE",
                f"Definition module {module_name!r} has no attribute {attribute!r}.",
                {"source": source},
            ) from exc
    else:
        target = getattr(module, "definition", None)
        if target is None:
            raise HarnessError(
                "DEPLOYMENT_UNAVAILABLE",
                f"{module_name!r} defines no 'definition' attribute; use module:attribute.",
                {"source": source},
            )
    return _coerce_definition(target, source=source), _registry_from_module(module)


def load_definition(source: str) -> AgentDefinition:
    """Load an agent definition from an import reference or a manifest file."""
    return _load_definition_source(source)[0]


def build_store(configured: str | Path | None) -> SessionStore:
    """Select SQLite or PostgreSQL from a path/DSN without an implicit fallback."""
    from .postgres import PostgresSessionStore, is_postgres_dsn
    from .store import SQLiteSessionStore

    value = str(configured) if configured is not None else None
    if value and is_postgres_dsn(value):
        return PostgresSessionStore(value)
    return SQLiteSessionStore(value) if value else SQLiteSessionStore()


def build_local_bundle(setup: HostSetup) -> HostBundle:
    """The built-in composition: installed plugins + durable store + one definition."""
    definition, hook_registry = _load_definition_source(setup.definition)
    registry = hook_registry or PluginRegistry()
    registry.discover()
    store = build_store(setup.store_path)
    return HostBundle(
        definition=definition,
        registry=registry,
        store=store,
        close_store=True,
    )


def build_agent(bundle: HostBundle, setup: HostSetup | None = None) -> HarnessACPAgent:
    """Compose the ACP agent and host policy from a runtime bundle."""
    from .local import thinking_support

    models = setup.advertised_models() if setup else ()
    resources = dict(setup.resources) if setup else {}
    if setup and setup.model and "model.default" not in resources:
        resources["model.default"] = setup.model
    policy = HostPolicy(
        principal=setup.principal if setup else "local",
        deployment_id=setup.deployment_id if setup else "local",
        hosted=bool(setup.hosted) if setup else False,
        allowed_cwds=setup.allowed_cwds if setup else None,
        allow_local_binding_creation=setup.advertise_local_binding_creation if setup else True,
        models=models,
        default_model=(setup.default_model() if setup else None),
        thinking_models=thinking_support(models),
        resources=resources,
    )
    return HarnessACPAgent(bundle, policy)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-host",
        description=(
            "Serve a Harness agent definition over ACP v1 on stdio. Diagnostics go to "
            "stderr; stdout carries newline-delimited JSON-RPC only."
        ),
    )
    parser.add_argument(
        "definition",
        nargs="?",
        help="agent definition import reference (package.module:attribute) or manifest path",
    )
    parser.add_argument(
        "--definition",
        dest="definition_flag",
        help="definition source (alternative to the positional)",
    )
    parser.add_argument("--manifest", help="YAML/JSON agent definition manifest path")
    parser.add_argument(
        "--store",
        help="SQLite session store path or PostgreSQL DSN (enables load/list/delete)",
    )
    parser.add_argument("--model", help="default model id for this deployment")
    parser.add_argument("--models", help="comma-separated model ids offered to ACP clients")
    parser.add_argument(
        "--resource",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="named definition resource value (repeatable)",
    )
    parser.add_argument(
        "--allowed-cwd",
        action="append",
        dest="allowed_cwds",
        help="local workspace root (repeatable)",
    )
    parser.add_argument("--principal", help="local principal id (default: local)")
    parser.add_argument("--deployment-id", help="local deployment id (default: local)")
    parser.add_argument("--runtime-module", help="module exposing build_host_bundle(setup)")
    parser.add_argument(
        "--hosted", action="store_true", help="hosted worker: identity comes from HARNESS_* env"
    )
    parser.add_argument("--log-level", default="warning")
    return parser


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise _UsageError(f"{name} must be set by the server in hosted mode")
    return value


def setup_from_args(args: argparse.Namespace) -> HostSetup:
    """Resolve CLI arguments + the trusted hosted environment into a :class:`HostSetup`."""
    hosted = bool(args.hosted) or _env_flag("HARNESS_HOSTED")
    sources = [
        source for source in (args.definition, args.definition_flag, args.manifest) if source
    ]
    if not sources:
        raise _UsageError("an agent definition import reference or manifest path is required")
    if len(sources) > 1:
        raise _UsageError("provide exactly one definition source")
    definition = sources[0]
    resources: dict[str, str] = {}
    for item in args.resource:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            raise _UsageError(f"--resource expects NAME=VALUE, got {item!r}")
        resources[name.strip()] = value
    models = tuple(model.strip() for model in (args.models or "").split(",") if model.strip())
    if hosted:
        supplied = {
            "--principal": args.principal,
            "--deployment-id": args.deployment_id,
            "--allowed-cwd": args.allowed_cwds,
            "--store": args.store,
        }
        conflicting = [flag for flag, value in supplied.items() if value]
        if conflicting:
            raise _UsageError(
                ", ".join(conflicting)
                + " cannot be set in hosted mode; the server supplies HARNESS_PRINCIPAL, "
                "HARNESS_DEPLOYMENT_ID, HARNESS_ALLOWED_CWDS and HARNESS_STORE"
            )
        principal = _require_env("HARNESS_PRINCIPAL")
        deployment_id = _require_env("HARNESS_DEPLOYMENT_ID")
        store_path = _require_env("HARNESS_STORE")
        raw_allowed = _require_env("HARNESS_ALLOWED_CWDS")
        try:
            allowed = json.loads(raw_allowed)
        except json.JSONDecodeError as exc:
            raise _UsageError("HARNESS_ALLOWED_CWDS must be a JSON array of path strings") from exc
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise _UsageError("HARNESS_ALLOWED_CWDS must be a JSON array of path strings")
        allowed_cwds: tuple[str, ...] | None = tuple(allowed)
        local_binding_creation = False
    else:
        principal = args.principal or "local"
        deployment_id = args.deployment_id or "local"
        store_path = args.store
        allowed_cwds = tuple(args.allowed_cwds) if args.allowed_cwds else None
        local_binding_creation = True
    return HostSetup(
        definition=definition,
        store_path=store_path,
        model=args.model,
        models=models,
        resources=resources,
        hosted=hosted,
        principal=principal,
        deployment_id=deployment_id,
        allowed_cwds=allowed_cwds,
        local_binding_creation=local_binding_creation,
    )


def resolve_builder(module_name: str | None) -> Callable[[HostSetup], HostBundle]:
    if not module_name:
        return build_local_bundle
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise HarnessError(
            "DEPLOYMENT_UNAVAILABLE",
            f"Cannot import runtime module {module_name!r}: {exc}",
            {"module": module_name},
        ) from exc
    builder = getattr(module, "build_host_bundle", None)
    if not callable(builder):
        raise HarnessError(
            "DEPLOYMENT_UNAVAILABLE",
            f"Runtime module {module_name!r} exposes no build_host_bundle(setup) function.",
            {"module": module_name},
        )
    return builder


def build_router(agent: Any, *, use_unstable_protocol: bool = True) -> Any:
    """Build the SDK agent router plus the standard session-delete route."""
    router = build_agent_router(agent, use_unstable_protocol=use_unstable_protocol)
    router.route_request(
        AGENT_METHODS["session_delete"],
        DeleteSessionRequest,
        agent,
        "delete_session",
        adapt_result=normalize_result,
    )
    return router


class _AgentClientChannel:
    """The client-facing half of the SDK connection, over our router."""

    def __init__(self, connection: Connection) -> None:
        self._conn = connection

    async def session_update(
        self, session_id: str, update: acp_map.SessionUpdate, **kwargs: Any
    ) -> None:
        notification = schema.SessionNotification(
            session_id=session_id, update=update, field_meta=kwargs or None
        )
        await notify_model(self._conn, CLIENT_METHODS["session_update"], notification)

    async def request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        request = schema.RequestPermissionRequest(
            options=options,
            session_id=session_id,
            tool_call=tool_call,
            field_meta=kwargs or None,
        )
        return await request_model(
            self._conn,
            CLIENT_METHODS["session_request_permission"],
            request,
            schema.RequestPermissionResponse,
        )

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._conn.send_request(f"_{method}", params)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        await self._conn.send_notification(f"_{method}", params)


async def serve_agent(
    agent: HarnessACPAgent,
    *,
    input_stream: Any = None,
    output_stream: Any = None,
    use_unstable_protocol: bool = True,
) -> None:
    """Serve an agent over stdio (newline-delimited ACP JSON); diagnostics on stderr."""
    await agent.store.backend.initialize()
    if input_stream is None and output_stream is None:
        reader, writer = await stdio_streams(limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES)
    else:
        writer, reader = input_stream, output_stream
    router = build_router(agent, use_unstable_protocol=use_unstable_protocol)
    connection = Connection(router, writer, reader, listening=False)
    agent.on_connect(_AgentClientChannel(connection))
    try:
        await connection.main_loop()
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(connection.close())


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        setup = setup_from_args(args)
    except _UsageError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover
    try:
        bundle = resolve_builder(args.runtime_module)(setup)
        agent = build_agent(bundle, setup)
    except HarnessError as exc:
        print(f"harness-host: {exc.message}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_serve_until_closed(agent))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    return 0


async def _serve_until_closed(agent: HarnessACPAgent) -> None:
    try:
        await agent.store.backend.initialize()
        await serve_agent(agent)
    finally:
        await agent.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
