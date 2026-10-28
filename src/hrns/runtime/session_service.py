"""Session composition, metadata, persistence, plans, usage, and presentation."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from acp import schema
from acp.exceptions import RequestError
from acp.schema import DeleteSessionResponse
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.planning import PlanItem, PlanStore, SqlitePlanStore

from hrns.config import EFFORT_DEFAULT
from hrns.protocol import (
    HrnsError,
    PresentationUpdated,
    ToolPresentationSnapshot,
)

from . import acp as acp_map
from ._unavailable_tools import UnavailableTools
from .acp_store import SessionRecord, finalize_usage, merge_usage, usage_payload
from .client_io import ClientSession
from .composition import Composition, compose_definition
from .host_support import HostPolicy, SessionDeps, _iso_millis
from .presentation import snapshot_from_metadata
from .resources import ANSWERS, CLIENT, MODEL, PLAN, WORKSPACE, ResourceSet
from .steering import SteeringCapability, SteeringChannel
from .todo import PLAN_TABLE, PlanChannel, forget_plan, plan_update

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic_ai_harness.experimental.acp import AcpSession

    from .acp_store import ScopedSessionStore
    from .definitions import AgentDefinition
    from .host import HrnsACPAgent
    from .registry import PluginRegistry


class SessionService:
    """Owns the live-session map and everything derived from a composition."""

    def __init__(
        self,
        *,
        agent: HrnsACPAgent,
        store: ScopedSessionStore,
        policy: HostPolicy,
        definition: AgentDefinition,
        registry: PluginRegistry,
    ) -> None:
        self.agent = agent
        self.store = store
        self.policy = policy
        self.definition = definition
        self.registry = registry
        self.compositions: dict[str, Composition] = {}
        self.steering: dict[str, SteeringChannel] = {}
        self.turn_usage: dict[str, dict[str, Any]] = {}
        self.plan_channels: dict[str, PlanChannel] = {}
        self.client_sessions: dict[str, ClientSession] = {}
        self.new_lock = asyncio.Lock()
        self.pending_initial: list[Any] | None = None
        self.pending_effort: str | None = None

    # ------------------------------------------------------------- metadata

    def forget(self, session_id: str) -> None:
        self.compositions.pop(session_id, None)
        self.steering.pop(session_id, None)
        self.turn_usage.pop(session_id, None)
        self.plan_channels.pop(session_id, None)
        self.client_sessions.pop(session_id, None)

    def remember_client(self, session: AcpSession, cwd: str) -> ClientSession:
        """Record the connection handle this session's client-origin tools act through."""
        known = self.client_sessions.get(session.session_id)
        if known is not None and known.client is session.client:
            return known
        handle = ClientSession(
            client=session.client,
            session_id=session.session_id,
            cwd=cwd,
            capabilities=(
                session.client_capabilities if self.policy.accepts_client_capabilities else None
            ),
        )
        self.client_sessions[session.session_id] = handle
        return handle

    def client_capabilities(self, session_id: str) -> schema.ClientCapabilities | None:
        """What the connected client advertised, for recomposition outside ``session_config``."""
        handle = self.client_sessions.get(session_id)
        return handle.capabilities if handle is not None else None

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None
    ) -> schema.ListSessionsResponse:
        cwd_filter = self.policy.validate_cwd(cwd) if cwd is not None else None
        page = await self.store.list(cwd=cwd_filter, cursor=cursor, limit=50)
        entries = [
            schema.SessionInfo(
                session_id=record.session_id,
                cwd=record.cwd,
                title=record.title,
                updated_at=_iso_millis(record.last_active_at),
            )
            for record in page.records
        ]
        return schema.ListSessionsResponse(sessions=entries, next_cursor=page.next_cursor)

    async def delete_session(self, session_id: str) -> DeleteSessionResponse:
        record = await self.store.get(session_id)
        if record is None:
            raise RequestError.invalid_params(
                {"sessionId": session_id, "reason": "no stored session with this id"}
            )
        await self.agent.close_session(session_id)
        await self.store.delete(session_id)
        database = self.plan_database()
        if database is not None:
            await asyncio.to_thread(forget_plan, database, session_id)
        return DeleteSessionResponse()

    # -------------------------------------------------------- session state

    def require_composition(self, session_id: str) -> Composition:
        composition = self.compositions.get(session_id)
        if composition is None:
            raise HrnsError(
                "CAPABILITY_UNAVAILABLE",
                "The session is not open in this host; load it before reading configuration.",
                {"sessionId": session_id},
            )
        return composition

    def composition(self, session_id: str) -> Composition | None:
        return self.compositions.get(session_id)

    def options_for(self, session_id: str) -> list[Any]:
        composition = self.compositions.get(session_id)
        support = composition.effort.support if composition is not None else None
        current_effort = (composition.effort.effort if composition is not None else None) or (
            EFFORT_DEFAULT
        )
        options: list[Any] = []
        model = self.agent.session_model_option(session_id)
        if model is not None:
            options.append(model)
        effort = acp_map.effort_option(current_effort, support=support)
        if effort is not None:
            options.append(effort)
        return options

    def context_window(self, session_id: str) -> int:
        """The selected model's context window, or 0 when the profile does not say."""
        composition = self.compositions.get(session_id)
        model = getattr(composition.agent, "model", None) if composition is not None else None
        window = getattr(model, "context_window", None)
        if isinstance(window, int) and not isinstance(window, bool) and window > 0:
            return window
        return 0

    # ------------------------------------------------------------- usage

    async def report_model_usage(self, session_id: str, response: ModelResponse) -> None:
        current = self.turn_usage.get(session_id)
        if current is None:
            return
        payload = usage_payload(response.usage)
        increment = dict(payload)
        increment["requests"] = 1
        current = merge_usage(current, increment)
        self.turn_usage[session_id] = current
        record = await self.store.get(session_id)
        await self.send_session_usage(
            session_id,
            finalize_usage(merge_usage(record.usage if record is not None else {}, current)),
        )
        await self.send_context_usage(session_id, payload)

    async def send_context_usage(self, session_id: str, payload: Mapping[str, Any]) -> None:
        """Publish how full the model's context window is after one model response.

        ACP's ``UsageUpdate`` carries the gauge (tokens in context) separately from the
        cumulative session counters, so clients can show a context readout that does not
        grow by the whole history on every turn.
        """
        conn = self.agent._client_conn
        if conn is None:
            return
        input_tokens = payload.get("input_tokens")
        output_tokens = payload.get("output_tokens")
        used = payload.get("total_tokens")
        if not isinstance(used, int):
            used = int(input_tokens or 0) + int(output_tokens or 0)
        detail = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            }
            and isinstance(value, int)
            and not isinstance(value, bool)
        }
        with contextlib.suppress(Exception):
            await conn.session_update(
                session_id=session_id,
                update=schema.UsageUpdate(
                    session_update="usage_update",
                    used=max(0, used),
                    size=self.context_window(session_id),
                    field_meta={"hrns": {"contextUsage": detail}},
                ),
            )

    async def send_session_usage(self, session_id: str, totals: Mapping[str, Any]) -> None:
        conn = self.agent._client_conn
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.session_update(
                session_id=session_id,
                update=schema.SessionInfoUpdate(
                    session_update="session_info_update",
                    field_meta={"hrns": {"sessionUsage": {"scope": "session", **totals}}},
                ),
            )

    async def send_session_title(self, session_id: str, title: str) -> None:
        conn = self.agent._client_conn
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.session_update(
                session_id=session_id,
                update=schema.SessionInfoUpdate(session_update="session_info_update", title=title),
            )

    # ------------------------------------------------------ presentation

    async def presentation_snapshots(
        self, session_id: str, tool_call_ids: Sequence[str]
    ) -> list[ToolPresentationSnapshot]:
        stored = await self.store.load(session_id)
        if stored is None:
            return []
        wanted = set(tool_call_ids)
        snapshots: dict[str, ToolPresentationSnapshot] = {}
        for message in stored.messages:
            for part in getattr(message, "parts", ()):
                if not isinstance(part, ToolReturnPart):
                    continue
                if part.tool_call_id not in wanted:
                    continue
                snapshot = snapshot_from_metadata(
                    session_id, part.tool_call_id, getattr(part, "metadata", None)
                )
                if snapshot is not None:
                    snapshots[part.tool_call_id] = snapshot
        return [snapshots[item] for item in tool_call_ids if item in snapshots]

    def presentation_sink(self, session_id: str) -> Any:
        async def publish(snapshot: ToolPresentationSnapshot) -> None:
            conn = self.agent._client_conn
            if conn is None:
                return
            with contextlib.suppress(Exception):
                await conn.ext_notification(
                    "hrns/presentation/updated",
                    PresentationUpdated(snapshots=[snapshot]).to_wire(),
                )

        del session_id
        return publish

    async def remembered_tool_names(self, session_id: str) -> frozenset[str]:
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

    async def apply_unavailable_tools(self, session_id: str, composition: Composition) -> None:
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
            remembered = await self.remembered_tool_names(session_id)
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

    # ---------------------------------------------------------------- plans

    def plan_channel(self, session_id: str) -> PlanChannel:
        channel = self.plan_channels.get(session_id)
        if channel is None:
            channel = PlanChannel(session_id, self.publish_plan, store=self.plan_store(session_id))
            self.plan_channels[session_id] = channel
        return channel

    def plan_database(self) -> str | None:
        """The SQLite file that can hold plans, or ``None`` for Postgres/in-memory stores."""
        path = getattr(self.store.backend, "path", None)
        if path is None or str(path) == ":memory:":
            return None
        return str(path)

    def plan_store(self, session_id: str) -> PlanStore | None:
        """Persist plans in the session database so a reload shows the last plan."""
        database = self.plan_database()
        if database is None:
            return None
        return SqlitePlanStore(database, session=session_id, table=PLAN_TABLE)

    async def publish_plan(self, session_id: str, items: list[PlanItem]) -> None:
        conn = self.agent._client_conn
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.session_update(
                session_id=session_id,
                update=plan_update(items),
            )

    # ----------------------------------------------------------- composition

    def resources(self, cwd: str, session_id: str | None = None) -> ResourceSet:
        """The deployment's bindings plus the services this session owns.

        A deployment binding always wins: the ACP client's cwd only fills a workspace slot the
        deployment left empty. That is what lets a host bind a remote or sandboxed workspace
        backend while the client still sends an ordinary path as its ``cwd``.
        """
        resources = ResourceSet(self.policy.resources)
        resources.bind_if_absent(ANSWERS, self.agent.question_channel)
        if session_id is not None:
            resources.bind_if_absent(PLAN, self.plan_channel(session_id))
            handle = self.client_sessions.get(session_id)
            if handle is not None:
                # Definitions may require the handle even while every client plugin is disabled.
                # Capability acceptance is enforced by the catalog and the handle's capabilities.
                resources.bind_if_absent(CLIENT, handle)
        model = self.agent._default_model
        if model is not None:
            resources.bind_if_absent(MODEL, model)
        if self.definition.declares(WORKSPACE):
            resources.bind_if_absent(WORKSPACE, cwd)
        return resources

    async def compose_session(self, session: Any) -> Any:
        agent = self.agent
        record = await self.store.get(session.session_id)
        initial = None
        saved = None
        effort = self.policy.default_effort
        if record is None:
            initial = self.pending_initial
            if self.pending_effort is not None:
                effort = self.pending_effort
        else:
            saved = record.bindings
            effort = record.effort or None
            if effort == EFFORT_DEFAULT:
                effort = None
        cwd = self.policy.validate_cwd(session.cwd)
        handle = self.remember_client(session, cwd)
        model = (record.model if record is not None else None) or agent._default_model
        support = self.policy.effort_support(model)
        composition = await compose_definition(
            self.definition,
            self.registry,
            resources=self.resources(cwd, session.session_id),
            session_id=session.session_id,
            principal=self.policy.principal,
            deployment_id=self.policy.deployment_id,
            initial_configuration=initial,
            saved_bindings=saved,
            model_settings=self.policy.model_settings,
            effort=effort,
            effort_support=support,
            presentation_sink=self.presentation_sink(session.session_id),
            client_capabilities=handle.capabilities,
            accepts_client_capabilities=self.policy.accepts_client_capabilities,
        )
        composition.effort.model = model
        self.compositions[session.session_id] = composition
        if record is None:
            await self.store.create(
                SessionRecord(
                    session_id=session.session_id,
                    principal=self.policy.principal,
                    deployment_id=self.policy.deployment_id,
                    agent_id=self.definition.agent_id,
                    definition_version=self.definition.version or "",
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
        steering = SteeringChannel()
        composition.config = type(composition.config)(
            deps=SessionDeps(session_id=session.session_id, composition=composition),
            capabilities=[
                *(composition.config.capabilities or ()),
                SteeringCapability(steering),
            ],
            toolsets=composition.config.toolsets,
            workspace=composition.config.workspace,
        )
        self.steering[session.session_id] = steering
        await self.apply_unavailable_tools(session.session_id, composition)
        channel = self.plan_channels.get(session.session_id)
        if channel is not None:
            await channel.publish_current()
        return composition.config

    # ------------------------------------------------------- small helpers

    @staticmethod
    def reject_additional_directories(additional_directories: Sequence[str] | None) -> None:
        if additional_directories:
            raise HrnsError(
                "INVALID_CONFIGURATION",
                "additionalDirectories is not supported by this host.",
                {"count": len(additional_directories)},
            )

    def reject_client_mcp(self, mcp_servers: Sequence[Any] | None) -> None:
        if not mcp_servers:
            return
        raise acp_map.mcp_forbidden_error(hosted=self.policy.hosted, servers=mcp_servers)
