"""Session composition, metadata, persistence, plans, usage, and presentation."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING, Any

from acp import schema
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
from .acp_store import SessionRecord, finalize_usage, merge_usage, now_ms, usage_payload
from .client_io import ClientGate, ClientSession
from .composition import Composition, compose_definition
from .host_support import HostPolicy, RevisionSpec, SessionDeps, _iso_millis
from .presentation import snapshot_from_metadata
from .request_context import current_connection_id
from .resources import (
    ANSWERS,
    CLIENT,
    MODEL,
    PLAN,
    WORKSPACE,
    ResourceBindings,
    ResourceSet,
)
from .session_policy import PolicyAction, SessionAccess, decide_policy
from .steering import SteeringCapability, SteeringChannel
from .todo import PLAN_TABLE, PlanChannel, plan_update

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pydantic_ai_harness.experimental.acp import AcpSession, AcpSessionConfig

    from .acp_store import ScopedSessionStore
    from .creation import SessionCreationOperation
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
        revision_resolver: Callable[[str], RevisionSpec | None] | None = None,
    ) -> None:
        self.agent = agent
        self.store = store
        self.policy = policy
        self.definition = definition
        self.registry = registry
        self.revision_resolver = revision_resolver
        self.compositions: dict[str, Composition] = {}
        self.steering: dict[str, SteeringChannel] = {}
        self.turn_usage: dict[str, dict[str, Any]] = {}
        self.plan_channels: dict[str, PlanChannel] = {}
        self.client_sessions: dict[str, ClientSession] = {}
        self.client_gate_locks: dict[str, asyncio.Lock] = {}
        self.new_lock = asyncio.Lock()
        self.pending_initial: list[Any] | None = None
        self.pending_effort: str | None = None
        self.pending_creation: SessionRecord | None = None
        self.prepared_configs: dict[str, AcpSessionConfig[object]] = {}

    @property
    def principal(self) -> str:
        """The principal acting on this request, not the actor's first connection."""
        return self.agent.acting_principal()

    # ------------------------------------------------------------- metadata

    def forget(self, session_id: str) -> None:
        self.store.discard(session_id)
        self.compositions.pop(session_id, None)
        self.steering.pop(session_id, None)
        self.turn_usage.pop(session_id, None)
        self.plan_channels.pop(session_id, None)
        self.client_sessions.pop(session_id, None)
        gate = self.agent.fanout.client_gates.pop(session_id, None)
        if gate is not None:
            gate.accepts = False
        self.prepared_configs.pop(session_id, None)

    def remember_client(
        self, session: AcpSession, cwd: str, *, accepts: bool | None = None
    ) -> ClientSession:
        """Record the connection handle this session's client-origin tools act through.

        ``accepts`` is the session's server gate. ``None`` falls back to the deployment
        default so an older record without an override keeps its old behavior.
        """
        if accepts is None:
            accepts = self.policy.accepts_client_capabilities
        self.agent.fanout.client_gates.setdefault(
            session.session_id, ClientGate(session.session_id, accepts)
        )
        # Client-origin tools go through the fan-out so the call is routed to the
        # connection that initiated the current turn, not whichever client composed
        # the session first.
        client = self.agent.fanout
        advertised = self.agent.current_capabilities()
        if advertised is None and current_connection_id.get() is None:
            # Local stdio has no connection registry; use the adapter's negotiated value.
            advertised = session.client_capabilities
        # Negotiation and authorization are separate: keep the advertised capabilities
        # so an authorized live re-enable can use them without a reconnect.
        capabilities = advertised
        known = self.client_sessions.get(session.session_id)
        if (
            known is not None
            and known.client is client
            and known.capabilities == capabilities
            and known.cwd == cwd
        ):
            return known
        handle = ClientSession(
            client=client,
            session_id=session.session_id,
            cwd=cwd,
            capabilities=capabilities,
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
        page = await self.store.list_visible(
            self.principal, cwd=cwd_filter, cursor=cursor, limit=50
        )
        entries: list[schema.SessionInfo] = []
        for record in page.records:
            decision = await decide_policy(
                self.policy.session_policy,
                self.agent.session_context(record, record.session_id),
                PolicyAction.LIST,
            )
            if not decision.allowed:
                continue
            entries.append(
                schema.SessionInfo(
                    session_id=record.session_id,
                    cwd=record.cwd,
                    title=record.title,
                    updated_at=_iso_millis(record.last_active_at),
                )
            )
        return schema.ListSessionsResponse(sessions=entries, next_cursor=page.next_cursor)

    async def ensure_persisted(self, session_id: str) -> bool:
        """Write the row for a session that ``session/new`` only composed in memory.

        Called at the moments a session becomes real: its first prompt, or an explicit rename.
        The staged metadata is refreshed from the live composition first, so effort, model, and
        binding changes made before the first prompt are kept with the session they belong to.
        """
        draft = self.store.staged(session_id)
        if draft is None:
            return await self.store.get_visible(session_id, self.principal) is not None
        composition = self.compositions.get(session_id)
        if composition is not None:
            draft.effort = composition.effort.effort or EFFORT_DEFAULT
            draft.bindings = composition.catalog.export_bindings()
        await self.store.materialize(session_id)
        return True

    async def cwd(self, session_id: str) -> str | None:
        """The workspace a session was opened in, staged or stored."""
        draft = self.store.staged(session_id)
        if draft is not None:
            return draft.cwd
        record = await self.store.get_visible(session_id, self.principal)
        return record.cwd if record is not None else None

    async def ensure_title(self, session_id: str, title: str) -> None:
        """Name a session after its first prompt, so even a failed turn stays recognizable."""
        if not title:
            return
        record = await self.store.get_visible(session_id, self.principal)
        if record is None or record.title:
            return
        await self.store.update(session_id, title=title)
        await self.send_session_title(session_id, title)

    async def persist_metadata(
        self,
        session_id: str,
        *,
        effort: str | None = None,
        bindings: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        """Record metadata for a staged session in memory, or write it once it has a row."""
        draft = self.store.staged(session_id)
        if draft is None:
            await self.store.update(session_id, effort=effort, bindings=bindings, touch=True)
            return
        if effort is not None:
            draft.effort = effort
        if bindings is not None:
            draft.bindings = dict(bindings)

    async def set_access(
        self,
        session_id: str,
        *,
        access: SessionAccess,
        collaborators: frozenset[str] | None = None,
        principal: str | None = None,
    ) -> bool:
        """Change a session's access mode, staged or stored.

        The caller is responsible for authorizing the change; this method only writes it.
        """
        acting = principal or self.principal
        draft = self.store.staged(session_id)
        if draft is not None:
            if draft.effective_owner != acting:
                raise HrnsError(
                    "POLICY_DENIED",
                    "Only the session owner may change its access mode.",
                    {"sessionId": session_id},
                )
            if draft.access == access and (
                collaborators is None or draft.collaborators == collaborators
            ):
                return False
            draft.access = access
            if collaborators is not None:
                draft.collaborators = collaborators
            # Sharing an unused session is itself a reason to persist it: another
            # principal must be able to find the row that grants it access.
            changed = await self.store.materialize(session_id)
        else:
            before = await self.store.get_visible(session_id, acting)
            if (
                before is not None
                and before.access == access
                and (collaborators is None or before.collaborators == collaborators)
            ):
                return False
            changed = await self.store.set_access(
                session_id, access=access, collaborators=collaborators, principal=acting
            )
        if changed:
            await self._retain_admitted_viewers(session_id, acting)
        return changed

    async def _retain_admitted_viewers(self, session_id: str, acting: str) -> None:
        """Drop viewers the new access decision no longer admits.

        The owner and still-admitted collaborators keep their existing subscriptions,
        including when the change came from the embedding API and has no request
        connection to re-subscribe. A missing record fails closed: without a decision
        there is no safe audience to retain.
        """
        record = await self.store.get_visible(session_id, acting)
        if record is None:
            self.agent.fanout.unsubscribe_session(session_id)
            return
        for connection_id in self.agent.fanout.subscribed_connections(session_id):
            principal = self.agent.connection_principal(connection_id)
            decision = await decide_policy(
                self.policy.session_policy,
                self.agent.session_context(
                    record,
                    session_id,
                    principal=principal,
                    connection_id=connection_id,
                ),
                PolicyAction.REPLAY,
            )
            if not decision.allowed:
                self.agent.fanout.unsubscribe(connection_id, session_id)

    async def set_client_capabilities(
        self,
        session_id: str,
        accepts: bool | None,
        *,
        principal: str | None = None,
        expected_revision: int | None = None,
    ) -> SessionRecord:
        """Commit a fenced capability override before opening the live gate."""
        acting = principal or self.principal
        async with self.client_gate_locks.setdefault(session_id, asyncio.Lock()):
            draft = self.store.staged(session_id)
            record = draft or await self.store.get_visible(session_id, acting)
            if record is None or record.effective_owner != acting:
                raise HrnsError("SESSION_NOT_FOUND", "No session with this id is available.")
            if expected_revision is not None and record.capability_revision != expected_revision:
                raise HrnsError(
                    "REVISION_CONFLICT",
                    "The capability revision has changed.",
                    {"expectedRevision": expected_revision, "revision": record.capability_revision},
                )
            if draft is not None:
                await self.store.materialize(session_id)
            revision = self.revision_for(record)
            effective = revision.accepts_client_capabilities if accepts is None else accepts
            gate = self.agent.fanout.client_gates.get(session_id)
            if gate is None:
                previous = (
                    record.accepts_client_capabilities
                    if record.accepts_client_capabilities is not None
                    else revision.accepts_client_capabilities
                )
                gate = self.agent.fanout.client_gates.setdefault(
                    session_id, ClientGate(session_id, previous)
                )
            # Close the live gate before awaiting persistence. If the store commits but
            # the caller is cancelled before receiving its result, the actor must not
            # continue to grant the now-durably-revoked capability.
            # A failed or superseded write must never leave stale authorization live.
            gate.accepts = False
            lease = await self.agent.acquire_session_lease(session_id)
            temporary = lease is None
            if temporary:
                owner = f"local-capability:{uuid.uuid4().hex}"
                acquisition = asyncio.create_task(
                    self.store.acquire_lease(session_id, owner, self.policy.lease_ttl_ms)
                )
                try:
                    lease = await asyncio.shield(acquisition)
                except asyncio.CancelledError:
                    while not acquisition.done():
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await asyncio.shield(acquisition)
                    if not acquisition.cancelled() and acquisition.exception() is None:
                        acquired = acquisition.result()
                        if acquired is not None:
                            await self._release_capability_lease(
                                session_id, acquired.owner_id, acquired.fence
                            )
                    raise
            if lease is None:
                gate.accepts = False
                raise HrnsError("SESSION_HELD", "Another worker owns this session.")
            write: asyncio.Task[SessionRecord] | None = None
            try:

                async def commit() -> SessionRecord:
                    committed = await self.store.set_client_capabilities(
                        session_id,
                        accepts,
                        principal=acting,
                        expected_revision=record.capability_revision
                        if expected_revision is None
                        else expected_revision,
                        owner_id=lease.owner_id,
                        fence=lease.fence,
                    )
                    # A superseded actor must not reopen its gate after its write committed.
                    current_lease = await self.store.get_lease(session_id)
                    if (
                        current_lease is None
                        or current_lease.owner_id != lease.owner_id
                        or current_lease.fence != lease.fence
                        or current_lease.expires_at <= now_ms()
                    ):
                        raise HrnsError("SESSION_CONFLICT", "The session lease was lost.")
                    gate.accepts = effective
                    composition = self.compositions.get(session_id)
                    if composition is not None:
                        composition.catalog.accepts_client_capabilities = effective
                        for offering in composition.catalog.offerings.values():
                            offering.accepts_client_capabilities = effective
                    return committed

                write = asyncio.create_task(commit())
                try:
                    return await asyncio.shield(write)
                except asyncio.CancelledError:
                    while not write.done():
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await asyncio.shield(write)
                    if not write.cancelled():
                        write.exception()
                    raise
            except BaseException:
                if (
                    write is None
                    or not write.done()
                    or write.cancelled()
                    or write.exception() is not None
                ):
                    gate.accepts = False
                raise
            finally:
                if temporary:
                    await self._release_capability_lease(session_id, lease.owner_id, lease.fence)

    async def _release_capability_lease(self, session_id: str, owner_id: str, fence: int) -> None:
        release = asyncio.create_task(self.store.release_lease(session_id, owner_id, fence))
        cancelled = False
        while not release.done():
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError:
                cancelled = True
        release.result()
        if cancelled:
            raise asyncio.CancelledError

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
        if model is None and composition is not None and composition.models:
            current_model = composition.effort.model
            if current_model is not None:
                model = schema.SessionConfigOptionSelect(
                    id=acp_map.MODEL_OPTION_ID,
                    name="Model",
                    type="select",
                    current_value=current_model,
                    options=[
                        schema.SessionConfigSelectOption(value=value, name=value)
                        for value in composition.models
                    ],
                )
        if model is not None:
            if composition is not None and composition.models:
                model = model.model_copy(
                    update={
                        "current_value": composition.effort.model or model.current_value,
                        "options": [
                            schema.SessionConfigSelectOption(value=value, name=value)
                            for value in composition.models
                        ],
                    }
                )
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
        record = await self.store.get_visible(session_id, self.principal)
        await self.send_session_usage(
            session_id,
            finalize_usage(merge_usage(record.usage if record is not None else {}, current)),
            durable=False,
        )
        await self.send_context_usage(session_id, payload)

    async def send_context_usage(self, session_id: str, payload: Mapping[str, Any]) -> None:
        """Publish how full the model's context window is after one model response.

        ACP's ``UsageUpdate`` carries the gauge (tokens in context) separately from the
        cumulative session counters, so clients can show a context readout that does not
        grow by the whole history on every turn.
        """
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
        await self.agent.publish_session_update(
            session_id,
            schema.UsageUpdate(
                session_update="usage_update",
                used=max(0, used),
                size=self.context_window(session_id),
                field_meta={"hrns": {"contextUsage": detail}},
            ),
            durable=False,
        )

    async def send_session_usage(
        self,
        session_id: str,
        totals: Mapping[str, Any],
        *,
        durable: bool = True,
    ) -> None:
        await self.agent.publish_session_update(
            session_id,
            schema.SessionInfoUpdate(
                session_update="session_info_update",
                field_meta={"hrns": {"sessionUsage": {"scope": "session", **totals}}},
            ),
            durable=durable,
        )

    async def send_session_title(self, session_id: str, title: str) -> None:
        await self.agent.publish_session_update(
            session_id,
            schema.SessionInfoUpdate(session_update="session_info_update", title=title),
            durable=True,
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
            with contextlib.suppress(Exception):
                await self.agent.fanout.session_notification(
                    session_id,
                    "hrns/presentation/updated",
                    PresentationUpdated(snapshots=[snapshot]).to_wire(),
                )

        return publish

    async def remembered_tool_names(self, session_id: str) -> frozenset[str]:
        """Function-tool names already present in the session's native history.

        Parts the framework emits for its own tools — a typed `ToolCallPart` such as
        `load_capability` or `search_tools` — are skipped: they are not function tools,
        and stubbing one would shadow the tool the framework installs for the run. A
        deferred-capability loader then refuses the whole request because the name is
        reserved, so resuming a session that had loaded a skill failed every turn.
        """
        stored = await self.store.load(session_id)
        if stored is None:
            return frozenset()
        names: set[str] = set()
        for message in stored.messages:
            for part in getattr(message, "parts", ()):
                if isinstance(part, ToolCallPart) and part.tool_kind is None and part.tool_name:
                    names.add(part.tool_name)
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
        await self.agent.publish_session_update(
            session_id,
            plan_update(items),
            durable=True,
        )

    # ----------------------------------------------------------- composition

    def current_revision(self) -> RevisionSpec:
        """The latest native revision this actor serves for new sessions."""
        return RevisionSpec(
            definition=self.definition,
            allowed_cwds=self.policy.allowed_cwds,
            default_cwd=self.policy.default_cwd,
            models=self.policy.models,
            thinking_models=self.policy.thinking_models,
            resources=self.policy.resources,
            accepts_client_capabilities=self.policy.accepts_client_capabilities,
        )

    def revision_for(self, record: SessionRecord | None) -> RevisionSpec:
        """Resolve the complete revision pinned by a stored session.

        A hosted server's resolver fails closed when the pinned revision is not native;
        silently falling back to the latest revision would execute different tools and
        resources under the old row's version.
        """
        if record is not None and record.definition_version and self.revision_resolver:
            revision = self.revision_resolver(record.definition_version)
            if revision is None:
                raise HrnsError(
                    "DEPLOYMENT_UNAVAILABLE",
                    "The revision pinned by this session is not registered in this process.",
                    {
                        "sessionId": record.session_id,
                        "definitionVersion": record.definition_version,
                    },
                )
            return revision
        if (
            record is not None
            and record.definition_version
            and self.policy.hosted
            and record.definition_version != (self.definition.version or "")
        ):
            raise HrnsError(
                "DEPLOYMENT_UNAVAILABLE",
                "The revision pinned by this session is not registered in this process.",
                {
                    "sessionId": record.session_id,
                    "definitionVersion": record.definition_version,
                },
            )
        return self.current_revision()

    def resources(
        self,
        cwd: str,
        session_id: str | None = None,
        definition: AgentDefinition | None = None,
        resource_bindings: ResourceSet | ResourceBindings | None = None,
        model: str | None = None,
    ) -> ResourceSet:
        """The deployment's bindings plus the services this session owns.

        A deployment binding always wins: the ACP client's cwd only fills a workspace slot the
        deployment left empty. That is what lets a host bind a remote or sandboxed workspace
        backend while the client still sends an ordinary path as its ``cwd``.
        """
        resources = ResourceSet(
            self.policy.resources if resource_bindings is None else resource_bindings
        )
        resources.bind_if_absent(ANSWERS, self.agent.question_channel)
        if session_id is not None:
            resources.bind_if_absent(PLAN, self.plan_channel(session_id))
            handle = self.client_sessions.get(session_id)
            if handle is not None:
                # Definitions may require the handle even while every client plugin is disabled.
                # Capability acceptance is enforced by the catalog and the live client gate.
                resources.bind_if_absent(CLIENT, handle)
        bound_model = model or self.agent._default_model
        if bound_model is not None:
            resources.bind_if_absent(MODEL, bound_model)
        if (definition or self.definition).declares(WORKSPACE):
            resources.bind_if_absent(WORKSPACE, cwd)
        return resources

    async def resolved_inputs(
        self,
        *,
        record: SessionRecord | None,
        definition: AgentDefinition,
        cwd: str,
        session_id: str,
        resource_bindings: ResourceSet | ResourceBindings,
        model: str | None,
        action: PolicyAction,
        creation: SessionCreationOperation | None = None,
    ) -> tuple[ResourceSet, object]:
        resources = self.resources(cwd, session_id, definition, resource_bindings, model)
        if self.agent.bundle.resource_resolver is None:
            return resources, None
        if record is None:
            raise HrnsError("SESSION_NOT_FOUND", "Resource resolution requires a session record.")
        resolved = await self.agent.resolve_session_resources(record, action, creation=creation)
        if resolved is None:
            raise HrnsError(
                "INVALID_CONFIGURATION", "The session resource resolver is unavailable."
            )
        declared = {
            name: requirement
            for requirement in definition.resources
            for name in (requirement.name, requirement.slot.name, *requirement.slot.aliases)
        }
        for name, value in resolved.resources.values.items():
            requirement = declared.get(name)
            if requirement is None:
                raise HrnsError(
                    "INVALID_CONFIGURATION",
                    "The session resource resolver supplied an undeclared resource.",
                    {"resource": name},
                )
            for alias in (requirement.name, requirement.slot.name, *requirement.slot.aliases):
                resources.bind(alias, None)
            resources.bind(requirement.slot, value)
        return resources, resolved.deps

    def adopt_composition(
        self, session_id: str, composition: Composition, refreshed: Composition
    ) -> None:
        """Replace a live composition without invalidating the adapter's SessionDeps."""
        steering = self.steering.get(session_id)
        refreshed.config = type(refreshed.config)(
            deps=SessionDeps(session_id=session_id, composition=composition),
            capabilities=[
                *(refreshed.config.capabilities or ()),
                *([SteeringCapability(steering)] if steering is not None else []),
            ],
            toolsets=refreshed.config.toolsets,
            workspace=refreshed.config.workspace,
        )
        composition.agent = refreshed.agent
        composition.catalog = refreshed.catalog
        composition.snapshot = refreshed.snapshot
        composition.resources = refreshed.resources
        composition.effort = refreshed.effort
        composition.config = refreshed.config
        composition.capabilities = refreshed.capabilities
        composition.commands = refreshed.commands
        composition.presentation = refreshed.presentation
        composition.workspace = refreshed.workspace
        composition.deps = refreshed.deps
        composition.models = refreshed.models

    async def refresh_turn_resources(self, session_id: str) -> None:
        """Compose with this turn's authorized bindings, never a previous caller's."""
        if self.agent.bundle.resource_resolver is None:
            return
        composition = self.require_composition(session_id)
        record = await self.agent.session_record(session_id)
        if record is None:
            raise HrnsError("SESSION_NOT_FOUND", "no stored session with this id")
        revision = self.revision_for(record)
        cwd = self.policy.validate_cwd(record.cwd, allowed=revision.allowed_cwds)
        model = composition.effort.model or revision.default_model or self.agent._default_model
        accepts = (
            record.accepts_client_capabilities
            if record.accepts_client_capabilities is not None
            else revision.accepts_client_capabilities
        )
        resources, deps = await self.resolved_inputs(
            record=record,
            definition=revision.definition,
            cwd=cwd,
            session_id=session_id,
            resource_bindings=revision.resources,
            model=model,
            action=PolicyAction.PROMPT,
        )
        refreshed = await compose_definition(
            revision.definition,
            self.registry,
            resources=resources,
            deps=deps,
            session_id=session_id,
            principal=record.effective_owner,
            deployment_id=record.deployment_id,
            saved_bindings=composition.catalog.export_bindings(),
            model_settings=self.policy.model_settings,
            effort=composition.effort.effort,
            effort_support=(
                revision.effort_support(model)
                if revision.models
                else self.policy.effort_support(model)
            ),
            provider_types=self.policy.provider_types,
            presentation_sink=self.presentation_sink(session_id),
            client_capabilities=self.client_capabilities(session_id),
            accepts_client_capabilities=accepts,
            client_gate=self.agent.fanout.client_gates.get(session_id),
            selected_revision=composition.snapshot.selected_revision,
        )
        gate = self.agent.fanout.client_gates.get(session_id)
        if gate is not None and gate.accepts != accepts:
            raise HrnsError(
                "CAPABILITY_UNAVAILABLE", "The session capability gate changed during composition."
            )
        refreshed.effort.model = model
        refreshed.models = revision.models
        self.adopt_composition(session_id, composition, refreshed)
        await self.apply_unavailable_tools(session_id, composition)

    async def compose_session(
        self, session: Any, *, creation: SessionCreationOperation | None = None
    ) -> Any:
        async with self.client_gate_locks.setdefault(session.session_id, asyncio.Lock()):
            return await self._compose_session_locked(session, creation=creation)

    async def _compose_session_locked(
        self, session: Any, *, creation: SessionCreationOperation | None = None
    ) -> Any:
        agent = self.agent
        prepared = self.prepared_configs.pop(session.session_id, None)
        if prepared is not None:
            return prepared
        # Subscribe the initiator before composition can publish presentation, plan, or
        # configuration notifications. Hosted actors have no no-subscriber fallback.
        connection_id = current_connection_id.get()
        if connection_id is not None:
            self.agent.fanout.subscribe(connection_id, session.session_id)
        record = await self.store.get_visible(session.session_id, self.principal)
        draft = self.store.staged(session.session_id) if record is None else None
        known = record or draft
        initial = None
        effort = self.policy.default_effort
        creating = self.pending_creation is not None and (
            self.pending_creation.session_id == session.session_id
        )
        if creating != (creation is not None):
            raise HrnsError(
                "SESSION_CREATION_CONFLICT", "The session creation context is missing or invalid."
            )
        if known is None or creating:
            initial = self.pending_initial
            if self.pending_effort is not None:
                effort = self.pending_effort
        else:
            effort = known.effort or None
            if effort == EFFORT_DEFAULT:
                effort = None
        revision = self.revision_for(known)
        cwd = self.policy.validate_cwd(session.cwd, allowed=revision.allowed_cwds)
        accepts = (
            known.accepts_client_capabilities
            if known is not None and known.accepts_client_capabilities is not None
            else revision.accepts_client_capabilities
        )
        handle = self.remember_client(session, cwd, accepts=accepts)
        model = (
            (known.model if known is not None else None)
            or revision.default_model
            or agent._default_model
        )
        saved = known.bindings if known is not None and not creating else None
        support = (
            revision.effort_support(model) if revision.models else self.policy.effort_support(model)
        )
        definition = revision.definition
        resources, deps = await self.resolved_inputs(
            record=known,
            definition=definition,
            cwd=cwd,
            session_id=session.session_id,
            resource_bindings=revision.resources,
            model=model,
            action=PolicyAction.CREATE if creating else PolicyAction.LOAD,
            creation=creation,
        )
        composition = await compose_definition(
            definition,
            self.registry,
            resources=resources,
            session_id=session.session_id,
            principal=(known.effective_owner if known is not None else self.principal),
            deployment_id=self.policy.deployment_id,
            initial_configuration=initial,
            saved_bindings=saved,
            model_settings=self.policy.model_settings,
            effort=effort,
            effort_support=support,
            provider_types=self.policy.provider_types,
            deps=deps,
            presentation_sink=self.presentation_sink(session.session_id),
            client_capabilities=handle.capabilities,
            accepts_client_capabilities=accepts,
            client_gate=self.agent.fanout.client_gates[session.session_id],
        )
        latest = await self.store.get_visible(session.session_id, self.principal)
        if latest is not None:
            effective = (
                latest.accepts_client_capabilities
                if latest.accepts_client_capabilities is not None
                else revision.accepts_client_capabilities
            )
            gate = self.agent.fanout.client_gates[session.session_id]
            if effective != accepts or gate.accepts != accepts:
                if not effective:
                    gate.accepts = False
                raise HrnsError(
                    "CAPABILITY_UNAVAILABLE",
                    "The session capability gate changed during composition; load it again.",
                    {"sessionId": session.session_id},
                )
        composition.effort.model = model
        composition.models = revision.models
        self.compositions[session.session_id] = composition
        if record is None:
            # Composed, not yet used: the row is staged and written when the session is used.
            if creating and self.pending_creation is not None:
                self.pending_creation.bindings = composition.catalog.export_bindings()
                self.pending_creation.model = model
                self.pending_creation.effort = composition.effort.effort or EFFORT_DEFAULT
                self.store.stage(self.pending_creation)
            else:
                self.store.stage(
                    SessionRecord(
                        session_id=session.session_id,
                        principal=self.principal,
                        deployment_id=self.policy.deployment_id,
                        agent_id=definition.agent_id,
                        definition_version=definition.version or "",
                        cwd=cwd,
                        effort=effort or EFFORT_DEFAULT,
                        bindings=composition.catalog.export_bindings(),
                    )
                )
        elif not record.read_only:
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
