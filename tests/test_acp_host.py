"""In-process contract tests for the ACP host adapter.

These drive :class:`harness_runtime.host.HarnessHostAgent` directly (no subprocess)
against the real runtime session controller, policy, SQLite store, and event
translation, with the demo deployment from ``test_acp_runtime_double``. Turns run a
real Pydantic AI agent on ``FunctionModel`` — no provider credentials.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import test_acp_runtime_double as demo
from acp import schema
from acp.exceptions import RequestError
from harness_protocol import BindingListResponse, ConfigurationSnapshot, PluginListResponse
from harness_runtime.host import HarnessHostAgent, HostSetup, build_agent

EXTENSION_METHODS = [
    "_harness/capabilities/list",
    "_harness/plugins/list",
    "_harness/configuration/get",
    "_harness/configuration/update",
    "_harness/configuration/changed",
]


def boolean_capabilities() -> schema.ClientCapabilities:
    return schema.ClientCapabilities(
        session=schema.ClientSessionCapabilities(
            config_options=schema.SessionConfigOptionsCapabilities(
                boolean=schema.BooleanConfigOptionCapabilities()
            )
        )
    )


class FakeClient:
    """Records ACP updates and answers permission requests like a terminal client."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, schema.SessionUpdate]] = []
        self.permissions: list[dict[str, Any]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.permission_choice = "allow_once"
        self.permission_outcomes: list[str] = []
        self.permission_gate: asyncio.Event | None = None

    async def session_update(self, session_id: str, update: schema.SessionUpdate, **kwargs: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        self.permissions.append(
            {"session_id": session_id, "tool_call": tool_call, "options": list(options)}
        )
        if self.permission_gate is not None:
            gate, self.permission_gate = self.permission_gate, None
            await gate.wait()
        outcome = self.permission_outcomes.pop(0) if self.permission_outcomes else "selected"
        if outcome == "cancelled":
            return schema.RequestPermissionResponse(
                outcome=schema.DeniedOutcome(outcome="cancelled")
            )
        choice = self.permission_choice if outcome == "selected" else outcome
        option = next((item for item in options if item.kind == choice), options[0])
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id=option.option_id)
        )

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))

    # --- helpers -------------------------------------------------------

    def updates_of(self, kind: str) -> list[schema.SessionUpdate]:
        return [update for _, update in self.updates if update.session_update == kind]

    def text(self) -> str:
        return "".join(
            chunk.content.text
            for chunk in self.updates_of("agent_message_chunk")
            if getattr(chunk.content, "text", None)
        )


async def make_host(
    tmp_path: Path,
    *,
    hosted: bool = False,
    allowed: tuple[str, ...] | None = None,
    models: tuple[str, ...] = (),
    boolean: bool = True,
    store: bool = True,
    principal: str = "tester",
    deployment: str = "dep-1",
    extension_version: int = 1,
    client_name: str = "pytest",
) -> tuple[HarnessHostAgent, FakeClient]:
    setup = HostSetup(
        definition="test_acp_runtime_double:demo_definition",
        store_path=str(tmp_path / "runtime.sqlite3") if store else None,
        models=models,
        hosted=hosted,
        principal=principal,
        deployment_id=deployment,
        allowed_cwds=allowed,
    )
    bundle = demo.build_host_bundle(setup)
    agent = build_agent(bundle, setup)
    client = FakeClient()
    agent.on_connect(client)
    await agent.initialize(
        1,
        boolean_capabilities() if boolean else schema.ClientCapabilities(),
        schema.Implementation(name=client_name, version="0.0"),
        harness={"version": extension_version, "methods": EXTENSION_METHODS},
    )
    return agent, client


async def new_session(agent: HarnessHostAgent, cwd: Path | str, **meta: Any) -> schema.NewSessionResponse:
    kwargs: dict[str, Any] = {}
    if meta:
        kwargs["harness"] = meta
    return await agent.new_session(cwd=str(cwd), **kwargs)


def error_data(exc: RequestError) -> dict[str, Any]:
    assert isinstance(exc.data, dict), exc.data
    return exc.data


async def wait_until(predicate: Any, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached in time")


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def test_initialize_advertises_standard_surface_and_extensions(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path)

    response = await agent.initialize(
        1,
        boolean_capabilities(),
        schema.Implementation(name="pytest", version="0.0"),
        harness={"version": 1, "methods": EXTENSION_METHODS},
    )

    assert response.protocol_version == 1
    capabilities = response.agent_capabilities
    assert capabilities.load_session is True
    assert capabilities.session_capabilities.list is not None
    assert capabilities.session_capabilities.delete is not None
    assert capabilities.session_capabilities.close is not None
    assert response.agent_info.name == "Demo Agent"
    assert response.agent_info.version == "1.0.0"
    advertisement = response.field_meta["harness"]
    assert advertisement["version"] == 1
    assert advertisement["localBindingCreation"] is True
    assert "_harness/plugins/list" in advertisement["methods"]
    assert "_harness/capabilities/list" in advertisement["methods"]
    # Nothing is consumed by merely initializing.
    assert client.updates == []


async def test_hosted_advertisement_hides_local_binding_creation(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    agent, _ = await make_host(tmp_path, hosted=True, allowed=(str(work),))

    response = await agent.initialize(
        1,
        boolean_capabilities(),
        harness={"version": 1, "methods": EXTENSION_METHODS},
    )

    advertisement = response.field_meta["harness"]
    assert advertisement["localBindingCreation"] is False
    assert "_harness/plugins/list" not in advertisement["methods"]
    assert "_harness/capabilities/list" in advertisement["methods"]
    assert agent.policy.allow_local_binding_creation is False


# ---------------------------------------------------------------------------
# Session creation and workspace policy
# ---------------------------------------------------------------------------


async def test_new_session_returns_model_and_boolean_toggle_options(tmp_path: Path) -> None:
    agent, _ = await make_host(tmp_path, models=("test",))

    response = await new_session(agent, tmp_path)

    options = {option.id: option for option in response.config_options}
    model = options["model"]
    assert isinstance(model, schema.SessionConfigOptionSelect)
    assert model.current_value == "test"
    assert [entry.value for entry in model.options] == ["test"]
    tickets = options["binding:tickets"]
    assert isinstance(tickets, schema.SessionConfigOptionBoolean)
    assert tickets.current_value is False
    # Mandatory (client_can_disable=False) and new_session bindings are not standard options.
    assert "binding:locked" not in options
    assert "binding:session_tool" not in options


async def test_toggle_falls_back_to_select_without_boolean_capability(tmp_path: Path) -> None:
    agent, _ = await make_host(tmp_path, boolean=False)

    response = await new_session(agent, tmp_path)
    tickets = next(option for option in response.config_options if option.id == "binding:tickets")
    assert isinstance(tickets, schema.SessionConfigOptionSelect)
    assert tickets.current_value == "off"
    assert {entry.value for entry in tickets.options} == {"on", "off"}

    updated = await agent.set_config_option("binding:tickets", response.session_id, "on")
    tickets = next(option for option in updated.config_options if option.id == "binding:tickets")
    assert tickets.current_value == "on"


async def test_new_session_rejects_relative_and_escaping_cwds(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = work / "escape"
    link.symlink_to(outside)
    agent, _ = await make_host(tmp_path, allowed=(str(work),))

    with pytest.raises(RequestError) as relative:
        await new_session(agent, "relative/path")
    assert error_data(relative.value)["code"] == "INVALID_CONFIGURATION"

    with pytest.raises(RequestError) as sibling:
        await new_session(agent, outside)
    assert error_data(sibling.value)["code"] == "WORKSPACE_UNAVAILABLE"

    with pytest.raises(RequestError) as symlink:
        await new_session(agent, link)
    assert error_data(symlink.value)["code"] == "WORKSPACE_UNAVAILABLE"

    accepted = await new_session(agent, work)
    assert agent._sessions[accepted.session_id].cwd == os.path.realpath(work)


# ---------------------------------------------------------------------------
# Extension configuration
# ---------------------------------------------------------------------------


async def test_initial_configuration_and_capability_listing(tmp_path: Path) -> None:
    agent, _ = await make_host(tmp_path)

    response = await new_session(
        agent,
        tmp_path,
        initialConfiguration=[{"type": "enable", "bindingId": "tickets", "enabled": True}],
    )
    tickets = next(option for option in response.config_options if option.id == "binding:tickets")
    assert tickets.current_value is True

    listing = await agent.ext_method(
        "harness/capabilities/list", {"sessionId": response.session_id}
    )
    assert listing["schemaVersion"] == 1
    assert listing["selectedRevision"] == 1
    assert listing["activeRevision"] == 1
    assert listing["policyRevision"] >= 1
    assert "nextCursor" not in listing
    validated = BindingListResponse.model_validate(listing)  # canonical DTO shape
    assert validated.next_cursor is None
    descriptors = {entry["bindingId"]: entry for entry in listing["items"]}
    assert set(descriptors) == {"tickets", "locked", "session_tool"}
    tickets_descriptor = descriptors["tickets"]
    assert tickets_descriptor["enabled"] is True
    assert tickets_descriptor["config"]["endpoint"] == "https://tickets.example.invalid"
    assert "endpoint" in tickets_descriptor["configSchema"]["properties"]
    assert tickets_descriptor["policy"]["clientCanEnable"] is True
    assert tickets_descriptor["policy"]["editableFields"] == ["endpoint"]
    assert descriptors["session_tool"]["changeMode"] == "new_session"

    configuration = await agent.ext_method(
        "harness/configuration/get", {"sessionId": response.session_id}
    )
    snapshot = ConfigurationSnapshot.model_validate(configuration)  # strictly the DTO
    assert snapshot.bindings["tickets"].enabled is True
    assert configuration["selectedRevision"] == 1

    with pytest.raises(RequestError) as malformed:
        await new_session(
            agent, tmp_path, initialConfiguration=[{"type": "enable", "bogus": True}]
        )
    assert error_data(malformed.value)["code"] == "INVALID_CONFIGURATION"

    with pytest.raises(RequestError) as unknown_plugin:
        await new_session(
            agent,
            tmp_path,
            initialConfiguration=[
                {"type": "add", "bindingId": "ghost", "pluginId": "nope.missing", "enabled": True}
            ],
        )
    assert error_data(unknown_plugin.value)["code"] in {
        "CAPABILITY_UNAVAILABLE",
        "INVALID_CONFIGURATION",
    }


async def test_configuration_transactions_policy_and_retry_identity(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    session_id = response.session_id

    first = await agent.ext_method(
        "harness/configuration/update",
        {
            "sessionId": session_id,
            "requestId": "enable-1",
            "expectedRevision": 1,
            "changes": [{"type": "enable", "bindingId": "tickets", "enabled": True}],
        },
    )
    assert first["selectedRevision"] == 2

    retry = await agent.ext_method(
        "harness/configuration/update",
        {
            "sessionId": session_id,
            "requestId": "enable-1",
            "expectedRevision": 1,
            "changes": [{"type": "enable", "bindingId": "tickets", "enabled": True}],
        },
    )
    assert retry["selectedRevision"] == 2  # original committed result, not a new revision

    with pytest.raises(RequestError) as reused:
        await agent.ext_method(
            "harness/configuration/update",
            {
                "sessionId": session_id,
                "requestId": "enable-1",
                "expectedRevision": first["selectedRevision"],
                "changes": [{"type": "enable", "bindingId": "tickets", "enabled": False}],
            },
        )
    assert error_data(reused.value)["code"] == "INVALID_CONFIGURATION"

    with pytest.raises(RequestError) as stale:
        await agent.ext_method(
            "harness/configuration/update",
            {
                "sessionId": session_id,
                "requestId": "stale-1",
                "expectedRevision": 1,
                "changes": [{"type": "enable", "bindingId": "tickets", "enabled": False}],
            },
        )
    assert error_data(stale.value)["code"] == "REVISION_CONFLICT"
    assert error_data(stale.value)["details"]["snapshot"]["selectedRevision"] == 2

    with pytest.raises(RequestError) as not_editable:
        await agent.ext_method(
            "harness/configuration/update",
            {
                "sessionId": session_id,
                "requestId": "protected-field",
                "expectedRevision": 2,
                "changes": [
                    {"type": "configure", "bindingId": "tickets", "fields": {"limit": 9}}
                ],
            },
        )
    assert error_data(not_editable.value)["code"] == "POLICY_DENIED"

    configured = await agent.ext_method(
        "harness/configuration/update",
        {
            "sessionId": session_id,
            "requestId": "configure-endpoint",
            "expectedRevision": 2,
            "changes": [
                {
                    "type": "configure",
                    "bindingId": "tickets",
                    "fields": {"endpoint": "https://configured.example"},
                }
            ],
        },
    )
    assert configured["selectedRevision"] == 3
    assert configured["bindings"]["tickets"]["config"]["endpoint"] == "https://configured.example"

    with pytest.raises(RequestError) as locked:
        await agent.ext_method(
            "harness/configuration/update",
            {
                "sessionId": session_id,
                "requestId": "locked-1",
                "expectedRevision": 3,
                "changes": [{"type": "enable", "bindingId": "locked", "enabled": False}],
            },
        )
    assert error_data(locked.value)["code"] == "POLICY_DENIED"

    with pytest.raises(RequestError) as new_session_required:
        await agent.ext_method(
            "harness/configuration/update",
            {
                "sessionId": session_id,
                "requestId": "new-session-1",
                "expectedRevision": 3,
                "changes": [{"type": "enable", "bindingId": "session_tool", "enabled": True}],
            },
        )
    assert error_data(new_session_required.value)["code"] == "NEW_SESSION_REQUIRED"

    # The idempotent no-op is accepted without burning a revision.
    noop = await agent.ext_method(
        "harness/configuration/update",
        {
            "sessionId": session_id,
            "requestId": "noop-1",
            "expectedRevision": 3,
            "changes": [{"type": "enable", "bindingId": "tickets", "enabled": True}],
        },
    )
    assert noop["selectedRevision"] == 3

    # Extension updates also publish the complete changed snapshot.
    changed = [params for method, params in client.notifications
               if method == "harness/configuration/changed"]
    assert changed and changed[-1]["selectedRevision"] == 3

    # Standard ACP option change uses the same controller and notifies when negotiated.
    client.notifications.clear()
    updated = await agent.set_config_option("binding:tickets", session_id, False)
    tickets = next(option for option in updated.config_options if option.id == "binding:tickets")
    assert tickets.current_value is False
    assert [method for method, _ in client.notifications] == ["harness/configuration/changed"]
    assert client.notifications[-1][1]["selectedRevision"] == 4


async def test_capability_list_uses_one_canonical_envelope_for_every_client(
    tmp_path: Path,
) -> None:
    agent, _ = await make_host(tmp_path, client_name="harness-client")
    response = await new_session(agent, tmp_path)

    listing = await agent.ext_method(
        "harness/capabilities/list", {"sessionId": response.session_id}
    )
    assert "tickets" in [entry["bindingId"] for entry in listing["items"]]
    assert "bindings" not in listing
    BindingListResponse.model_validate(listing)

    standard, _ = await make_host(tmp_path / "standard", client_name="third-party")
    standard_response = await new_session(standard, tmp_path / "standard")
    standard_listing = await standard.ext_method(
        "harness/capabilities/list", {"sessionId": standard_response.session_id}
    )
    assert "bindings" not in standard_listing
    BindingListResponse.model_validate(standard_listing)


async def test_extension_validation_uses_project_error_code(tmp_path: Path) -> None:
    agent, _ = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    with pytest.raises(RequestError) as bad_limit:
        await agent.ext_method(
            "harness/capabilities/list",
            {"sessionId": response.session_id, "limit": 101},
        )
    assert error_data(bad_limit.value)["code"] == "INVALID_CONFIGURATION"

    with pytest.raises(RequestError) as empty_changes:
        await agent.ext_method(
            "harness/configuration/update",
            {
                "sessionId": response.session_id,
                "requestId": "empty-1",
                "expectedRevision": 1,
                "changes": [],
            },
        )
    assert error_data(empty_changes.value)["code"] == "INVALID_CONFIGURATION"


async def test_configuration_changed_notification_requires_negotiation(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path, extension_version=2)
    response = await new_session(agent, tmp_path)

    await agent.set_config_option("binding:tickets", response.session_id, True)

    assert client.notifications == []


async def test_load_replay_marks_interrupted_turns(tmp_path: Path) -> None:
    agent, _ = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    store = agent.bundle.manager.store
    await store.append_transcript(
        response.session_id,
        "interrupted-turn",
        "turn_interrupted",
        {"code": "TURN_INTERRUPTED", "message": "host stopped mid-turn"},
    )
    await agent.close_session(response.session_id)

    reloaded, client = await make_host(tmp_path, client_name="third-party")
    await reloaded.load_session(cwd=str(tmp_path), session_id=response.session_id)
    text = "".join(
        str(getattr(update.content, "text", ""))
        for update in client.updates_of("agent_message_chunk")
    )
    assert "Interrupted turn" in text
    assert "host stopped mid-turn" in text


async def test_model_option_requires_offered_model_and_persists(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path, models=("test",))
    response = await new_session(agent, tmp_path)
    session_id = response.session_id

    with pytest.raises(RequestError) as unknown:
        await agent.set_config_option("model", session_id, "not-a-model")
    assert error_data(unknown.value)["code"] == "INVALID_CONFIGURATION"

    updated = await agent.set_config_option("model", session_id, "test")
    model = next(option for option in updated.config_options if option.id == "model")
    assert model.current_value == "test"
    # The extension notification is exactly a ConfigurationSnapshot (no host extras).
    assert "model" not in client.notifications[-1][1]
    assert client.notifications[-1][1]["sessionId"] == session_id

    await agent.close_session(session_id)
    reloaded, reloaded_client = await make_host(tmp_path, models=("test",))
    loaded = await reloaded.load_session(cwd=str(tmp_path / "elsewhere"), session_id=session_id)
    model = next(option for option in loaded.config_options if option.id == "model")
    assert model.current_value == "test"
    assert reloaded_client.updates_of("agent_message_chunk") == []


# ---------------------------------------------------------------------------
# Prompts, tool presentation, usage, permissions, cancellation
# ---------------------------------------------------------------------------


async def test_prompt_streams_text_tool_presentation_and_usage(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)

    result = await agent.prompt(
        response.session_id, [schema.TextContentBlock(type="text", text="read files please")]
    )

    assert result.stop_reason == "end_turn"
    assert result.usage is not None and result.usage.total_tokens > 0
    starts = client.updates_of("tool_call")
    assert len(starts) == 1
    start = starts[0]
    assert start.tool_call_id == "call-read"
    assert start.title == "Read file"
    assert start.kind == "read"
    assert start.raw_input == {"path": "notes.md"}
    assert start.locations[0].path == os.path.realpath(tmp_path / "notes.md")
    progress = client.updates_of("tool_call_update")
    assert [(update.tool_call_id, update.status) for update in progress] == [
        ("call-read", "completed")
    ]
    assert progress[0].raw_output == "contents of notes.md"
    assert client.text() == "tool result: contents of notes.md"
    assert len(client.updates_of("agent_message_chunk")) >= 2


async def test_permissions_allow_reject_cancel_and_remembered_decisions(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    session_id = response.session_id

    allowed = await agent.prompt(
        session_id, [schema.TextContentBlock(type="text", text="record please")]
    )
    assert allowed.stop_reason == "end_turn"
    assert len(client.permissions) == 1
    request = client.permissions[0]
    assert request["tool_call"].tool_call_id == "call-note"
    assert {option.kind for option in request["options"]} == {
        "allow_once",
        "allow_always",
        "reject_once",
        "reject_always",
    }
    assert client.text().endswith("tool result: note(record please)")

    client.updates.clear()
    client.permission_choice = "reject_once"
    rejected = await agent.prompt(
        session_id, [schema.TextContentBlock(type="text", text="record please")]
    )
    assert rejected.stop_reason == "end_turn"
    assert len(client.permissions) == 2
    assert any(
        update.status == "failed" for update in client.updates_of("tool_call_update")
    )
    assert "Rejected by the client" in client.text()

    client.updates.clear()
    client.permission_choice = "allow_always"
    await agent.prompt(session_id, [schema.TextContentBlock(type="text", text="record please")])
    assert len(client.permissions) == 3
    # A remembered "always allow" decision is not asked again for the same scope.
    await agent.prompt(session_id, [schema.TextContentBlock(type="text", text="record please")])
    assert len(client.permissions) == 3

    client.updates.clear()
    client.permission_outcomes = ["cancelled"]
    cancelled = await agent.prompt(
        session_id, [schema.TextContentBlock(type="text", text="record cancel")]
    )
    assert cancelled.stop_reason == "cancelled"
    assert len(client.permissions) == 4
    assert any(
        update.status == "failed" for update in client.updates_of("tool_call_update")
    )
    # The deferred segment is settled with denials, so the session remains usable.
    client.updates.clear()
    usable = await agent.prompt(
        session_id, [schema.TextContentBlock(type="text", text="echo still alive")]
    )
    assert usable.stop_reason == "end_turn"
    assert "echo: echo still alive" in client.text()


async def test_cancel_in_flight_turn_marks_outstanding_tools_failed(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    session_id = response.session_id

    task = asyncio.create_task(
        agent.prompt(session_id, [schema.TextContentBlock(type="text", text="please wait now")])
    )
    await wait_until(lambda: bool(client.updates_of("tool_call")))
    await agent.cancel(session_id)
    result = await asyncio.wait_for(task, timeout=5)

    assert result.stop_reason == "cancelled"
    assert any(
        update.status == "failed" for update in client.updates_of("tool_call_update")
    )
    # The runtime drained before answering; a follow-up prompt is accepted.
    client.updates.clear()
    follow_up = await agent.prompt(
        session_id, [schema.TextContentBlock(type="text", text="echo after cancel")]
    )
    assert follow_up.stop_reason == "end_turn"
    assert "echo: echo after cancel" in client.text()


async def test_configuration_change_waits_for_approval_continuation(tmp_path: Path) -> None:
    """One ACP prompt is one logical turn: config commits only after it finishes."""
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    session_id = response.session_id
    gate = asyncio.Event()
    client.permission_gate = gate

    prompt_task = asyncio.create_task(
        agent.prompt(session_id, [schema.TextContentBlock(type="text", text="record please")])
    )
    await wait_until(lambda: len(client.permissions) == 1)
    revision = (
        await agent.ext_method("harness/configuration/get", {"sessionId": session_id})
    )["selectedRevision"]

    update_task = asyncio.create_task(
        agent.ext_method(
            "harness/configuration/update",
            {
                "sessionId": session_id,
                "requestId": "during-approval",
                "expectedRevision": revision,
                "changes": [{"type": "enable", "bindingId": "tickets", "enabled": True}],
            },
        )
    )
    await asyncio.sleep(0.1)
    assert not update_task.done(), "a configuration transaction must wait for the prompt"
    during = await agent.ext_method(
        "harness/configuration/get", {"sessionId": session_id}
    )
    assert during["selectedRevision"] == revision

    gate.set()
    result = await asyncio.wait_for(prompt_task, timeout=5)
    assert result.stop_reason == "end_turn"
    committed = await asyncio.wait_for(update_task, timeout=5)
    assert committed["selectedRevision"] == revision + 1
    assert committed["bindings"]["tickets"]["enabled"] is True


async def test_cancel_while_prompt_is_queued_is_not_lost(tmp_path: Path) -> None:
    agent, _client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    session_id = response.session_id
    state = agent._sessions[session_id]

    async with state.turn_lock:  # hold the session so the prompt cannot start
        task = asyncio.create_task(
            agent.prompt(
                session_id, [schema.TextContentBlock(type="text", text="please wait now")]
            )
        )
        await asyncio.sleep(0.05)
        await agent.cancel(session_id)
    result = await asyncio.wait_for(task, timeout=5)
    assert result.stop_reason == "cancelled"


async def test_close_during_turn_cancels_honestly_and_retains_session(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    session_id = response.session_id

    task = asyncio.create_task(
        agent.prompt(session_id, [schema.TextContentBlock(type="text", text="please wait now")])
    )
    await wait_until(lambda: bool(client.updates_of("tool_call")))
    await agent.close_session(session_id)

    result = await asyncio.wait_for(task, timeout=5)
    assert result.stop_reason == "cancelled"
    assert any(
        update.status == "failed" for update in client.updates_of("tool_call_update")
    )
    # Closing retains authoritative history; the session can be loaded again.
    reloaded, reloaded_client = await make_host(tmp_path)
    assert (await reloaded.load_session(cwd=str(tmp_path), session_id=session_id)) is not None
    assert reloaded._sessions[session_id].cwd == os.path.realpath(tmp_path)


# ---------------------------------------------------------------------------
# Local plugin catalog / add
# ---------------------------------------------------------------------------


async def test_plugins_list_and_local_add_are_local_only(tmp_path: Path) -> None:
    agent, _ = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    session_id = response.session_id

    plugins = await agent.ext_method("harness/plugins/list", {})
    PluginListResponse.model_validate(plugins)  # canonical DTO shape
    plugin_ids = {plugin["pluginId"] for plugin in plugins["items"]}
    assert plugin_ids >= {
        "demo.tickets",
        "demo.locked",
        "demo.session_tool",
        "demo.extra",
    }

    added = await agent.ext_method(
        "harness/configuration/update",
        {
            "sessionId": session_id,
            "requestId": "add-extra",
            "expectedRevision": 1,
            "changes": [
                {
                    "type": "add",
                    "bindingId": "extra1",
                    "pluginId": "demo.extra",
                    "config": {},
                    "enabled": True,
                }
            ],
        },
    )
    assert added["selectedRevision"] == 2
    assert added["bindings"]["extra1"]["enabled"] is True
    listing = await agent.ext_method(
        "harness/capabilities/list", {"sessionId": session_id}
    )
    added_descriptor = next(
        entry for entry in listing["items"] if entry["bindingId"] == "extra1"
    )
    assert added_descriptor["pluginId"] == "demo.extra"
    assert added_descriptor["policy"]["clientCanEnable"] is True

    work = tmp_path / "hosted-work"
    work.mkdir()
    hosted_agent, _ = await make_host(tmp_path, hosted=True, allowed=(str(work),))
    hosted_response = await new_session(hosted_agent, work)
    with pytest.raises(RequestError) as hidden:
        await hosted_agent.ext_method("harness/plugins/list", {})
    assert hidden.value.code == -32601
    with pytest.raises(RequestError) as forbidden:
        await hosted_agent.ext_method(
            "harness/configuration/update",
            {
                "sessionId": hosted_response.session_id,
                "requestId": "add-hosted",
                "expectedRevision": 1,
                "changes": [
                    {
                        "type": "add",
                        "bindingId": "extra1",
                        "pluginId": "demo.extra",
                        "enabled": True,
                    }
                ],
            },
        )
    assert error_data(forbidden.value)["code"] == "POLICY_DENIED"


# ---------------------------------------------------------------------------
# Reload / list / delete
# ---------------------------------------------------------------------------


async def test_load_preserves_cwd_and_replays_transcript(tmp_path: Path) -> None:
    agent, client = await make_host(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    response = await new_session(agent, work)
    session_id = response.session_id
    await agent.prompt(
        session_id, [schema.TextContentBlock(type="text", text="read files please")]
    )
    live_text = client.text()
    live_tool_starts = [
        (update.tool_call_id, update.status) for update in client.updates_of("tool_call")
    ]
    assert live_text

    listed = await agent.list_sessions()
    assert [(entry.session_id, entry.cwd) for entry in listed.sessions] == [
        (session_id, os.path.realpath(work))
    ]
    filtered = await agent.list_sessions(cwd=str(work))
    assert [entry.session_id for entry in filtered.sessions] == [session_id]
    other_cwd = tmp_path / "not-there"
    other_cwd.mkdir()
    assert (await agent.list_sessions(cwd=str(other_cwd))).sessions == []

    await agent.close_session(session_id)

    reloaded_agent, reloaded = await make_host(tmp_path)
    loaded = await reloaded_agent.load_session(
        cwd=str(tmp_path / "unrelated"), session_id=session_id
    )
    assert loaded is not None
    assert reloaded_agent._sessions[session_id].cwd == os.path.realpath(work)
    assert reloaded.text() == live_text
    replayed_tool_starts = [
        (update.tool_call_id, update.status) for update in reloaded.updates_of("tool_call")
    ]
    assert replayed_tool_starts == live_tool_starts
    assert reloaded.updates_of("tool_call_update")

    # Conversation history is native and continues after reload.
    reloaded.updates.clear()
    continued = await reloaded_agent.prompt(
        session_id, [schema.TextContentBlock(type="text", text="echo after reload")]
    )
    assert continued.stop_reason == "end_turn"
    assert "echo: echo after reload" in reloaded.text()

    await reloaded_agent.delete_session(session_id)
    assert (await reloaded_agent.list_sessions()).sessions == []
    with pytest.raises(RequestError) as missing:
        await reloaded_agent.load_session(cwd=str(work), session_id=session_id)
    assert missing.value.data["code"] == "POLICY_DENIED"


async def test_load_uses_recorded_cwd_and_hosted_policy(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    agent, _ = await make_host(tmp_path, hosted=True, allowed=(str(work),))
    response = await new_session(agent, work)
    session_id = response.session_id
    await agent.close_session(session_id)

    reloaded, _ = await make_host(tmp_path, hosted=True, allowed=(str(work),))
    loaded = await reloaded.load_session(cwd="relative-client-cwd", session_id=session_id)
    assert loaded is not None
    assert reloaded._sessions[session_id].cwd == os.path.realpath(work)

    outside = tmp_path / "outside"
    outside.mkdir()
    hostile, _ = await make_host(tmp_path, hosted=True, allowed=(str(outside),))
    with pytest.raises(RequestError) as denied:
        await hostile.load_session(cwd=str(outside), session_id=session_id)
    assert error_data(denied.value)["code"] == "WORKSPACE_UNAVAILABLE"
