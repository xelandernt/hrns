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
from collections.abc import Callable, Mapping, Sequence
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
from pydantic_ai.agent import AbstractAgent
from pydantic_ai_harness.experimental import HarnessExperimentalWarning
from pydantic_ai_harness.experimental.acp import (
    PydanticAIACPAgent,
)

from harness.config import EFFORT_DEFAULT
from harness.protocol import (
    EXTENSION_METHODS,
    BindingListResponse,
    CommandCancelRequest,
    CommandInvokeRequest,
    CommandListRequest,
    CommandListResponse,
    ConfigurationUpdate,
    ExtensionAdvertisement,
    HarnessError,
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
from .command_service import CommandService
from .configuration_service import ConfigurationService
from .definitions import (
    AgentDefinition,
    DefinitionManifest,
    manifest_from_file,
)
from .host_support import (
    HostBundle,
    HostPolicy,
    HostSetup,
    _ComposedAgent,
    _prompt_text,
    build_store,
)
from .registry import PluginRegistry
from .resources import MODEL, ResourceSet
from .session_service import SessionService

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

__all__ = [
    "HarnessACPAgent",
    "build_agent",
    "build_local_bundle",
    "build_router",
    "load_definition",
    "main",
    "serve_agent",
]

_LOG = logging.getLogger("harness.runtime.host")

_EXTENSION_METHODS = tuple(EXTENSION_METHODS)


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
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self.config = ConfigurationService(
            agent=self,
            store=self.store,
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
            version=self.definition.version,
            session_config=self.sessions.compose_session,
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
            self.sessions.reject_additional_directories(additional_directories)
            self.sessions.reject_client_mcp(mcp_servers)
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
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
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

    # --- prompts, effort, and metadata -----------------------------------

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        async with self._turn_lock(session_id):
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
            await self.store.update(
                session_id,
                effort=value,
                touch=True,
            )
            return schema.SetSessionConfigOptionResponse(
                config_options=self.sessions.options_for(session_id)
            )
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
                await self.store.update(
                    session_id,
                    effort=keep or EFFORT_DEFAULT,
                    touch=True,
                )
        if response is None:
            return response
        return schema.SetSessionConfigOptionResponse(
            config_options=self.sessions.options_for(session_id)
        )

    # --- project extension ------------------------------------------------

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if method == "harness/session/rename":
                from harness.protocol.models import SessionTitleUpdate

                request = SessionTitleUpdate.model_validate(params)
                if await self.store.get(request.session_id) is None:
                    raise HarnessError("SESSION_CLOSED", "Load the session before renaming it.")
                await self.store.update(request.session_id, title=request.title, touch=True)
                await self.sessions.send_session_title(request.session_id, request.title)
                return {"sessionId": request.session_id, "title": request.title}
            if method == "harness/session/steer":
                request = SteerRequest.model_validate(dict(params))
                if await self.store.get(request.session_id) is None:
                    raise HarnessError("SESSION_CLOSED", "Load the session before steering it.")
                channel = self.sessions.steering.get(request.session_id)
                if channel is None or not channel.enqueue(request.text):
                    raise HarnessError(
                        "TURN_NOT_RUNNING",
                        "The turn is no longer accepting steering; send the message as a prompt.",
                    )
                return {"sessionId": request.session_id, "steered": True}
            if method == "harness/capabilities/list":
                request = ListRequest.model_validate(dict(params))
                if not request.session_id:
                    raise HarnessError(
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
                composition = self.sessions.require_composition(session_id)
                return composition.snapshot.to_wire()
            if method == "harness/configuration/update":
                request = ConfigurationUpdate.model_validate(dict(params))
                return await self._update_configuration(request)
            if method == "harness/commands/list":
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
            if method == "harness/commands/invoke":
                request = CommandInvokeRequest.model_validate(dict(params))
                return await self.commands.invoke(request)
            if method == "harness/commands/cancel":
                request = CommandCancelRequest.model_validate(dict(params))
                if await self.store.get(request.session_id) is None:
                    raise HarnessError(
                        "SESSION_CLOSED", "Load the session before cancelling a command."
                    )
                return self.commands.cancel(request)
            if method == "harness/presentation/get":
                request = PresentationListRequest.model_validate(dict(params))
                self.sessions.require_composition(request.session_id)
                snapshots = await self.sessions.presentation_snapshots(
                    request.session_id, request.tool_call_ids
                )
                return PresentationListResponse(snapshots=snapshots).to_wire()
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
        return await self.config.update(request)


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


def build_local_bundle(setup: HostSetup) -> HostBundle:
    """The built-in composition: installed plugins + durable store + one definition."""
    definition, hook_registry = _load_definition_source(setup.definition)
    registry = hook_registry or PluginRegistry()
    registry.discover()
    definition = definition.resolve(registry)
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
    resources = ResourceSet(setup.resources if setup else None)
    if setup and setup.model:
        resources.bind_if_absent(MODEL, setup.model)
    policy = HostPolicy(
        principal=setup.principal if setup else "local",
        deployment_id=setup.deployment_id if setup else "local",
        hosted=setup.hosted if setup else False,
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
