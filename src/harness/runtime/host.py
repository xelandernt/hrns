"""The ``harness-host`` ACP worker: a standard ACP v1 agent over stdio.

The host is the process boundary described by the project concept: a client (the
Harness terminal or any independent ACP client) speaks ACP JSON-RPC over stdio, and
this module drives the project runtime's :class:`~harness.runtime.session.SessionManager`.
It is deliberately a *thin* adapter:

* capability policy, revisions, composition, persistence, and transcript journaling
  belong to the runtime (``docs/RUNTIME_API.md``);
* this module maps sessions, prompts, tool activity, permissions, configuration, and
  the ``_harness/*`` extension onto the official ACP SDK's public dispatch;
* per-session ACP metadata the runtime does not own (the working directory and the
  selected model option) is kept in a small sidecar store keyed by session id.

Hosted workers receive their trusted identity from the server environment
(``HARNESS_PRINCIPAL``, ``HARNESS_DEPLOYMENT_ID``, ``HARNESS_ALLOWED_CWDS``,
``HARNESS_STORE``, ``HARNESS_HOSTED=1``). Values are never taken from ACP metadata.
In hosted mode local binding creation, plugin-inventory exposure, and client-supplied
MCP servers are prohibited; working directories are validated against the canonical
allowed roots; and a loaded session keeps its recorded ``cwd``.

See ``docs/ACP.md`` for the interaction surface and
``docs/ACP_RUNTIME_CONTRACT.md`` for the runtime surface this adapter needs.
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
import re
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import reduce
from pathlib import Path
from typing import Any

import acp
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
from pydantic_ai import DeferredToolResults, ToolDenied

from harness.config import THINKING_LEVELS
from harness.protocol import (
    AddChange,
    BindingChange,
    BindingDescriptor,
    BindingListResponse,
    ConfigurationSnapshot,
    ConfigurationUpdate,
    EnableChange,
    HarnessError,
    ListRequest,
    PluginListResponse,
    paginate,
)

from . import acp as acp_map
from .acp import ToolCallPresenter
from .definitions import (
    AgentDefinition,
    DefinitionBinding,
    DefinitionManifest,
    manifest_from_file,
)
from .registry import PluginRegistry
from .resources import ResourceRequirement
from .session import SessionManager
from .store import SessionStore

__all__ = [
    "HarnessHostAgent",
    "HostBundle",
    "HostMetadataStore",
    "HostPolicy",
    "HostSetup",
    "HostedSession",
    "build_agent",
    "build_local_bundle",
    "load_definition",
    "main",
    "serve_agent",
]

_LOG = logging.getLogger("harness.runtime.host")

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class _TurnCancelled(Exception):
    """Internal marker: the client cancelled the turn at a permission dialog."""


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _event_pair(event: Any) -> tuple[str, dict[str, Any]]:
    """Normalize a runtime stream event (dict or model) into ``(type, data)``."""
    if isinstance(event, Mapping):
        data = event.get("data") or {}
        return str(event.get("type") or ""), dict(data) if isinstance(data, Mapping) else {}
    event_type = getattr(event, "type", None)
    data = getattr(event, "data", None) or {}
    return str(event_type or ""), dict(data) if isinstance(data, Mapping) else {}


def _jsonable(value: Any) -> Any:
    return acp_map.jsonable(value)


def _transcript_prompt_text(payload: Any) -> str:
    """Readable user text retained for ACP session/load replay."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, Sequence) or isinstance(payload, str | bytes):
        return str(payload) if payload is not None else ""
    parts: list[str] = []
    for block in payload:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping):
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            elif kind in {"resource", "resource_link"}:
                resource = block.get("resource")
                uri = block.get("uri")
                if isinstance(resource, Mapping):
                    uri = resource.get("uri") or uri
                    if isinstance(resource.get("text"), str):
                        parts.append(str(resource["text"]))
                        continue
                parts.append(f"[attached resource: {uri}]" if uri else "[attached resource]")
    return "\n".join(part for part in parts if part)


def _usage_total(usage: schema.Usage | None, payload: Any) -> schema.Usage | None:
    """Add one turn segment's usage onto the accumulated per-prompt usage."""
    segment = acp_map.turn_usage(payload)
    if segment is None:
        return usage
    if usage is None:
        return segment
    usage.total_tokens += segment.total_tokens
    usage.input_tokens += segment.input_tokens
    usage.output_tokens += segment.output_tokens
    for name in ("thought_tokens", "cached_read_tokens", "cached_write_tokens"):
        left = getattr(usage, name, None)
        right = getattr(segment, name, None)
        if right is not None:
            setattr(usage, name, (left or 0) + right)
    return usage


def _consume_task_result(task: "asyncio.Task[Any]") -> None:
    with contextlib.suppress(BaseException):
        task.exception()


#: Providers whose model classes accept the explicit OpenAI reasoning-effort key.
#: Custom model ids use it because the unified ``thinking`` setting is gated on a
#: model profile the registry does not have for them.
_OPENAI_EFFORT_PROVIDERS = frozenset(
    {"openai", "openai-chat", "openai-responses", "azure", "azure-responses", "deepseek"}
)


def _valid_effort(effort: str | None, support: str | None) -> str | None:
    """Normalize a stored effort against the selected model's reasoning support."""
    if support is None or effort is None:
        return None
    if effort == acp_map.EFFORT_OFF:
        return None if support == "always" else effort
    return effort if effort in THINKING_LEVELS else None


# ---------------------------------------------------------------------------
# Host configuration
# ---------------------------------------------------------------------------


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
    """The runtime pieces a host process serves.

    ``build_host_bundle(setup)``/``build_local_bundle(setup)`` are the documented
    entry points; tests may substitute an equivalent bundle.
    """

    definition: AgentDefinition
    registry: PluginRegistry
    manager: SessionManager
    store_path: str | None = None
    meta: "HostMetadataStore | None" = None


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """Host-owned policy: identity, workspace bounds, and local-extension rights."""

    principal: str = "local"
    deployment_id: str = "local"
    hosted: bool = False
    allowed_cwds: tuple[str, ...] | None = None
    allow_local_binding_creation: bool = True
    allow_client_mcp: bool = True
    models: tuple[str, ...] = ()
    default_model: str | None = None
    #: Model id -> ``"supported"`` or ``"always"`` when the model reasons.
    thinking_models: Mapping[str, str] = field(default_factory=dict)
    #: Initial unified ``thinking`` effort (``None`` means the provider default).
    default_effort: str | None = None
    #: Model settings read from local configuration at process start.
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


# ---------------------------------------------------------------------------
# Per-session ACP metadata (cwd + selected model) the runtime does not own
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionMeta:
    cwd: str
    model: str | None = None
    effort: str | None = None


class HostMetadataStore:
    """Persist ACP metadata in a session store or stdio sidecar files.

    The runtime persists conversation/config/turns; the working directory and the
    host-selected model are ACP-host concerns. Per-session files (not one index) keep
    workers of different sessions from clobbering each other. A ``None`` path means
    process-memory only unless ``store`` is supplied. Network servers pass their
    shared session store so SQLite and PostgreSQL both retain cwd/model/effort.
    """

    def __init__(
        self, store_path: str | Path | None = None, *, store: SessionStore | None = None
    ) -> None:
        self.store = store
        base: Path | None = None
        if store_path is not None and str(store_path) != ":memory:":
            base = Path(store_path).expanduser()
        self.directory: Path | None = (
            base.parent / f"{base.name}.acp-sessions" if base is not None else None
        )
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._memory: dict[str, SessionMeta] = {}

    def _path(self, session_id: str) -> Path:
        if not _SAFE_SESSION_ID.match(session_id):
            raise HarnessError(
                "INVALID_CONFIGURATION", "Invalid session id.", {"sessionId": session_id}
            )
        assert self.directory is not None
        return self.directory / f"{session_id}.json"

    async def get(self, session_id: str) -> SessionMeta | None:
        if self.store is not None:
            data = await self.store.get_acp_metadata(session_id)
            return SessionMeta(**data) if data is not None else None
        if session_id in self._memory:
            return self._memory[session_id]
        if self.directory is None:
            return None
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HarnessError(
                "DEPLOYMENT_UNAVAILABLE",
                "Stored session metadata could not be read.",
                {"sessionId": session_id, "reason": str(exc)},
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("cwd"), str):
            return None
        meta = SessionMeta(cwd=data["cwd"], model=data.get("model"), effort=data.get("effort"))
        self._memory[session_id] = meta
        return meta

    async def put(self, session_id: str, meta: SessionMeta) -> None:
        if self.store is not None:
            await self.store.put_acp_metadata(
                session_id, {"cwd": meta.cwd, "model": meta.model, "effort": meta.effort}
            )
            return
        self._memory[session_id] = meta
        if self.directory is None:
            return
        path = self._path(session_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"cwd": meta.cwd, "model": meta.model, "effort": meta.effort},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    async def delete(self, session_id: str) -> None:
        if self.store is not None:
            await self.store.delete_acp_metadata(session_id)
            return
        self._memory.pop(session_id, None)
        if self.directory is not None:
            with contextlib.suppress(FileNotFoundError):
                self._path(session_id).unlink()


# ---------------------------------------------------------------------------
# Live ACP session
# ---------------------------------------------------------------------------


class HostedSession:
    """One live ACP session: turn serialization, permissions, and ACP updates."""

    def __init__(
        self,
        host: "HarnessHostAgent",
        runtime_session: Any,
        *,
        cwd: str,
        model: str | None,
        effort: str | None = None,
    ) -> None:
        self.host = host
        self.runtime = runtime_session
        self.session_id = str(runtime_session.session_id)
        self.cwd = cwd
        self.model = model
        self.effort = _valid_effort(effort, self._effort_support())
        self.turn_lock = asyncio.Lock()
        self.active_turn: asyncio.Task[Any] | None = None
        self.cancel_requested = False
        self.started_tools: set[str] = set()
        self.finished_tools: set[str] = set()
        self._always_allow: set[tuple[str, str]] = set()
        self._always_reject: set[tuple[str, str]] = set()
        self._permission_counter = 0
        self._cancel_event = asyncio.Event()
        self._prompt_queued = False
        self._cancel_before_start = False
        self._closed = False

    # --- state ---------------------------------------------------------

    @property
    def snapshot(self) -> ConfigurationSnapshot:
        return self.host._snapshot_of(self.runtime)

    @property
    def closed(self) -> bool:
        return self._closed

    def _effort_support(self) -> str | None:
        if not self.model:
            return None
        return self.host.policy.thinking_models.get(self.model)

    async def persist_meta(self) -> None:
        await self.host.metadata.put(
            self.session_id,
            SessionMeta(cwd=self.cwd, model=self.model, effort=self.effort),
        )

    # --- turn execution ------------------------------------------------

    async def prompt(self, blocks: list[dict[str, Any]]) -> schema.PromptResponse:
        """Run one user prompt, serialized per session and cancellable."""
        self._prompt_queued = True
        try:
            async with self.turn_lock:
                if self._closed:
                    raise RequestError.invalid_params(
                        {"sessionId": self.session_id, "reason": "session is closed"}
                    )
                if self._cancel_before_start:
                    # ``session/cancel`` arrived while this prompt was waiting for the
                    # previous one; honour it instead of running a turn nobody awaits.
                    self._cancel_before_start = False
                    return schema.PromptResponse(stop_reason="cancelled")
                self.cancel_requested = False
                self._cancel_event = asyncio.Event()
                self.started_tools.clear()
                self.finished_tools.clear()
                turn = asyncio.ensure_future(self._run_prompt(blocks))
                self.active_turn = turn
                self._prompt_queued = False
                try:
                    return await asyncio.shield(turn)
                except _TurnCancelled:
                    return schema.PromptResponse(stop_reason="cancelled")
                except asyncio.CancelledError:
                    # Connection teardown, ``session/close`` or deletion cancels this task;
                    # a client ``session/cancel`` is delivered through the runtime token.
                    if self.cancel_requested:
                        with contextlib.suppress(BaseException):
                            await asyncio.shield(turn)
                        return schema.PromptResponse(stop_reason="cancelled")
                    turn.cancel()
                    with contextlib.suppress(BaseException):
                        await turn
                    raise
                finally:
                    self.active_turn = None
                    self.cancel_requested = False
        finally:
            self._prompt_queued = False

    async def _run_prompt(self, blocks: list[dict[str, Any]]) -> schema.PromptResponse:
        usage: schema.Usage | None = None
        stop_reason: schema.StopReason = "end_turn"
        prompt: Any = blocks
        deferred_results: DeferredToolResults | None = None
        pending_approvals: list[Mapping[str, Any]] = []
        text_since_tool = False
        try:
            while True:
                if self.cancel_requested:
                    if pending_approvals:
                        await self._settle_deferred_as_cancelled(pending_approvals)
                        pending_approvals = []
                    await self._fail_outstanding_tools()
                    return schema.PromptResponse(stop_reason="cancelled", usage=usage)
                approvals: list[Mapping[str, Any]] = []
                async for event in self._stream(prompt, deferred_results):
                    event_type, data = _event_pair(event)
                    if event_type == "text_delta":
                        text = data.get("text", data.get("delta", ""))
                        thought = bool(data.get("thought")) or data.get("kind") == "thinking"
                        if isinstance(text, str) and text and not thought:
                            text_since_tool = True
                    elif event_type == "tool_call":
                        text_since_tool = False
                    if event_type == "error":
                        details = dict(data.get("details") or {})
                        if details.get("cancelled"):
                            await self._fail_outstanding_tools()
                            return schema.PromptResponse(stop_reason="cancelled", usage=usage)
                        raise HarnessError(
                            str(data.get("code") or "TURN_FAILED"),
                            str(data.get("message") or "The agent turn failed."),
                            details,
                        )
                    if event_type == "deferred_tool_requests":
                        calls = data.get("calls") or []
                        if calls:
                            raise HarnessError(
                                "CAPABILITY_UNAVAILABLE",
                                "External tool execution is not supported by the ACP host.",
                                {"tools": [call.get("tool_name") for call in calls]},
                            )
                        approvals = list(data.get("approvals") or [])
                        continue
                    if event_type == "turn_complete":
                        stop_reason = acp_map.stop_reason_for(data.get("stop_reason") or "end_turn")
                        usage = _usage_total(usage, data.get("usage"))
                        # Some providers return the final answer only in the run
                        # result after tool calls instead of streaming it. Forward
                        # that text so the client does not lose the response.
                        output = data.get("output")
                        if isinstance(output, str) and output.strip() and not text_since_tool:
                            for update in acp_map.text_updates(output):
                                await self._send(update)
                        deferred = data.get("deferred_tool_requests") or {}
                        if deferred.get("approvals"):
                            approvals = list(deferred["approvals"])
                        if deferred.get("calls"):
                            raise HarnessError(
                                "CAPABILITY_UNAVAILABLE",
                                "External tool execution is not supported by the ACP host.",
                                {"tools": [call.get("tool_name") for call in deferred["calls"]]},
                            )
                        continue
                    await self._forward_event(event_type, data)
                if not approvals:
                    break
                pending_approvals = list(approvals)
                if self.cancel_requested:
                    await self._settle_deferred_as_cancelled(pending_approvals)
                    pending_approvals = []
                    await self._fail_outstanding_tools()
                    return schema.PromptResponse(stop_reason="cancelled", usage=usage)
                try:
                    resolved = await self._resolve_approvals(approvals)
                except (asyncio.CancelledError, _TurnCancelled):
                    raise
                except BaseException:
                    # The deferred segment is already committed; settle it with denials
                    # so the conversation history has no unprocessed calls, then surface
                    # the original failure (a dying connection makes this best-effort).
                    with contextlib.suppress(Exception):
                        await self._settle_deferred_as_cancelled(approvals)
                    pending_approvals = []
                    raise
                if resolved is None:
                    # The client dismissed an approval dialog (or cancelled meanwhile).
                    # The runtime already committed the deferred segment, so deny the
                    # pending calls and drain it: history then has no unprocessed tool
                    # calls and the next prompt is usable. Updates are suppressed.
                    await self._fail_outstanding_tools()
                    await self._settle_deferred_as_cancelled(approvals)
                    pending_approvals = []
                    return schema.PromptResponse(stop_reason="cancelled", usage=usage)
                if self.cancel_requested:
                    # The client answered a permission dialog and then cancelled before
                    # the resumed segment started. Settle the approved calls too.
                    await self._settle_deferred_as_cancelled(approvals)
                    pending_approvals = []
                    await self._fail_outstanding_tools()
                    return schema.PromptResponse(stop_reason="cancelled", usage=usage)
                deferred_results = resolved
                pending_approvals = []
                prompt = None  # resume with the approval results; no new user message
        except (asyncio.CancelledError, _TurnCancelled):
            if pending_approvals:
                with contextlib.suppress(BaseException):
                    await self._settle_deferred_as_cancelled(pending_approvals)
            await self._fail_outstanding_tools()
            raise
        except (HarnessError, RequestError):
            if pending_approvals:
                with contextlib.suppress(BaseException):
                    await self._settle_deferred_as_cancelled(pending_approvals)
            await self._fail_outstanding_tools()
            raise
        except Exception as exc:
            await self._fail_outstanding_tools()
            raise HarnessError(
                "TURN_FAILED",
                f"The agent turn failed: {exc}",
                {"type": type(exc).__name__},
            ) from exc
        return schema.PromptResponse(stop_reason=stop_reason, usage=usage)

    def _stream(
        self, prompt: Any, deferred_results: DeferredToolResults | None
    ) -> AsyncIterator[Any]:
        runtime = self.runtime
        stream = getattr(runtime, "stream", None)
        if stream is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                "The runtime session does not expose stream(); see docs/ACP_RUNTIME_CONTRACT.md.",
            )
        kwargs: dict[str, Any] = {}
        if deferred_results is not None:
            kwargs["deferred_tool_results"] = deferred_results
        if self.host.policy.models and self.model:
            kwargs["model"] = self.model
        support = self._effort_support()
        if (
            self.host.policy.model_settings
            or self.host.policy.thinking_models
            or self.effort is not None
        ):
            model_settings = dict(self.host.policy.model_settings)
            if self.effort is not None and support is not None:
                off = self.effort == acp_map.EFFORT_OFF
                model_settings["thinking"] = False if off else self.effort
                provider = (self.model or "").partition(":")[0]
                if support == "custom" and provider in _OPENAI_EFFORT_PROVIDERS:
                    # Profiles absent from the registry would silently ignore
                    # unified thinking; the explicit key still reaches the wire.
                    model_settings["openai_reasoning_effort"] = "none" if off else self.effort
            else:
                # Session state is authoritative: "Default" must also clear a level
                # that the configuration supplied when the process started.
                model_settings.pop("thinking", None)
            kwargs["model_settings"] = model_settings
        return stream(prompt, **kwargs)

    async def _forward_event(self, event_type: str, data: Mapping[str, Any]) -> None:
        if event_type == "text_delta":
            text = data.get("text", data.get("delta", ""))
            if isinstance(text, str) and text:
                thought = bool(data.get("thought")) or data.get("kind") == "thinking"
                for update in acp_map.text_updates(text, thought=thought):
                    await self._send(update)
            return
        if event_type == "tool_call":
            update = acp_map.event_to_update(
                event_type, data, cwd=self.cwd, presenter=self.host.presenter
            )
            if update is not None:
                if update.tool_call_id in self.started_tools:
                    return  # a resumed approval segment re-emits the call event
                self.started_tools.add(update.tool_call_id)
                await self._send(update)
            return
        if event_type == "tool_update":
            update = acp_map.event_to_update(
                event_type, data, cwd=self.cwd, presenter=self.host.presenter
            )
            if update is not None:
                if getattr(update, "status", None) in ("completed", "failed", "cancelled"):
                    self.finished_tools.add(update.tool_call_id)
                await self._send(update)
            return
        if event_type in ("usage", "session_info", "user_message"):
            if event_type == "user_message":
                return  # replay-only: the client already renders its own prompt
            update = acp_map.event_to_update(
                event_type, data, cwd=self.cwd, presenter=self.host.presenter
            )
            if update is not None:
                await self._send(update)
            return
        if event_type == "config_changed":
            await self.host._send_configuration_changed(self)
            return
        # custom/capability/deferred/error/turn events without standard rendering are
        # represented by the runtime's own transcript and extension surfaces.
        update = acp_map.event_to_update(
            event_type, data, cwd=self.cwd, presenter=self.host.presenter
        )
        if update is not None:
            await self._send(update)

    # --- approvals -------------------------------------------------------

    def _permission_scope(self, tool_name: str, args: Any) -> tuple[str, str]:
        try:
            canonical = json.dumps(_jsonable(args), sort_keys=True, separators=(",", ":"))
        except Exception:  # pragma: no cover - defensive
            canonical = repr(args)
        return (tool_name, canonical)

    async def _resolve_approvals(
        self, approvals: Sequence[Mapping[str, Any]]
    ) -> DeferredToolResults | None:
        """Ask the client about each paused tool call; ``None`` means the dialog was cancelled."""
        conn = self.host.require_connection()
        results: dict[str, Any] = {}
        for approval in approvals:
            tool_call_id = str(approval.get("tool_call_id") or "")
            tool_name = str(approval.get("tool_name") or "tool")
            args = approval.get("args") or {}
            if not tool_call_id:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    "The runtime supplied an approval without a tool_call_id.",
                )
            scope = self._permission_scope(tool_name, args)
            if scope in self._always_allow:
                results[tool_call_id] = True
                await self._send(
                    acp.update_tool_call(tool_call_id=tool_call_id, status="in_progress")
                )
                continue
            if scope in self._always_reject:
                results[tool_call_id] = ToolDenied("Rejected by the client.")
                continue
            kind = acp_map.tool_call_start_update(
                {"tool_call_id": tool_call_id, "tool_name": tool_name, "args": args},
                cwd=self.cwd,
                presenter=self.host.presenter,
            ).kind
            if tool_call_id not in self.started_tools:
                await self._send(
                    acp.start_tool_call(
                        tool_call_id=tool_call_id,
                        title=acp_map.humanize_tool_name(tool_name),
                        kind=kind,
                        status="pending",
                        raw_input=_jsonable(args),
                    )
                )
                self.started_tools.add(tool_call_id)
            else:
                # The call was announced as running before the model paused for approval;
                # correct it to pending so a rejected call is never shown as executed.
                await self._send(
                    acp.update_tool_call(tool_call_id=tool_call_id, status="pending", kind=kind)
                )
            self._permission_counter += 1
            options = acp_map.permission_options(
                {
                    "options": [
                        {"option_id": "allow-once", "name": "Allow once", "kind": "allow_once"},
                        {
                            "option_id": "allow-always",
                            "name": "Always allow",
                            "kind": "allow_always",
                        },
                        {"option_id": "reject-once", "name": "Reject", "kind": "reject_once"},
                        {
                            "option_id": "reject-always",
                            "name": "Always reject",
                            "kind": "reject_always",
                        },
                    ]
                }
            )
            permission_task = asyncio.ensure_future(
                conn.request_permission(
                    session_id=self.session_id,
                    tool_call=schema.ToolCallUpdate(
                        tool_call_id=tool_call_id,
                        title=acp_map.humanize_tool_name(tool_name),
                        kind=kind,
                        status="pending",
                        raw_input=_jsonable(args),
                    ),
                    options=options,
                )
            )
            cancel_task = asyncio.ensure_future(self._cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {permission_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                cancel_task.cancel()
                with contextlib.suppress(BaseException):
                    await cancel_task
            if cancel_task in done and permission_task not in done:
                # ACP has no cancellation for an outstanding permission request; let the
                # client's eventual answer resolve harmlessly, and end this prompt now.
                permission_task.add_done_callback(_consume_task_result)
                return None
            if permission_task.cancelled():
                return None
            response = permission_task.result()
            outcome = response.outcome
            if getattr(outcome, "outcome", None) != "selected":
                return None  # the user dismissed the dialog
            chosen = next(
                (option for option in options if option.option_id == outcome.option_id),
                None,
            )
            if chosen is None:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"The client selected an unknown permission option {outcome.option_id!r}.",
                    {"toolCallId": tool_call_id},
                )
            allowed = chosen.kind in ("allow_once", "allow_always")
            if chosen.kind == "allow_always":
                self._always_allow.add(scope)
            elif chosen.kind == "reject_always":
                self._always_reject.add(scope)
            results[tool_call_id] = True if allowed else ToolDenied("Rejected by the client.")
            if allowed:
                await self._send(
                    acp.update_tool_call(tool_call_id=tool_call_id, status="in_progress")
                )
        return DeferredToolResults(approvals=results)

    async def _settle_deferred_as_cancelled(self, approvals: Sequence[Mapping[str, Any]]) -> None:
        """Resume a cancelled approval pause with denials until history is clean.

        A model may request another approval after observing a denial; each round is
        denied too so a cancelled prompt cannot leave unprocessed tool calls behind.
        Updates are deliberately suppressed: the prompt was already reported cancelled.
        """
        remaining = list(approvals)
        for _ in range(8):
            denied_results = DeferredToolResults(
                approvals={
                    str(approval.get("tool_call_id")): ToolDenied("Cancelled by the client.")
                    for approval in remaining
                    if approval.get("tool_call_id")
                }
            )
            following: list[Mapping[str, Any]] = []
            try:
                async for event in self._stream(None, denied_results):
                    event_type, data = _event_pair(event)
                    if event_type == "error":
                        _LOG.debug(
                            "deferred segment ended with error while settling cancellation: %s",
                            data,
                        )
                        return
                    if event_type == "turn_complete":
                        deferred = data.get("deferred_tool_requests") or {}
                        following = list(deferred.get("approvals") or [])
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("failed to settle deferred approvals after cancellation")
                return
            if not following:
                return
            remaining = following
        _LOG.warning(
            "cancelled prompt for %s kept requesting approvals after 8 denial rounds; "
            "the session may need to be reloaded",
            self.session_id,
        )

    async def cancel(self) -> None:
        self.cancel_requested = True
        self._cancel_event.set()
        if self.active_turn is None and self._prompt_queued:
            # The prompt handler has been dispatched but has not started its turn yet
            # (it is waiting on the session lock). Record the intent for that prompt.
            self._cancel_before_start = True
        cancel = getattr(self.runtime, "cancel", None)
        if cancel is not None:
            with contextlib.suppress(Exception):
                await _maybe_await(cancel())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.active_turn is not None and not self.active_turn.done():
            self.cancel_requested = True
            with contextlib.suppress(Exception):
                await _maybe_await(self.runtime.cancel())
            self.active_turn.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.shield(self.active_turn)
        try:
            await self.runtime.close()
        except Exception:
            _LOG.exception("runtime session %s failed to close cleanly", self.session_id)

    # --- plumbing --------------------------------------------------------

    async def _fail_outstanding_tools(self) -> None:
        for tool_call_id in list(self.started_tools - self.finished_tools):
            self.finished_tools.add(tool_call_id)
            with contextlib.suppress(BaseException):
                await self._send(acp.update_tool_call(tool_call_id=tool_call_id, status="failed"))

    async def _send(self, update: acp_map.SessionUpdate) -> None:
        await self.host.send_update(self.session_id, update)


# ---------------------------------------------------------------------------
# ACP agent
# ---------------------------------------------------------------------------


class HarnessHostAgent:
    """The ACP ``Agent`` implementation served by ``harness-host``."""

    def __init__(
        self,
        bundle: HostBundle,
        policy: HostPolicy | None = None,
        *,
        tool_presenter: ToolCallPresenter | None = None,
    ) -> None:
        self.bundle = bundle
        self.definition = bundle.definition
        self.policy = policy or HostPolicy()
        self.presenter = tool_presenter or acp_map.default_presenter()
        self.manager = bundle.manager
        self.metadata = bundle.meta or HostMetadataStore(bundle.store_path)
        self._sessions: dict[str, HostedSession] = {}
        self._conn: Any | None = None
        self._client_capabilities: schema.ClientCapabilities | None = None
        self._client_name = ""
        self._boolean_config_options = False
        self._client_ext: set[str] = set()

    # --- SDK plumbing ----------------------------------------------------

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def shutdown(self) -> None:
        """Close live sessions and the manager-owned store on connection teardown.

        Releasing control matters: the runtime refuses to hand a session to another
        worker while a stale ownership claim remains, so a clean EOF must drain.
        """
        for state in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                await state.close()
        self._sessions.clear()
        with contextlib.suppress(Exception):
            await self.manager.close()

    def require_connection(self) -> Any:
        if self._conn is None:  # pragma: no cover - SDK calls on_connect first
            raise HarnessError("CAPABILITY_UNAVAILABLE", "ACP connection is not established yet.")
        return self._conn

    async def send_update(self, session_id: str, update: acp_map.SessionUpdate) -> None:
        await self.require_connection().session_update(session_id=session_id, update=update)

    # --- initialization ---------------------------------------------------

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        capabilities = client_capabilities or schema.ClientCapabilities()
        self._client_capabilities = capabilities
        self._client_name = client_info.name if client_info is not None else ""
        session_caps = getattr(capabilities, "session", None)
        config_caps = getattr(session_caps, "config_options", None)
        self._boolean_config_options = getattr(config_caps, "boolean", None) is not None
        try:
            methods, version = acp_map.parse_client_extensions(kwargs.get("harness"))
        except Exception:  # malformed client metadata must not break initialization
            methods, version = set(), None
        self._client_ext = methods if version == 1 else set()
        negotiated = (
            protocol_version
            if 1 <= protocol_version <= acp.PROTOCOL_VERSION
            else acp.PROTOCOL_VERSION
        )
        advertisement = acp_map.extension_advertisement(
            local_binding_creation=self.policy.allow_local_binding_creation
        )
        return schema.InitializeResponse(
            protocol_version=negotiated,
            agent_capabilities=schema.AgentCapabilities(
                load_session=True,
                # Embedded resources are converted to native content by the runtime.
                # Images/audio remain opt-in because provider model support varies.
                prompt_capabilities=schema.PromptCapabilities(embedded_context=True),
                mcp_capabilities=schema.McpCapabilities(),
                session_capabilities=schema.SessionCapabilities(
                    list=schema.SessionListCapabilities(),
                    delete=schema.SessionDeleteCapabilities(),
                    close=schema.SessionCloseCapabilities(),
                ),
            ),
            auth_methods=[],
            agent_info=schema.Implementation(
                name=self.definition.name or self.definition.agent_id,
                title=self.definition.name or None,
                version=self.definition.version,
            ),
            field_meta={"harness": advertisement.to_wire()},
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        raise RequestError.auth_required(
            {"reason": "this host advertises no authentication methods", "methodId": method_id}
        )

    # --- session lifecycle ------------------------------------------------

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
            initial = acp_map.initial_configuration_changes(kwargs.get("harness"))
            if self.policy.hosted:
                added = [change for change in initial if isinstance(change, AddChange)]
                if added:
                    raise HarnessError(
                        "POLICY_DENIED",
                        "Local binding creation is prohibited for hosted sessions.",
                        {"bindingIds": [change.binding_id for change in added]},
                    )
            requested_cwd = acp_map.validate_requested_cwd(
                cwd, allowed=self.policy.allowed_cwds, hosted=self.policy.hosted
            )
            resources = self._session_resources(requested_cwd)
            runtime_session = await self.manager.open_session(
                self.definition,
                resources=resources,
                principal=self.policy.principal,
                deployment_id=self.policy.deployment_id,
                initial_configuration=list(initial) or None,
            )
            state = HostedSession(
                self,
                runtime_session,
                cwd=requested_cwd,
                model=self.policy.default_model,
                effort=self.policy.default_effort,
            )
            self._sessions[state.session_id] = state
            await state.persist_meta()
            return schema.NewSessionResponse(
                session_id=state.session_id,
                config_options=await self._standard_options(state),
            )
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except RequestError:
            raise
        except ValidationError as exc:
            raise RequestError.invalid_params({"errors": exc.errors()[:10]}) from exc

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
            summary = await self.manager.store.get_session(session_id)
            if (
                summary is None
                or summary.principal != self.policy.principal
                or summary.deployment_id != self.policy.deployment_id
            ):
                raise HarnessError(
                    "POLICY_DENIED",
                    f"Session {session_id!r} is not available to this deployment.",
                    {"sessionId": session_id, "reason": "ownership"},
                )
            meta = await self.metadata.get(session_id)
            recorded_cwd = getattr(summary, "cwd", None) or (meta.cwd if meta else None)
            if recorded_cwd:
                stored_cwd = recorded_cwd
            elif self.policy.hosted:
                raise HarnessError(
                    "WORKSPACE_UNAVAILABLE",
                    "The stored session has no recorded working directory for this deployment.",
                    {"sessionId": session_id},
                )
            else:
                stored_cwd = self.policy.fallback_cwd()
            # Reopening preserves the recorded cwd; the client's requested cwd is ignored.
            stored_cwd = acp_map.validate_requested_cwd(
                stored_cwd, allowed=self.policy.allowed_cwds, hosted=self.policy.hosted
            )
            prior = self._sessions.pop(session_id, None)
            if prior is not None:
                await prior.close()
            runtime_session = await self.manager.open_saved_session(
                self.policy.principal,
                session_id,
                resources=self._session_resources(stored_cwd),
                deployment_id=self.policy.deployment_id,
            )
            restored_model = meta.model if meta and meta.model else None
            if self.policy.models and restored_model not in self.policy.models:
                # A deployment may retire a model between sessions. Never advertise
                # one value while passing a different, unavailable id to the runtime.
                restored_model = self.policy.default_model
            state = HostedSession(
                self,
                runtime_session,
                cwd=stored_cwd,
                model=restored_model or self.policy.default_model,
                effort=(meta.effort if meta and meta.effort else self.policy.default_effort),
            )
            self._sessions[session_id] = state
            await state.persist_meta()
            await self._replay_transcript(state)
            return schema.LoadSessionResponse(config_options=await self._standard_options(state))
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except RequestError:
            raise
        except ValidationError as exc:
            raise RequestError.invalid_params({"errors": exc.errors()[:10]}) from exc

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> schema.ListSessionsResponse:
        try:
            cwd_filter: str | None = None
            if cwd is not None:
                cwd_filter = acp_map.validate_requested_cwd(
                    cwd, allowed=self.policy.allowed_cwds, hosted=self.policy.hosted
                )
            summaries = await self.manager.list_sessions(self.policy.principal, limit=None)
            entries: list[schema.SessionInfo] = []
            for summary in summaries:
                if summary.deployment_id != self.policy.deployment_id:
                    continue
                meta = await self.metadata.get(summary.session_id)
                session_cwd = getattr(summary, "cwd", None) or (meta.cwd if meta else None)
                if session_cwd is None:
                    if self.policy.hosted:
                        continue  # fail closed: do not expose a host path for a hosted session
                    session_cwd = self.policy.fallback_cwd()
                if cwd_filter is not None and acp_map.canonical_root(session_cwd) != cwd_filter:
                    continue
                entries.append(
                    schema.SessionInfo(
                        session_id=summary.session_id,
                        cwd=session_cwd,
                        title=summary.title,
                        updated_at=_iso_millis(summary.last_active_at),
                    )
                )
            page, next_cursor = paginate(entries, cursor=cursor, limit=50)
            return schema.ListSessionsResponse(sessions=page, next_cursor=next_cursor)
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except RequestError:
            raise

    async def delete_session(self, session_id: str, **kwargs: Any) -> DeleteSessionResponse | None:
        try:
            summary = await self.manager.store.get_session(session_id)
            if (
                summary is None
                or summary.principal != self.policy.principal
                or summary.deployment_id != self.policy.deployment_id
            ):
                raise HarnessError(
                    "POLICY_DENIED",
                    f"Session {session_id!r} is not available to this deployment.",
                    {"sessionId": session_id, "reason": "ownership"},
                )
            state = self._sessions.pop(session_id, None)
            if state is not None:
                await state.close()
            await self.manager.delete_session(self.policy.principal, session_id)
            await self.metadata.delete(session_id)
            return DeleteSessionResponse()
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except RequestError:
            raise

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse | None:
        state = self._sessions.pop(session_id, None)
        if state is not None:
            await state.close()
        return schema.CloseSessionResponse()

    # --- prompts and cancellation -----------------------------------------

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        state = self._sessions.get(session_id)
        if state is None:
            raise RequestError.invalid_params(
                {"sessionId": session_id, "reason": "no live session; load it first"}
            )
        blocks = acp_map.prompt_blocks_to_runtime(prompt)
        try:
            return await state.prompt(blocks)
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except RequestError:
            raise
        except Exception as exc:
            raise acp_map.harness_rpc_error(
                HarnessError("TURN_FAILED", f"The agent turn failed: {exc}", {})
            ) from exc

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            await state.cancel()

    # --- standard configuration options -----------------------------------

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse:
        state = self._sessions.get(session_id)
        if state is None:
            raise RequestError.invalid_params({"sessionId": session_id})
        try:
            if config_id == acp_map.MODEL_OPTION_ID:
                await self._set_model(state, value)
            elif config_id == acp_map.EFFORT_OPTION_ID:
                await self._set_effort(state, value)
            elif config_id.startswith(acp_map.BINDING_OPTION_PREFIX):
                binding_id = config_id[len(acp_map.BINDING_OPTION_PREFIX) :]
                if isinstance(value, bool):
                    enabled = value
                elif value in ("on", "off"):
                    enabled = value == "on"
                else:
                    raise HarnessError(
                        "INVALID_CONFIGURATION",
                        f"Binding option {config_id!r} expects 'on' or 'off'.",
                        {"configId": config_id, "value": value},
                    )
                await self._update_configuration(
                    state,
                    [EnableChange(binding_id=binding_id, enabled=enabled)],
                    expected_revision=None,
                    request_id=uuid.uuid4().hex,
                )
            else:
                raise HarnessError(
                    "INVALID_CONFIGURATION",
                    f"Unknown configuration option {config_id!r}.",
                    {"configId": config_id},
                )
            await self._send_configuration_changed(state)
            return schema.SetSessionConfigOptionResponse(
                config_options=await self._standard_options(state)
            )
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except RequestError:
            raise

    # --- project extension -------------------------------------------------

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if method == "harness/capabilities/list":
                return await self._ext_capabilities_list(params or {})
            if method == "harness/plugins/list":
                return await self._ext_plugins_list(params or {})
            if method == "harness/configuration/get":
                return await self._ext_configuration_get(params or {})
            if method == "harness/configuration/update":
                return await self._ext_configuration_update(params or {})
            raise RequestError.method_not_found(f"_{method}")
        except HarnessError as exc:
            raise acp_map.harness_rpc_error(exc) from exc
        except RequestError:
            raise
        except ValidationError as exc:
            # Extension DTOs are project-owned application contracts: use the
            # documented -32000 INVALID_CONFIGURATION shape.
            error = HarnessError(
                "INVALID_CONFIGURATION",
                "The extension request payload is invalid.",
                {"errors": exc.errors()[:10]},
            )
            raise acp_map.harness_rpc_error(error) from exc

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        _LOG.debug("ignoring extension notification %r", method)

    async def _ext_capabilities_list(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request = ListRequest.model_validate(dict(params))
        if not request.session_id:
            raise HarnessError(
                "INVALID_CONFIGURATION", "sessionId is required for a capability listing."
            )
        state = self._sessions.get(request.session_id)
        if state is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                "The session is not open in this host; load it before listing capabilities.",
                {"sessionId": request.session_id},
            )
        descriptors, next_cursor = await state.runtime.capabilities(request.cursor, request.limit)
        snapshot = state.snapshot
        response = BindingListResponse(
            items=[
                descriptor
                if isinstance(descriptor, BindingDescriptor)
                else BindingDescriptor.model_validate(descriptor)
                for descriptor in descriptors
            ],
            next_cursor=next_cursor,
            selected_revision=snapshot.selected_revision,
            active_revision=snapshot.active_revision,
            policy_revision=snapshot.policy_revision,
        )
        return response.to_wire()

    async def _ext_plugins_list(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.policy.allow_local_binding_creation:
            raise RequestError.method_not_found("_harness/plugins/list")
        request = ListRequest.model_validate(dict(params))
        page, next_cursor = paginate(
            self.bundle.registry.catalog(), cursor=request.cursor, limit=request.limit
        )
        return PluginListResponse(items=page, next_cursor=next_cursor).to_wire()

    async def _ext_configuration_get(self, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId", params.get("session_id"))
        if not isinstance(session_id, str) or not session_id:
            raise HarnessError("INVALID_CONFIGURATION", "sessionId is required.")
        state = self._sessions.get(session_id)
        if state is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                "The session is not open in this host; load it before reading configuration.",
                {"sessionId": session_id},
            )
        return self._snapshot_payload(state, state.snapshot)

    async def _ext_configuration_update(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request = ConfigurationUpdate.model_validate(dict(params))
        state = self._sessions.get(request.session_id)
        if state is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                "The session is not open in this host; load it before updating configuration.",
                {"sessionId": request.session_id},
            )
        if self.policy.hosted:
            added = [change for change in request.changes if isinstance(change, AddChange)]
            if added:
                raise HarnessError(
                    "POLICY_DENIED",
                    "Local binding creation is prohibited for hosted sessions.",
                    {"bindingIds": [change.binding_id for change in added]},
                )
        snapshot = await self._update_configuration(
            state,
            request.changes,
            expected_revision=request.expected_revision,
            request_id=request.request_id,
        )
        await self._send_configuration_changed(state)
        return self._snapshot_payload(state, snapshot)

    # --- runtime integration helpers ---------------------------------------

    def _session_resources(self, cwd: str) -> dict[str, Any]:
        resources: dict[str, Any] = dict(self.policy.resources)
        if self.policy.default_model is not None:
            resources.setdefault("model.default", self.policy.default_model)
        # The ACP session's cwd is the local workspace binding unless the deployment
        # explicitly bound a richer workspace (sandbox backend/ref) under that name.
        for requirement in self.definition.resources:
            if requirement.kind == "workspace" and requirement.name not in resources:
                resources[requirement.name] = cwd
        return resources

    def _snapshot_of(self, runtime_session: Any) -> ConfigurationSnapshot:
        snapshot = getattr(runtime_session, "snapshot", None)
        snapshot = snapshot() if callable(snapshot) else snapshot
        if isinstance(snapshot, ConfigurationSnapshot):
            return snapshot
        if isinstance(snapshot, Mapping):
            return ConfigurationSnapshot.model_validate(dict(snapshot))
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            "The runtime session did not expose a ConfigurationSnapshot; see "
            "docs/ACP_RUNTIME_CONTRACT.md.",
        )

    async def _all_descriptors(self, state: HostedSession) -> list[BindingDescriptor]:
        catalog = getattr(state.runtime, "catalog", None)
        if catalog is not None and hasattr(catalog, "descriptors"):
            try:
                descriptors = catalog.descriptors()
            except TypeError:
                descriptors = None
            if descriptors is not None:
                return [
                    descriptor
                    if isinstance(descriptor, BindingDescriptor)
                    else BindingDescriptor.model_validate(descriptor)
                    for descriptor in descriptors
                ]
        collected: list[BindingDescriptor] = []
        cursor: str | None = None
        while True:
            page, cursor = await state.runtime.capabilities(cursor, 100)
            collected.extend(
                descriptor
                if isinstance(descriptor, BindingDescriptor)
                else BindingDescriptor.model_validate(descriptor)
                for descriptor in page
            )
            if cursor is None:
                return collected

    async def _update_configuration(
        self,
        state: HostedSession,
        changes: Sequence[BindingChange],
        *,
        expected_revision: int | None,
        request_id: str | None,
    ) -> ConfigurationSnapshot:
        await self._reject_new_session_changes(state, changes)
        # A prompt turn (including every approval-resume segment) captures one immutable
        # composition. Serializing configuration transactions behind the same turn lock
        # keeps a change made while an approval dialog is open from silently altering the
        # continuation of that logical turn; the change commits when the prompt is done.
        async with state.turn_lock:
            return await state.runtime.update_configuration(
                list(changes),
                request_id=request_id or uuid.uuid4().hex,
                expected_revision=expected_revision,
            )

    async def _reject_new_session_changes(
        self, state: HostedSession, changes: Sequence[BindingChange]
    ) -> None:
        """``new_session`` bindings are inspectable but not editable after creation."""
        if not changes:
            return
        descriptors = {
            descriptor.binding_id: descriptor for descriptor in await self._all_descriptors(state)
        }
        for change in changes:
            if isinstance(change, AddChange):
                plugin = self.bundle.registry.lookup(change.plugin_id)
                if plugin is None or plugin.change_mode != "new_session":
                    continue
            else:
                descriptor = descriptors.get(change.binding_id)
                if descriptor is None or descriptor.change_mode != "new_session":
                    continue
                if isinstance(change, EnableChange) and descriptor.enabled == change.enabled:
                    continue  # an idempotent state selection is not an edit
            raise HarnessError(
                "NEW_SESSION_REQUIRED",
                f"Binding {change.binding_id!r} can only change when a new session is created.",
                {"bindingId": change.binding_id},
            )

    async def _set_model(self, state: HostedSession, value: str | bool) -> None:
        if not isinstance(value, str):
            raise HarnessError("INVALID_CONFIGURATION", "The model option expects a string value.")
        if not self.policy.models:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE", "This deployment does not offer model selection."
            )
        if value not in self.policy.models:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Model {value!r} is not offered by this deployment.",
                {"model": value, "options": list(self.policy.models)},
            )
        async with state.turn_lock:
            if value != state.model:
                # Effort is paired with the model; a switch starts from the
                # configured default when the new model supports it.
                state.model = value
                state.effort = _valid_effort(self.policy.default_effort, state._effort_support())
            await state.persist_meta()

    async def _set_effort(self, state: HostedSession, value: str | bool) -> None:
        if not isinstance(value, str):
            raise HarnessError("INVALID_CONFIGURATION", "The effort option expects a string value.")
        support = state._effort_support()
        option = acp_map.effort_option(None, support=support)
        if option is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                "The selected model does not offer reasoning effort.",
                {"model": state.model},
            )
        allowed = {
            item.value
            for item in option.options
            if isinstance(item, schema.SessionConfigSelectOption)
        }
        if value not in allowed:
            raise HarnessError(
                "INVALID_CONFIGURATION",
                f"Reasoning effort {value!r} is not available for this model.",
                {"model": state.model, "options": sorted(allowed)},
            )
        async with state.turn_lock:
            state.effort = None if value == acp_map.EFFORT_DEFAULT else value
            await state.persist_meta()

    async def _standard_options(
        self, state: HostedSession
    ) -> list[schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean]:
        descriptors = await self._all_descriptors(state)
        return acp_map.config_options(
            state.snapshot,
            descriptors,
            models=self.policy.models,
            current_model=state.model,
            current_effort=state.effort or acp_map.EFFORT_DEFAULT,
            effort_support=state._effort_support(),
            boolean_toggles=self._boolean_config_options,
        )

    def _snapshot_payload(
        self, state: HostedSession, snapshot: ConfigurationSnapshot
    ) -> dict[str, Any]:
        """The complete configuration snapshot, exactly as the protocol DTO defines it.

        Extra host-specific keys are intentionally absent: the native client validates
        this payload with ``ConfigurationSnapshot`` (``extra="forbid"``). Model selection
        is visible in the standard ``configOptions`` list instead.
        """
        del state
        return snapshot.to_wire()

    async def _send_configuration_changed(self, state: HostedSession) -> None:
        if not acp_map.changed_method_supported(self._client_ext):
            return
        await self.require_connection().ext_notification(
            "harness/configuration/changed", self._snapshot_payload(state, state.snapshot)
        )

    async def _replay_transcript(self, state: HostedSession) -> None:
        try:
            rows = await state.runtime.transcript()
        except Exception:
            _LOG.exception("failed to read the transcript for %s", state.session_id)
            return
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            kind = str(row.get("kind") or "")
            data = row.get("data") or {}
            if not isinstance(data, Mapping):
                continue
            if kind == "thinking_delta":
                text = data.get("text", "")
                if isinstance(text, str) and text:
                    for update in acp_map.text_updates(text, thought=True):
                        await state._send(update)
                continue
            if kind == "user_message":
                text = _transcript_prompt_text(data.get("prompt"))
                for chunk in acp_map.chunk_text(text):
                    if chunk:
                        await state._send(acp_map.user_message_update(chunk))
                continue
            if kind == "turn_interrupted":
                # A standard client must be able to distinguish an interrupted turn
                # from a completed one on authoritative replay.
                message = str(data.get("message") or "The previous turn was interrupted.")
                text = f"⚠ Interrupted turn: {message}"
                for update in acp_map.text_updates(text):
                    await state._send(update)
                continue
            if kind == "text_delta":
                text = data.get("text", "")
                if isinstance(text, str) and text:
                    for update in acp_map.text_updates(text):
                        await state._send(update)
                continue
            if kind in ("tool_call", "tool_update"):
                update = acp_map.event_to_update(
                    kind, data, cwd=state.cwd, presenter=self.presenter
                )
                if update is not None:
                    await state._send(update)
                continue
            # deferred requests, errors, custom capability events, and interrupted-turn
            # markers have no standard ACP transcript representation.
            _LOG.debug("skipping transcript row %s for %s", kind, state.session_id)

    # --- small validations --------------------------------------------------

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


def _iso_millis(value: int | None) -> str | None:
    if not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def build_router(agent: Any, *, use_unstable_protocol: bool = True) -> Any:
    """Build the SDK agent router plus the standard session-delete route.

    The installed ACP SDK defines ``session/delete`` in ``AGENT_METHODS`` and provides
    ``DeleteSessionRequest`` but does not register a route for it; the host adds the
    route explicitly so an advertised deletion capability actually works.
    """
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
    """The client-facing half of the SDK's agent connection, over our router.

    ``AgentSideConnection`` normally provides these outbound methods, but it builds its
    own router internally; the host needs the SDK router plus the standard
    ``session/delete`` route, so the outbound surface is provided here (same payloads:
    ``session/update`` notification, ``session/request_permission`` request, extension
    notifications).
    """

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
    agent: HarnessHostAgent,
    *,
    input_stream: Any = None,
    output_stream: Any = None,
    use_unstable_protocol: bool = True,
) -> None:
    """Serve an agent over stdio (newline-delimited ACP JSON); diagnostics on stderr."""
    if input_stream is None and output_stream is None:
        reader, writer = await stdio_streams(limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES)
    else:  # test injection: (writer, reader), matching the SDK connection convention
        writer, reader = input_stream, output_stream
    router = build_router(agent, use_unstable_protocol=use_unstable_protocol)
    connection = Connection(router, writer, reader, listening=False)
    agent.on_connect(_AgentClientChannel(connection))
    try:
        await connection.main_loop()
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(connection.close())


def build_agent(bundle: HostBundle, setup: HostSetup | None = None) -> HarnessHostAgent:
    """Compose the ACP agent (and host policy) from a runtime bundle.

    Model selection is advertised only when the deployment explicitly offers models
    (``--model``/``--models``): a definition's internal ``model`` value is a runtime
    default, not necessarily a client-switchable model id.
    """
    models = setup.advertised_models() if setup else ()
    hosted = bool(setup.hosted) if setup else False
    resources = dict(setup.resources) if setup else {}
    if setup and setup.model and "model.default" not in resources:
        resources["model.default"] = setup.model
    policy = HostPolicy(
        principal=setup.principal if setup else "local",
        deployment_id=setup.deployment_id if setup else "local",
        hosted=hosted,
        allowed_cwds=setup.allowed_cwds if setup else None,
        allow_local_binding_creation=(setup.advertise_local_binding_creation if setup else True),
        allow_client_mcp=not hosted,
        models=models,
        default_model=(setup.default_model() if setup else None) or (models[0] if models else None),
        resources=resources,
    )
    return HarnessHostAgent(bundle, policy)


# ---------------------------------------------------------------------------
# Definition loading and the local bundle
# ---------------------------------------------------------------------------


def definition_from_manifest(manifest: DefinitionManifest) -> AgentDefinition:
    """Build a runtime definition from a parsed manifest envelope."""
    return AgentDefinition(
        agent_id=manifest.agent_id,
        name=manifest.name,
        description=manifest.description,
        definition_version=manifest.definition_version,
        spec=manifest.spec,
        bindings=[
            DefinitionBinding(
                binding_id=binding.binding_id,
                plugin_id=binding.plugin_id,
                config=dict(binding.config),
                enabled=binding.enabled,
                policy=binding.policy,
            )
            for binding in manifest.bindings
        ],
        resources=[
            ResourceRequirement(
                name=requirement.name,
                kind=requirement.kind,
                description=requirement.description,
                required=requirement.required,
            )
            for requirement in manifest.resources
        ],
        model=manifest.model,
        interaction_features=list(manifest.interaction_features),
    )


_REGISTRY_HOOK_NAMES = ("build_registry", "registry")


async def _await_result(awaitable: Awaitable[Any]) -> Any:
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
    """A definition module may expose its deployment's plugin registry.

    Convention: ``build_registry()`` (or ``registry``) returns or holds a
    :class:`PluginRegistry` with the plugins that back this deployment's
    bindings. Installed ``harness.capabilities`` entry points are discovered on top.
    """
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
    """Load a definition plus the deployment registry hook, if the source provides one."""
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
    """Select SQLite or PostgreSQL from a path/DSN without an implicit fallback.

    A PostgreSQL URI selects :class:`~harness.runtime.postgres.PostgresSessionStore`;
    every other value remains a SQLite path handled by the default store. Selecting
    PostgreSQL requires the optional ``postgres`` extra and raises its actionable
    import error otherwise.
    """
    value = str(configured) if configured is not None else None
    from .postgres import PostgresSessionStore, is_postgres_dsn

    if value and is_postgres_dsn(value):
        return PostgresSessionStore(value)
    return SessionStore(value) if value else SessionStore()


def build_local_bundle(setup: HostSetup) -> HostBundle:
    """The built-in composition: installed plugins + durable store + one definition.

    ``--runtime-module`` may substitute a deployment module whose
    ``build_host_bundle(setup)`` returns the same :class:`HostBundle` shape.
    """
    definition, hook_registry = _load_definition_source(setup.definition)
    registry = hook_registry or PluginRegistry()
    registry.allow_local_binding_creation = setup.advertise_local_binding_creation
    registry.discover()  # installed "harness.capabilities" entry points, lazily
    store = build_store(setup.store_path)
    manager = SessionManager(
        registry,
        store=store,
        definitions=[definition],
        allow_local_binding_creation=setup.advertise_local_binding_creation,
    )
    # Sidecar ACP metadata is SQLite-file based; PostgreSQL sessions recover their
    # recorded cwd from the authoritative session record instead.
    candidate_path = getattr(store, "path", None)
    store_path = (
        str(candidate_path)
        if candidate_path is not None and str(candidate_path) != ":memory:"
        else None
    )
    return HostBundle(
        definition=definition,
        registry=registry,
        manager=manager,
        store_path=store_path,
        meta=HostMetadataStore(store_path),
    )


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
        "--no-local-add",
        action="store_true",
        help="do not advertise local binding creation (plugin inventory hidden)",
    )
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
            "--no-local-add": args.no_local_add,
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
        local_binding_creation = not args.no_local_add
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
    """Locate a bundle builder; the default is this module's built-in composition."""
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
        return 2  # pragma: no cover - argparse exits
    try:
        bundle = resolve_builder(args.runtime_module)(setup)
        agent = build_agent(bundle, setup)
    except HarnessError as exc:
        print(f"harness-host: {exc.message}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_serve_until_closed(agent))
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        return 130
    return 0


async def _serve_until_closed(agent: HarnessHostAgent) -> None:
    try:
        await serve_agent(agent)
    finally:
        await agent.shutdown()


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
