"""Client-core tests: profiles, catalog, extension negotiation, headless runs, CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from harness_client import (
    CatalogClient,
    CatalogError,
    HarnessAcpClient,
    HeadlessInteractions,
    Profile,
    Profiles,
    StdioAgentTransport,
    WebSocketAgentTransport,
    secret_value,
)
from harness_client.cli import main
from harness_client.errors import (
    HarnessClientError,
    InteractionRequired,
    SecretMissing,
    TransportError,
)
from harness_client.events import AgentTextChunk, ConfigurationChanged, TurnCompleted
from harness_client.extension import (
    advertisement_meta,
    parse_advertisement,
    parse_capability_list,
    parse_configuration_snapshot,
)
from harness_client.headless import EXIT_INTERACTION_REQUIRED, EXIT_OK, run_headless
from harness_client.interactions import (
    ElicitationRequest,
    PermissionRequest,
    RequireInteractions,
)
from harness_client.interactions import (
    HeadlessInteractions as ExplicitInteractions,
)
from harness_client.profiles import profiles_from_toml, toml_from_profiles
from harness_protocol import ConfigurationSnapshot
from test_client_support import INDEPENDENT_AGENT, relay_websocket_server, write_extension_agent

REPO_ROOT = Path(__file__).resolve().parents[1]


def stdio_transport(*args: str, name: str = "test") -> StdioAgentTransport:
    return StdioAgentTransport(sys.executable, list(args), name=name)


# --------------------------------------------------------------------- profiles


def test_profiles_roundtrip_toml(tmp_path: Path):
    profile = Profile(
        name="team",
        transport="server",
        url="https://acp.example.com",
        agent="research-assistant",
        token="env:TEAM_TOKEN",
        remote_cwd="/workspace",
        notes="team deployment",
    )
    local = Profile(
        name="local dev",
        transport="stdio",
        command="/opt/venv/bin/harness-host",
        args=["my_agents.dev:definition"],
        cwd="/Users/dev/project",
        env={"DEBUG": "1", "TOKEN": "keyring:harness/dev"},
        default_for_server=True,
    )
    text = toml_from_profiles({"team": profile, "local dev": local}, {"theme": "harness"})
    parsed, rest = profiles_from_toml(text)
    assert parsed["team"].url == "https://acp.example.com"
    assert parsed["team"].remote_cwd == "/workspace"
    assert parsed["local dev"].args == ["my_agents.dev:definition"]
    assert parsed["local dev"].env == {"DEBUG": "1", "TOKEN": "keyring:harness/dev"}
    assert parsed["local dev"].default_for_server is True
    assert rest["tui"] == {"theme": "harness"}


def test_profiles_reject_unknown_keys_and_bad_transport():
    with pytest.raises(ValueError, match="unknown keys"):
        profiles_from_toml('[profile.x]\ntransport = "stdio"\ncommand = "a"\nmystery = 1\n')
    with pytest.raises(ValueError, match="transport"):
        profiles_from_toml('[profile.x]\ntransport = "carrier-pigeon"\n')


def test_profiles_load_uses_sources_and_creates_demo(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HARNESS_PROFILES", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    saved = tmp_path / "custom.toml"
    saved.write_text(
        '[profile.work]\ntransport = "stdio"\ncommand = "/bin/agent"\nargs = ["--acp"]\n',
        encoding="utf-8",
    )
    profiles = Profiles.load(extra_files=[saved])
    assert profiles.names == ["work"]
    assert profiles.sources == [saved]
    assert profiles.get("work").command == "/bin/agent"


def test_secret_references(monkeypatch):
    monkeypatch.setenv("CLIENT_TEST_SECRET", "hidden")
    assert secret_value("env:CLIENT_TEST_SECRET") == "hidden"
    assert secret_value("literal-token") == "literal-token"
    with pytest.raises(SecretMissing, match="environment variable"):
        secret_value("env:CLIENT_TEST_MISSING")
    with pytest.raises(SecretMissing, match="no environment variable"):
        secret_value("env:")


# ---------------------------------------------------------------------- catalog


def catalog_client(handler, **kwargs) -> CatalogClient:
    return CatalogClient(
        "https://catalog.example",
        token="catalog-token",
        http_transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_catalog_pagination_and_bearer_header():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer catalog-token"
        if request.url.path == "/v1/agents":
            assert request.url.params["limit"] == "25"
            cursor = request.url.params.get("cursor")
            if cursor is None:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "agentId": "research-1.0.0",
                                "name": "Research",
                                "version": "1.0.0",
                                "recommended": True,
                                "defaultWorkingDirectory": "/workspace",
                                "workspaces": ["/workspace", "/shared"],
                            }
                        ],
                        "nextCursor": "next",
                    },
                )
            return httpx.Response(200, json={"items": [], "nextCursor": None})
        if request.url.path == "/v1/agents/research-1.0.0":
            return httpx.Response(200, json={"agentId": "research-1.0.0", "name": "Research"})
        return httpx.Response(404)

    async with catalog_client(handler) as catalog:
        page = await catalog.list_agents(limit=25)
        assert page.items[0].label == "Research"
        assert page.items[0].recommended is True
        assert page.items[0].default_working_directory == "/workspace"
        assert page.next_cursor == "next"
        page2 = await catalog.list_agents(cursor=page.next_cursor, limit=25)
        assert page2.items == ()
        detail = await catalog.get_agent("research-1.0.0")
        assert detail.name == "Research"
        assert catalog.websocket_url("research-1.0.0") == (
            "wss://catalog.example/v1/agents/research-1.0.0/acp"
        )
    assert len(seen) == 3


async def test_catalog_error_shapes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"code": "FORBIDDEN", "message": "Nope", "details": {"agentId": "x"}}},
        )

    async with catalog_client(handler) as catalog:
        with pytest.raises(CatalogError) as excinfo:
            await catalog.get_agent("x")
    assert excinfo.value.status == 403
    assert excinfo.value.code == "FORBIDDEN"


# -------------------------------------------------------------------- extension


def test_extension_advertisement_parse_and_meta():
    meta = advertisement_meta()
    assert meta["harness"]["version"] == 1
    assert "_harness/configuration/update" in meta["harness"]["methods"]
    parsed = parse_advertisement(meta)
    assert parsed.enabled and parsed.supports("_harness/configuration/get")
    assert not parsed.local_binding_creation
    full = parse_advertisement(
        {"harness": {"version": 1, "methods": ["configuration/get"], "localBindingCreation": True}}
    )
    assert full.supports("_configuration/get")
    assert not full.supports("_harness/configuration/update")
    assert full.local_binding_creation
    assert not parse_advertisement({"harness": {"version": 2}}).enabled
    assert not parse_advertisement(None).enabled
    assert not parse_advertisement({"harness": "garbage"}).enabled


def test_capability_and_snapshot_payloads():
    listing = parse_capability_list(
        {
            "bindings": [
                {
                    "bindingId": "web",
                    "policy": {"available": True, "clientCanDisable": True, "changeMode": "next_turn"},
                }
            ],
            "selectedRevision": 4,
            "activeRevision": 3,
            "policyRevision": 2,
        }
    )
    assert listing.selected_revision == 4
    assert listing.active_revision == 3
    assert listing.bindings[0].binding_id == "web"
    with pytest.raises(Exception):
        parse_capability_list({"bindings": "not-a-list"})

    snapshot = parse_configuration_snapshot(
        {
            "schemaVersion": 1,
            "sessionId": "s",
            "selectedRevision": 5,
            "activeRevision": 4,
            "policyRevision": 1,
            "applicationStatus": "pending",
            "bindings": {"web": {"enabled": True, "config": {"max": 3}}},
        }
    )
    assert isinstance(snapshot, ConfigurationSnapshot)
    assert snapshot.bindings["web"].config == {"max": 3}
    assert "schemaVersion" not in snapshot.to_wire() or snapshot.to_wire()["schemaVersion"] == 1


# ------------------------------------------------------------------ interactions


async def test_required_policy_raises_and_explicit_policies_choose():
    request = PermissionRequest(
        session_id="s",
        tool_call_id="t",
        title="Run tests",
        kind="execute",
        status="pending",
        options=(
            {"optionId": "a", "name": "Allow", "kind": "allow_once"},
            {"optionId": "r", "name": "Reject", "kind": "reject_once"},
        ),
    )
    with pytest.raises(InteractionRequired, match="permission"):
        await RequireInteractions().choose_permission(request)
    allow = await ExplicitInteractions("allow").choose_permission(request)
    deny = await ExplicitInteractions("deny").choose_permission(request)
    assert allow.option_id == "a" and deny.option_id == "r"
    with pytest.raises(InteractionRequired):
        await ExplicitInteractions("required").answer_elicitation(
            ElicitationRequest(kind="form", message="Name?")
        )
    answer = await ExplicitInteractions("allow", answers={"name": "Ada"}).answer_elicitation(
        ElicitationRequest(
            kind="form",
            message="Name?",
            schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "count": {"type": "integer", "default": 2}},
                "required": ["name"],
            },
        )
    )
    assert answer.action == "accept"
    assert answer.content == {"name": "Ada", "count": 2}


# -------------------------------------------------------------------- headless


async def test_headless_run_against_independent_peer(tmp_path: Path, capsys):
    events = []
    client = HarnessAcpClient(stdio_transport(str(INDEPENDENT_AGENT)))
    client.bus.subscribe(events.append)
    outcome = await run_headless(
        client,
        prompt="tool hello",
        cwd=str(tmp_path),
        json_output=True,
    )
    await client.disconnect()
    assert outcome.ok
    assert outcome.exit_code == EXIT_OK
    assert outcome.stop_reason == "end_turn"
    assert outcome.text == "Independent reply: tool hello"
    assert outcome.tools and outcome.tools[0]["toolCallId"] == "fixture-tool"
    assert any(isinstance(event, AgentTextChunk) for event in events)
    assert any(isinstance(event, TurnCompleted) for event in events)
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True
    assert printed["sessionId"] == outcome.session_id


async def test_headless_interaction_required_retains_session(tmp_path: Path):
    client = HarnessAcpClient(stdio_transport(str(INDEPENDENT_AGENT)))
    outcome = await run_headless(
        client, prompt="permission please", cwd=str(tmp_path), json_output=True
    )
    await client.disconnect()
    assert not outcome.ok
    assert outcome.exit_code == EXIT_INTERACTION_REQUIRED
    assert outcome.error["code"] == "interaction-required"
    assert outcome.session_id is not None


async def test_headless_allow_and_deny_choose_agent_options(tmp_path: Path):
    allow_client = HarnessAcpClient(
        stdio_transport(str(INDEPENDENT_AGENT)), policy=HeadlessInteractions("allow")
    )
    allow = await run_headless(
        allow_client, prompt="permission please", cwd=str(tmp_path), json_output=True
    )
    await allow_client.disconnect()
    assert allow.ok and allow.text == "Approved"

    deny_client = HarnessAcpClient(
        stdio_transport(str(INDEPENDENT_AGENT)), policy=HeadlessInteractions("deny")
    )
    deny = await run_headless(
        deny_client, prompt="permission please", cwd=str(tmp_path), json_output=True
    )
    await deny_client.disconnect()
    assert deny.ok and deny.text == "Rejected"


async def test_extension_agent_capabilities_and_configuration(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    client = HarnessAcpClient(stdio_transport(str(script)))
    await client.connect()
    assert client.extension.enabled
    assert client.extension.local_binding_creation
    assert client.extension.supports("_harness/plugins/list")
    session = await client.new_session(str(tmp_path))
    listing = await session.list_capabilities()
    assert {binding.binding_id for binding in listing.bindings} == {"web", "shell", "policy.guard"}
    snapshot = await session.configuration()
    assert snapshot.selected_revision == 1

    changed_event: list[ConfigurationChanged] = []
    client.bus.subscribe(lambda event: changed_event.append(event) if isinstance(event, ConfigurationChanged) else None)
    updated = await session.update_configuration(
        [{"type": "enable", "bindingId": "shell", "enabled": True}], expected_revision=1
    )
    assert updated.selected_revision == 2
    assert updated.bindings["shell"].enabled is True
    await client.drain_events()
    assert changed_event and changed_event[-1].snapshot.session_id == session.session_id

    # A stale revision is reported with the host's stable error code.
    from harness_client.errors import HarnessClientError

    with pytest.raises(HarnessClientError, match="REVISION_CONFLICT"):
        await session.update_configuration(
            [{"type": "enable", "bindingId": "shell", "enabled": False}], expected_revision=1
        )

    plugins = await session.list_plugins()
    assert plugins.plugins and plugins.plugins[0].plugin_id == "example.custom"

    sessions = await client.list_sessions()
    assert sessions.sessions
    await client.delete_session(session.session_id)
    after = await client.list_sessions()
    assert all(item["sessionId"] != session.session_id for item in after.sessions)
    await client.disconnect()


async def test_standard_config_option_change(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    client = HarnessAcpClient(stdio_transport(str(script)))
    await client.connect()
    session = await client.new_session(str(tmp_path))
    options = await session.set_config_option("model", "test-deep")
    model = next(item for item in options if item["id"] == "model")
    assert model["currentValue"] == "test-deep"
    options = await session.set_config_option("stream", False)
    stream = next(item for item in options if item["id"] == "stream")
    assert stream["currentValue"] is False
    await client.disconnect()


async def test_elicitation_over_stdio(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    answers = {}

    class AnswerPolicy:
        async def choose_permission(self, request):
            from harness_client.interactions import PermissionDecision

            return PermissionDecision(option_id="allow-ext")

        async def answer_elicitation(self, request):
            from harness_client.interactions import ElicitationAnswer

            answers["schema"] = request.schema
            return ElicitationAnswer(action="accept", content={"name": "Ada", "count": 3})

    client = HarnessAcpClient(stdio_transport(str(script)), policy=AnswerPolicy())
    await client.connect()
    outcome = await run_headless(
        client, prompt="elicit something", cwd=str(tmp_path), json_output=True, connect=False
    )
    await client.disconnect()
    assert outcome.ok
    assert "elicited name=Ada count=3" in outcome.text
    assert answers["schema"]["properties"]["name"]["type"] == "string"


async def test_websocket_transport_end_to_end(tmp_path: Path):
    with_script = INDEPENDENT_AGENT
    async with relay_websocket_server(with_script) as server:
        from harness_client import WebSocketAgentTransport

        transport = WebSocketAgentTransport(server.url, token="test-token")
        client = HarnessAcpClient(lambda: transport)
        await client.connect()
        assert client.connected
        session = await client.new_session(str(tmp_path))
        texts = []
        client.bus.subscribe(
            lambda event: texts.append(event.text) if isinstance(event, AgentTextChunk) else None
        )
        response = await client.prompt(session.session_id, "over the wire")
        await client.drain_events()
        assert response.stop_reason == "end_turn"
        assert "".join(texts) == "Independent reply: over the wire"
        await client.disconnect()
        assert any(
            any(key.lower() == "authorization" for key in headers) for headers in server.requests
        )
        assert all(
            any(
                key.lower() == "sec-websocket-protocol" and value == "harness.acp.v1"
                for key, value in headers.items()
            )
            for headers in server.requests
        )


async def test_websocket_rejects_wrong_subprotocol(tmp_path: Path):
    from harness_client import WebSocketAgentTransport
    from harness_client.errors import ProtocolError

    async with relay_websocket_server(INDEPENDENT_AGENT, subprotocol=None) as server:
        transport = WebSocketAgentTransport(server.url, token="test-token")
        client = HarnessAcpClient(lambda: transport)
        with pytest.raises(ProtocolError, match="subprotocol"):
            await client.connect()
        await client.disconnect()


async def test_websocket_auth_failure_reports_token(tmp_path: Path):
    from harness_client import WebSocketAgentTransport
    from harness_client.errors import TransportError

    async with relay_websocket_server(INDEPENDENT_AGENT, token="correct") as server:
        transport = WebSocketAgentTransport(server.url, token="wrong")
        client = HarnessAcpClient(lambda: transport)
        with pytest.raises(TransportError):
            await client.connect()
        await client.disconnect()


async def test_oversized_prompt_is_rejected_before_sending(tmp_path: Path):
    client = HarnessAcpClient(stdio_transport(str(INDEPENDENT_AGENT)))
    await client.connect()
    session = await client.new_session(str(tmp_path))
    from harness_client.errors import TransportError

    with pytest.raises(TransportError, match="16"):
        await client.prompt(session.session_id, "x" * (17 * 1024 * 1024))
    await client.disconnect()


# ------------------------------------------------------------------------- CLI


def test_cli_run_json_against_fixture(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--stdio",
            "--json",
            "--",
            sys.executable,
            str(INDEPENDENT_AGENT),
            "--",
            "tool cli",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert "Independent reply: tool cli" in payload["text"]


def test_cli_interaction_required_exit_code(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--stdio",
            "--json",
            "--",
            sys.executable,
            str(INDEPENDENT_AGENT),
            "--",
            "permission please",
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_INTERACTION_REQUIRED
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "interaction-required"


def test_cli_allow_all_flag(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--stdio",
            "--json",
            "--allow-all",
            "--",
            sys.executable,
            str(INDEPENDENT_AGENT),
            "--",
            "permission please",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["text"] == "Approved"


def test_cli_agents_list_uses_profile(tmp_path: Path, capsys, monkeypatch):
    profile_path = tmp_path / "profiles.toml"
    profile_path.write_text(
        '[profile.team]\ntransport = "server"\nurl = "https://catalog.example"\n'
        'agent = "research"\ntoken = "literal-test-token"\n',
        encoding="utf-8",
    )
    # httpx.MockTransport cannot be injected through the CLI, so point the profile at a
    # local server implemented with a tiny ASGI app via uvicorn if available.
    pytest.importorskip("uvicorn")
    pytest.importorskip("starlette")

    import threading
    import time

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def agents(request):
        assert request.headers.get("authorization") == "Bearer literal-test-token"
        return JSONResponse(
            {
                "items": [
                    {
                        "agentId": "research-1",
                        "name": "Research",
                        "version": "1.0.0",
                        "recommended": True,
                        "defaultWorkingDirectory": "/workspace",
                    }
                ],
                "nextCursor": None,
            }
        )

    app = Starlette(routes=[Route("/v1/agents", agents)])
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    profile_path.write_text(
        f'[profile.team]\ntransport = "server"\nurl = "http://127.0.0.1:{port}"\n'
        'agent = "research"\ntoken = "literal-test-token"\n',
        encoding="utf-8",
    )
    code = main(["--profiles", str(profile_path), "agents", "list", "--server", "team"])
    captured = capsys.readouterr()
    server.should_exit = True
    thread.join(timeout=5)
    assert code == 0, captured.err
    assert "research-1" in captured.out
    assert "Research *" in captured.out


async def test_profile_auth_method_is_negotiated_and_unknown_method_fails(tmp_path):
    script = write_extension_agent(tmp_path)
    client = HarnessAcpClient(
        StdioAgentTransport(sys.executable, [str(script)]),
        auth_method="fixture-login",
    )
    try:
        await client.connect()
        assert [item["id"] for item in client.auth_candidates()] == ["fixture-login"]
    finally:
        await client.disconnect()

    wrong = HarnessAcpClient(
        StdioAgentTransport(sys.executable, [str(script)]),
        auth_method="not-advertised",
    )
    with pytest.raises(HarnessClientError, match="does not advertise auth method"):
        await wrong.connect()
    assert not wrong.connected


def test_plaintext_remote_websocket_is_rejected_but_loopback_dev_is_allowed():
    with pytest.raises(TransportError, match="wss://"):
        WebSocketAgentTransport("ws://agents.example.com/v1/agents/x/acp", token="secret")
    # Loopback development may use plain ws; the token never leaves the machine.
    transport = WebSocketAgentTransport("ws://127.0.0.1:8080/v1/agents/x/acp", token="dev")
    assert transport.url.startswith("ws://127.0.0.1")
    assert WebSocketAgentTransport("wss://agents.example.com/x").url.startswith("wss://")


def test_cli_unknown_profile_is_actionable(capsys):
    code = main(["--profiles", "/nonexistent/profiles.toml", "agents", "list", "--server", "nope"])
    captured = capsys.readouterr()
    assert code != 0
    assert "No profile named 'nope'" in captured.err
