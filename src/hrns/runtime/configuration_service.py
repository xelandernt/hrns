"""Configuration update transactions, caching, and notifications."""

from __future__ import annotations

import contextlib
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from hrns.protocol import (
    AddChange,
    ConfigurationSnapshot,
    ConfigurationUpdate,
    HrnsError,
)

from .composition import compose_definition
from .host_support import HostPolicy
from .policy import ChangeOutcome, changes_payload_hash, jsonable_config
from .session_policy import PolicyAction

if TYPE_CHECKING:
    from .host import HrnsACPAgent
    from .registry import PluginRegistry


class ConfigurationService:
    """Owns transactional configuration changes for live sessions."""

    def __init__(
        self,
        *,
        agent: HrnsACPAgent,
        policy: HostPolicy,
        registry: PluginRegistry,
    ) -> None:
        self.agent = agent
        self.policy = policy
        self.registry = registry
        self.requests: dict[str, OrderedDict[str, tuple[str, ConfigurationSnapshot]]] = {}

    def forget(self, session_id: str) -> None:
        self.requests.pop(session_id, None)

    def bindings_payload(self, outcome: ChangeOutcome) -> dict[str, dict[str, Any]]:
        added = {binding_id: plugin_id for binding_id, plugin_id, _, _ in outcome.added}
        return {
            binding_id: {
                "enabled": enabled,
                "config": jsonable_config(config),
                **({"plugin_id": added[binding_id]} if binding_id in added else {}),
            }
            for binding_id, (enabled, config) in outcome.states.items()
        }

    async def update(self, request: ConfigurationUpdate) -> dict[str, Any]:
        return await self.agent.order_configuration_update(request)

    def validate(self, request: ConfigurationUpdate) -> None:
        """Reject transactions that can never be admitted before they are queued."""
        if not self.policy.can_create_bindings and any(
            isinstance(change, AddChange) for change in request.changes
        ):
            raise HrnsError(
                "POLICY_DENIED",
                "Local binding creation is prohibited for this deployment.",
            )

    async def apply(
        self, request: ConfigurationUpdate, *, host_authoritative: bool = False
    ) -> dict[str, Any]:
        """Apply one configuration transaction at an ordered turn boundary."""
        agent = self.agent
        session_id = request.session_id
        self.validate(request)
        request_hash = f"{changes_payload_hash(request.changes)}|{request.expected_revision}"
        cache = self.requests.setdefault(session_id, OrderedDict())
        cached = cache.get(request.request_id)
        if cached is not None:
            if cached[0] != request_hash:
                raise HrnsError(
                    "INVALID_CONFIGURATION",
                    "A request id cannot be reused with a different configuration payload.",
                    {"requestId": request.request_id},
                )
            return cached[1].to_wire()
        composition = agent.sessions.require_composition(session_id)
        if request.expected_revision != composition.snapshot.selected_revision:
            raise HrnsError(
                "REVISION_CONFLICT",
                "The configuration changed since the client's expected revision.",
                {
                    "snapshot": composition.snapshot.to_wire(),
                    "expectedRevision": request.expected_revision,
                },
            )
        outcome = composition.catalog.apply_changes(
            list(request.changes),
            host_authoritative=host_authoritative,
        )
        if outcome.noop:
            snapshot = composition.snapshot
            cache[request.request_id] = (request_hash, snapshot)
            return snapshot.to_wire()
        cwd = await agent.sessions.cwd(session_id)
        if cwd is None:
            raise HrnsError("SESSION_CLOSED", "Load the session before updating configuration.")
        record = await agent.session_record(session_id)
        revision = agent.sessions.revision_for(record)
        effective_accepts = (
            record.accepts_client_capabilities
            if record is not None and record.accepts_client_capabilities is not None
            else revision.accepts_client_capabilities
        )
        next_revision = composition.snapshot.selected_revision + 1
        next_model = composition.effort.model or revision.default_model
        effective_model = next_model or agent._default_model
        support = (
            revision.effort_support(effective_model)
            if revision.models
            else self.policy.effort_support(effective_model)
        )
        resources, deps, capability_bindings = await agent.sessions.resolved_inputs(
            record=record,
            definition=revision.definition,
            cwd=self.policy.validate_cwd(cwd, allowed=revision.allowed_cwds),
            session_id=session_id,
            resource_bindings=revision.resources,
            model=next_model,
            action=PolicyAction.CONFIGURE,
        )
        if agent.bundle.resource_resolver is None:
            deps = composition.deps
        refreshed = await compose_definition(
            revision.definition,
            self.registry,
            resources=resources,
            session_id=session_id,
            principal=record.effective_owner if record is not None else agent.acting_principal(),
            deployment_id=self.policy.deployment_id,
            saved_bindings=self.bindings_payload(outcome),
            model_settings=self.policy.model_settings,
            effort=composition.effort.effort,
            effort_support=support,
            deps=deps,
            presentation_sink=agent.sessions.presentation_sink(session_id),
            client_capabilities=agent.sessions.client_capabilities(session_id),
            accepts_client_capabilities=effective_accepts,
            client_gate=agent.fanout.client_gates.get(session_id),
            selected_revision=next_revision,
            capability_bindings=capability_bindings,
        )
        gate = agent.fanout.client_gates.get(session_id)
        if gate is not None and gate.accepts != effective_accepts:
            raise HrnsError(
                "CAPABILITY_UNAVAILABLE",
                "The session capability gate changed during configuration; retry the update.",
                {"sessionId": session_id},
            )
        refreshed.effort.model = effective_model
        refreshed.models = revision.models
        snapshot = refreshed.catalog.snapshot(
            selected_revision=next_revision,
            active_revision=next_revision,
            application_status="applied",
        )
        refreshed.snapshot = snapshot
        await agent.sessions.persist_metadata(
            session_id, bindings=refreshed.catalog.export_bindings()
        )
        if gate is not None:
            refreshed.catalog.accepts_client_capabilities = gate.accepts
            for offering in refreshed.catalog.offerings.values():
                offering.accepts_client_capabilities = gate.accepts
        agent.sessions.adopt_composition(session_id, composition, refreshed)
        await agent.sessions.apply_unavailable_tools(session_id, composition)
        cache[request.request_id] = (request_hash, snapshot)
        await self.send_changed(snapshot)
        await agent.commands.send_changed(session_id, composition)
        return snapshot.to_wire()

    async def send_changed(self, snapshot: ConfigurationSnapshot) -> None:
        with contextlib.suppress(Exception):
            await self.agent.fanout.session_notification(
                snapshot.session_id,
                "hrns/configuration/changed",
                snapshot.to_wire(),
            )
