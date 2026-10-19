"""Final ACP interoperability review probes (independent of candidate_2).

Host-side checks drive the native ``harness-host`` against the deterministic
``test_acp_runtime_double`` deployment and, where the wire contract matters, use the
pinned official ``agent-client-protocol`` 0.11.1 client over stdio. Client-side checks
use the SDK-free fixtures under ``tests/fixtures`` / ``test_client_support``.

Findings verified on candidate_1 (tests named ``test_f*`` assert the published/Acp
contract and therefore fail against the current implementation):

- F1 (critical): cancellation that lands after a permission decision (or close/EOF
  while the dialog is pending) never settles the deferred runtime segment. The next
  prompt — or the next prompt after ``session/load`` — fails with TURN_FAILED because
  the history still contains unprocessed tool calls.
- F2 (critical): ``session/load`` does not replay user messages, although ACP v1
  requires the entire conversation to be replayed to a possibly different client.
- F3 (required): a client with ``advertise_extension=False`` still enables the
  extension whenever the agent advertises one; it then invokes methods it never
  advertised as supported.
- F4 (required): an explicit empty ``methods`` list in the extension advertisement is
  treated as the complete method set.
- F5 (required): a reloaded session keeps a model id that is no longer offered, while
  the standard ``model`` option reports the fallback model; the next turn still uses
  the stale id.
- F6 (required): a binding whose ``availability_reason`` marks it runtime-unavailable
  is still offered as an editable standard config option when ``policy.available`` is
  true.
- F7 (required): a hosted deployment whose allowed root is ``/`` rejects every
  descendant working directory.

The remaining tests are green conformance checks for negotiation errors, hosted
initial configuration, select/boolean option round-trips, cancellation that is
settled correctly, and an independent standard-only peer.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import test_acp_runtime_double as demo
from acp import schema
from acp.exceptions import RequestError
from acp.stdio import spawn_agent_process
from harness_client import (
    AgentTextChunk,
    HarnessAcpClient,
    StdioAgentTransport,
    ToolEvent,
)
from harness_client.extension import parse_advertisement
from harness_protocol import (
    BindingDescriptor,
    CapabilityPolicy,
    ConfigurationSnapshot,
)
from harness_runtime.acp import config_options
from harness_runtime.host import HarnessHostAgent, HostSetup, build_agent
from test_client_support import write_extension_agent

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
INDEPENDENT_AGENT = TESTS_DIR / "fixtures" / "independent_acp_agent.py"

EXTENSION_METHODS = [
    "_harness/capabilities/list",
    "_harness/plugins/list",
    "_harness/configuration/get",
    "_harness/configuration/update",
    "_harness/configuration/changed",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def boolean_capabilities() -> schema.ClientCapabilities:
    return schema.ClientCapabilities(
        session=schema.ClientSessionCapabilities(
            config_options=schema.SessionConfigOptionsCapabilities(
                boolean=schema.BooleanConfigOptionCapabilities()
            )
        )
    )


class RecordingClient:
    """In-process ACP client used by native-host checks."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, schema.SessionUpdate]] = []
        self.permissions: list[dict[str, Any]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.permission_choice = "allow_once"
        self.permission_gate: asyncio.Event | None = None

    async def session_update(
        self, session_id: str, update: schema.SessionUpdate, **kwargs: Any
    ) -> None:
        self.updates.append((session_id, update))

    async def request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        self.permissions.append(
            {
                "session_id": session_id,
                "tool_call": tool_call,
                "options": list(options),
            }
        )
        if self.permission_gate is not None:
            gate, self.permission_gate = self.permission_gate, None
            await gate.wait()
        option = next(
            (item for item in options if item.kind == self.permission_choice), options[0]
        )
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id=option.option_id)
        )

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))

    def updates_of(self, kind: str) -> list[schema.SessionUpdate]:
        return [update for _, update in self.updates if update.session_update == kind]

    def text(self) -> str:
        return "".join(
            chunk.content.text
            for chunk in self.updates_of("agent_message_chunk")
            if getattr(chunk.content, "text", None)
        )


class SdkRecordingClient:
    """Official-SDK client callback used by subprocess tests."""

    def __init__(self) -> None:
        self.updates: list[schema.SessionUpdate] = []

    async def session_update(
        self, session_id: str, update: schema.SessionUpdate, **kwargs: Any
    ) -> None:
        self.updates.append(update)

    async def request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        option = next((item for item in options if item.kind == "allow_once"), options[0])
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id=option.option_id)
        )

    def updates_of(self, kind: str) -> list[schema.SessionUpdate]:
        return [update for update in self.updates if update.session_update == kind]

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
    principal: str = "reviewer",
    deployment: str = "review-dep",
    extension: bool = True,
) -> tuple[HarnessHostAgent, RecordingClient]:
    setup = HostSetup(
        definition="test_acp_runtime_double:demo_definition",
        store_path=str(tmp_path / "runtime.sqlite3"),
        models=models,
        hosted=hosted,
        principal=principal,
        deployment_id=deployment,
        allowed_cwds=allowed,
    )
    bundle = demo.build_host_bundle(setup)
    agent = build_agent(bundle, setup)
    client = RecordingClient()
    agent.on_connect(client)
    await agent.initialize(
        1,
        schema.ClientCapabilities(),
        schema.Implementation(name="review", version="0.0"),
        **( {"harness": {"version": 1, "methods": EXTENSION_METHODS}} if extension else {} ),
    )
    return agent, client


async def new_session(
    agent: HarnessHostAgent, cwd: Path | str, **meta: Any
) -> schema.NewSessionResponse:
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


def subprocess_env() -> dict[str, str]:
    return {
        "PYTHONPATH": os.pathsep.join(
            [str(TESTS_DIR)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
        ),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYDANTIC_AI_NO_BANNER": "1",
        "PYTHONWARNINGS": "ignore",
    }


@asynccontextmanager
async def spawn_host(
    db_path: Path, *, models: tuple[str, ...] = ()
) -> AsyncIterator[tuple[Any, SdkRecordingClient]]:
    """Launch the real host exactly as an ACP client would (official SDK transport)."""
    client = SdkRecordingClient()
    command = [
        sys.executable,
        "-m",
        "harness_runtime.host",
        "test_acp_runtime_double:demo_definition",
        "--runtime-module",
        "test_acp_runtime_double",
        "--log-level",
        "warning",
        "--store",
        str(db_path),
    ]
    if models:
        command += ["--models", ",".join(models)]
    async with spawn_agent_process(
        client,
        *command,
        env=subprocess_env(),
        cwd=str(REPO_ROOT),
        transport_kwargs={"stderr": None},
    ) as (connection, _process):
        yield connection, client


async def initialize(
    connection: Any, *, boolean: bool = False, extension: bool = False
) -> schema.InitializeResponse:
    kwargs: dict[str, Any] = {}
    if extension:
        kwargs["harness"] = {"version": 1, "methods": EXTENSION_METHODS}
    return await connection.initialize(
        1,
        client_capabilities=boolean_capabilities() if boolean else schema.ClientCapabilities(),
        client_info=schema.Implementation(name="review-sdk", version="0.0.1"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Green conformance: pinned official SDK, select/boolean options and complete list
# ---------------------------------------------------------------------------


async def test_pinned_sdk_select_fallback_returns_complete_option_list(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    async with spawn_host(tmp_path / "select.db", models=("test", "echo")) as (
        connection,
        _client,
    ):
        await initialize(connection)
        session = await connection.new_session(cwd=str(work), mcp_servers=[])
        options = {option.id: option for option in session.config_options}

        assert isinstance(options["model"], schema.SessionConfigOptionSelect)
        tickets = options["binding:tickets"]
        assert isinstance(tickets, schema.SessionConfigOptionSelect)
        assert tickets.current_value == "off"
        assert [value.value for value in tickets.options] == ["on", "off"]

        updated = await connection.set_config_option(
            config_id="binding:tickets", session_id=session.session_id, value="on"
        )
        options = {option.id: option for option in updated.config_options}
        # The complete list is returned after a change, model first by priority.
        assert list(options) == ["model", "binding:tickets"]
        assert options["binding:tickets"].current_value == "on"

        model_update = await connection.set_config_option(
            config_id="model", session_id=session.session_id, value="echo"
        )
        options = {option.id: option for option in model_update.config_options}
        assert list(options) == ["model", "binding:tickets"]
        assert options["model"].current_value == "echo"
        assert options["binding:tickets"].current_value == "on"


async def test_pinned_sdk_boolean_option_roundtrip(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    async with spawn_host(tmp_path / "boolean.db") as (connection, _client):
        await initialize(connection, boolean=True)
        session = await connection.new_session(cwd=str(work), mcp_servers=[])
        options = {option.id: option for option in session.config_options}
        assert isinstance(options["binding:tickets"], schema.SessionConfigOptionBoolean)
        assert options["binding:tickets"].current_value is False

        updated = await connection.set_config_option(
            config_id="binding:tickets", session_id=session.session_id, value=True
        )
        options = {option.id: option for option in updated.config_options}
        assert isinstance(options["binding:tickets"], schema.SessionConfigOptionBoolean)
        assert options["binding:tickets"].current_value is True


async def test_pinned_sdk_load_replays_agent_text_and_recorded_cwd(tmp_path: Path) -> None:
    db_path = tmp_path / "reload.db"
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    async with spawn_host(db_path) as (connection, client):
        await initialize(connection)
        session = await connection.new_session(cwd=str(work), mcp_servers=[])
        await connection.prompt(
            session_id=session.session_id,
            prompt=[schema.TextContentBlock(type="text", text="read files please")],
        )
        assert "tool result: contents of notes.md" in client.text()

    async with spawn_host(db_path) as (connection, replayed):
        await initialize(connection)
        loaded = await connection.load_session(
            cwd=str(other), session_id=session.session_id, mcp_servers=[]
        )
        assert loaded is not None
        assert "tool result: contents of notes.md" in replayed.text()
        listed = await connection.list_sessions()
        assert listed.sessions[0].cwd == os.path.realpath(work)
        assert replayed.updates_of("tool_call")


# ---------------------------------------------------------------------------
# Green conformance: initialConfiguration policy and hosted restrictions
# ---------------------------------------------------------------------------


async def test_initial_configuration_respects_directional_policy(tmp_path: Path) -> None:
    agent, _ = await make_host(tmp_path)

    with pytest.raises(RequestError) as denied:
        await new_session(
            agent,
            tmp_path,
            initialConfiguration=[{"type": "enable", "bindingId": "locked", "enabled": False}],
        )
    assert error_data(denied.value)["code"] == "POLICY_DENIED"
    assert agent._sessions == {}, "a rejected initial configuration must not create a session"

    response = await new_session(
        agent,
        tmp_path,
        initialConfiguration=[
            {"type": "enable", "bindingId": "session_tool", "enabled": True}
        ],
    )
    listing = await agent.ext_method(
        "harness/capabilities/list", {"sessionId": response.session_id}
    )
    descriptors = {entry["bindingId"]: entry for entry in listing["items"]}
    assert descriptors["session_tool"]["enabled"] is True


async def test_hosted_initial_add_is_rejected_before_session_creation(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    agent, _ = await make_host(tmp_path, hosted=True, allowed=(str(work),))

    with pytest.raises(RequestError) as forbidden:
        await new_session(
            agent,
            work,
            initialConfiguration=[
                {
                    "type": "add",
                    "bindingId": "extra1",
                    "pluginId": "demo.extra",
                    "enabled": True,
                }
            ],
        )
    assert error_data(forbidden.value)["code"] == "POLICY_DENIED"
    assert agent._sessions == {}


async def test_extension_errors_use_standard_rpc_codes(tmp_path: Path) -> None:
    agent, _ = await make_host(tmp_path)
    with pytest.raises(RequestError) as unknown_method:
        await agent.ext_method("harness/does/not/exist", {})
    assert unknown_method.value.code == -32601

    with pytest.raises(RequestError) as malformed:
        await new_session(
            agent, tmp_path, initialConfiguration=[{"type": "enable", "unknown": True}]
        )
    assert error_data(malformed.value)["code"] == "INVALID_CONFIGURATION"
    assert agent._sessions == {}

    work = tmp_path / "hosted-work"
    work.mkdir()
    hosted, _ = await make_host(tmp_path, hosted=True, allowed=(str(work),))
    with pytest.raises(RequestError) as hidden:
        await hosted.ext_method("harness/plugins/list", {})
    assert hidden.value.code == -32601


# ---------------------------------------------------------------------------
# Green conformance: independent SDK-free peer and cancellation
# ---------------------------------------------------------------------------


async def test_independent_sdk_free_peer_drives_standard_only_client(tmp_path: Path) -> None:
    events: list[Any] = []
    client = HarnessAcpClient(StdioAgentTransport(sys.executable, [str(INDEPENDENT_AGENT)]))
    client.bus.subscribe(events.append)
    try:
        await client.connect()
        assert client.extension.enabled is False
        session = await client.new_session(cwd=str(tmp_path))
        await client.prompt(session.session_id, "tool hello")
        await client.bus.drain()

        text = "".join(
            event.text for event in events if isinstance(event, AgentTextChunk) and not event.thought
        )
        assert text == "Independent reply: tool hello"
        assert any(isinstance(event, ToolEvent) for event in events)
    finally:
        await client.disconnect()


async def test_cancel_while_permission_dialog_open_keeps_session_usable(
    tmp_path: Path,
) -> None:
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    gate = asyncio.Event()
    client.permission_gate = gate

    task = asyncio.create_task(
        agent.prompt(
            response.session_id,
            [schema.TextContentBlock(type="text", text="record please")],
        )
    )
    await wait_until(lambda: len(client.permissions) == 1)
    await agent.cancel(response.session_id)
    cancelled = await asyncio.wait_for(task, timeout=5)
    assert cancelled.stop_reason == "cancelled"
    gate.set()
    await asyncio.sleep(0.05)

    client.updates.clear()
    follow_up = await agent.prompt(
        response.session_id,
        [schema.TextContentBlock(type="text", text="echo after cancel")],
    )
    assert follow_up.stop_reason == "end_turn"
    assert "echo: echo after cancel" in client.text()


# ---------------------------------------------------------------------------
# F1 (critical): cancellation after the permission decision poisons history
# ---------------------------------------------------------------------------


async def test_f1_cancel_immediately_after_permission_decision_keeps_session_usable(
    tmp_path: Path,
) -> None:
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    original = client.request_permission

    async def decide_then_cancel(
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        result = await original(session_id, tool_call, options, **kwargs)
        # The client answered "allow" and then sent session/cancel before the host
        # resumed the deferred segment. This must not leave the approved call
        # unprocessed in the native history.
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.create_task(agent.cancel(session_id))
        )
        return result

    client.request_permission = decide_then_cancel  # type: ignore[method-assign]

    first = await agent.prompt(
        response.session_id,
        [schema.TextContentBlock(type="text", text="record please")],
    )
    assert first.stop_reason == "cancelled"
    await asyncio.sleep(0.05)  # let the queued cancellation task run

    follow_up = await agent.prompt(
        response.session_id,
        [schema.TextContentBlock(type="text", text="echo after late cancel")],
    )
    assert follow_up.stop_reason == "end_turn"
    assert "echo: echo after late cancel" in client.text()


async def test_f1_close_during_permission_dialog_keeps_loaded_session_usable(
    tmp_path: Path,
) -> None:
    agent, client = await make_host(tmp_path)
    response = await new_session(agent, tmp_path)
    gate = asyncio.Event()
    client.permission_gate = gate

    task = asyncio.create_task(
        agent.prompt(
            response.session_id,
            [schema.TextContentBlock(type="text", text="record please")],
        )
    )
    await wait_until(lambda: len(client.permissions) == 1)
    await agent.close_session(response.session_id)
    closed = await asyncio.wait_for(task, timeout=5)
    assert closed.stop_reason == "cancelled"
    gate.set()
    await asyncio.sleep(0.05)

    reloaded_agent, reloaded_client = await make_host(tmp_path)
    await reloaded_agent.load_session(cwd=str(tmp_path), session_id=response.session_id)
    follow_up = await reloaded_agent.prompt(
        response.session_id,
        [schema.TextContentBlock(type="text", text="echo after close during approval")],
    )
    assert follow_up.stop_reason == "end_turn"
    assert "echo after close during approval" in reloaded_client.text()


# ---------------------------------------------------------------------------
# F2 (critical): session/load must replay the entire conversation
# ---------------------------------------------------------------------------


async def test_f2_load_replays_user_messages_to_a_new_client(tmp_path: Path) -> None:
    db_path = tmp_path / "replay-users.db"
    work = tmp_path / "work"
    work.mkdir()

    async with spawn_host(db_path) as (connection, _client):
        await initialize(connection)
        session = await connection.new_session(cwd=str(work), mcp_servers=[])
        await connection.prompt(
            session_id=session.session_id,
            prompt=[schema.TextContentBlock(type="text", text="hello replay")],
        )

    async with spawn_host(db_path) as (connection, replayed):
        await initialize(connection)
        await connection.load_session(
            cwd=str(work), session_id=session.session_id, mcp_servers=[]
        )
        user_chunks = [
            update
            for update in replayed.updates_of("user_message_chunk")
            if getattr(update.content, "text", None)
        ]
        assert [chunk.content.text for chunk in user_chunks] == ["hello replay"], (
            "ACP v1 session/load MUST replay the entire conversation; a new client "
            "has no local copy of the earlier user prompt"
        )


# ---------------------------------------------------------------------------
# F3/F4 (required): extension negotiation must follow mutual advertisement
# ---------------------------------------------------------------------------


async def test_f3_client_does_not_enable_extension_it_did_not_advertise(
    tmp_path: Path,
) -> None:
    script = write_extension_agent(tmp_path)
    client = HarnessAcpClient(
        StdioAgentTransport(sys.executable, [str(script)]),
        advertise_extension=False,
    )
    try:
        await client.connect()
        enabled = client.extension.enabled
        methods = set(client.extension.methods)
    finally:
        await client.disconnect()

    assert client.extension.enabled is False
    assert methods == set()
    assert enabled is False, (
        "advertise_extension=False is standard-only mode, but the client enabled the "
        "agent's extension and can invoke methods it never advertised"
    )


def test_f4_empty_extension_methods_do_not_grant_every_method() -> None:
    advertised = parse_advertisement({"harness": {"version": 1, "methods": []}})
    assert advertised.supports("_harness/capabilities/list") is False
    assert advertised.supports("_harness/configuration/update") is False


# ---------------------------------------------------------------------------
# F5 (required): standard model option must match the effective next-turn model
# ---------------------------------------------------------------------------


async def test_f5_reloaded_model_option_matches_effective_model(tmp_path: Path) -> None:
    first, _ = await make_host(tmp_path, models=("old",))
    response = await new_session(first, tmp_path)
    await first.close_session(response.session_id)

    reloaded, _ = await make_host(tmp_path, models=("new",))
    loaded = await reloaded.load_session(cwd=str(tmp_path), session_id=response.session_id)
    options = {option.id: option for option in loaded.config_options}
    effective = reloaded._sessions[response.session_id].model

    assert options["model"].current_value == "new"
    assert effective == "new", (
        f"configOptions report {options['model'].current_value!r} but the next turn "
        f"would use {effective!r}, which this deployment no longer offers"
    )


# ---------------------------------------------------------------------------
# F6 (required): unavailable bindings are not editable standard options
# ---------------------------------------------------------------------------


def test_f6_unavailable_binding_is_not_offered_as_a_standard_option() -> None:
    descriptor = BindingDescriptor(
        binding_id="broken",
        name="Broken",
        enabled=False,
        availability_reason="upstream compatibility constraints do not hold on this host",
        policy=CapabilityPolicy(
            available=True,
            client_can_enable=True,
            client_can_disable=True,
            change_mode="next_turn",
        ),
        change_mode="next_turn",
    )
    snapshot = ConfigurationSnapshot(session_id="review")
    offered = [option.id for option in config_options(snapshot, [descriptor])]
    assert "binding:broken" not in offered, (
        "the standard list advertises an editable option whose enable/disable path "
        "the runtime rejects as CAPABILITY_UNAVAILABLE"
    )


# ---------------------------------------------------------------------------
# F7 (required): hosted allowed root "/" includes every absolute cwd
# ---------------------------------------------------------------------------


async def test_f7_hosted_root_slash_accepts_descendants(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    agent, _ = await make_host(tmp_path, hosted=True, allowed=("/",))
    response = await new_session(agent, work)
    assert agent._sessions[response.session_id].cwd == os.path.realpath(work)
