"""Full local product path: authenticated WebSocket -> server -> worker -> native agent."""

import asyncio
import hashlib
import sys

import pytest
import uvicorn
from acp import schema, spawn_agent_process
from harness_client import AgentTextChunk, CatalogClient, HarnessAcpClient, WebSocketAgentTransport
from harness_server import create_app

TOKEN = "hosted-e2e-token"


@pytest.fixture
async def hosted_server(tmp_path):
    config = {
        "server": {
            "host": "127.0.0.1",
            "port": 8080,
            "require_tls": False,
            "store_path": str(tmp_path / "store"),
            "max_worker_count": 4,
        },
        "deployments": [{
            "id": "demo.v1",
            "name": "Offline demo",
            "version": "1.0.0",
            "description": "Hosted deterministic native agent.",
            "command": [sys.executable, "-m", "harness_runtime.host",
                        "harness_runtime.examples:demo_definition"],
            "cwd": str(tmp_path),
            "allowed_cwds": [str(tmp_path)],
            "default_cwd": str(tmp_path),
        }],
        "principals": [{
            "id": "alice",
            "token_hashes": [hashlib.sha256(TOKEN.encode()).hexdigest()],
            "allowed_agent_ids": ["demo.v1"],
        }],
    }
    server = uvicorn.Server(uvicorn.Config(
        create_app(config), host="127.0.0.1", port=0, log_level="warning",
        ws_max_size=16 * 1024 * 1024, ws_ping_interval=30, ws_ping_timeout=30,
    ))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", tmp_path
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)


async def test_hosted_capability_transaction_and_transcript_load(hosted_server):
    base_url, workspace = hosted_server
    events = []
    async with CatalogClient(base_url, token=TOKEN) as catalog:
        page = await catalog.list_agents(limit=50)
        assert [item.agent_id for item in page.items] == ["demo.v1"]
        assert page.items[0].default_working_directory == str(workspace)
        url = catalog.websocket_url("demo.v1")
        assert url.startswith("ws://")

    client = HarnessAcpClient(lambda: WebSocketAgentTransport(url, token=TOKEN))
    client.bus.subscribe(events.append)
    try:
        await client.connect()
        assert client.extension.supports("_harness/configuration/update")
        session = await client.new_session(cwd=str(workspace))
        await session.prompt("Say hello.")
        await client.bus.drain()
        first = "".join(event.text for event in events if isinstance(event, AgentTextChunk))
        assert "tone=concise" in first

        listing = await session.list_capabilities()
        assert [binding.binding_id for binding in listing.bindings] == ["tone"]
        snapshot = await session.configuration()
        changed = await session.update_configuration(
            [{"type": "enable", "bindingId": "tone", "enabled": False}],
            expected_revision=snapshot.selected_revision,
            request_id="hosted-disable-tone",
        )
        assert changed.bindings["tone"].enabled is False
        assert changed.selected_revision == snapshot.selected_revision + 1

        events.clear()
        await session.prompt("Say hello again.")
        await client.bus.drain()
        second = "".join(event.text for event in events if isinstance(event, AgentTextChunk))
        assert "tone=none" in second

        # A retry with the same transaction id and body returns the committed result;
        # it does not create another revision.
        replay = await session.update_configuration(
            [{"type": "enable", "bindingId": "tone", "enabled": False}],
            expected_revision=snapshot.selected_revision,
            request_id="hosted-disable-tone",
        )
        assert replay.selected_revision == changed.selected_revision
        session_id = session.session_id
    finally:
        await client.disconnect()

    # Reconnect from a fresh worker and use authoritative session/load.
    restored_events = []
    restored = HarnessAcpClient(lambda: WebSocketAgentTransport(url, token=TOKEN))
    restored.bus.subscribe(restored_events.append)
    try:
        await restored.connect()
        loaded = await restored.load_session(session_id, str(workspace))
        await restored.bus.drain()
        loaded_snapshot = await loaded.configuration()
        assert loaded_snapshot.bindings["tone"].enabled is False
        replay_text = "".join(
            event.text for event in restored_events if isinstance(event, AgentTextChunk)
        )
        assert "tone=concise" in replay_text and "tone=none" in replay_text
    finally:
        await restored.disconnect()


class BridgeClient:
    def __init__(self):
        self.updates = []

    def on_connect(self, connection):
        self.connection = connection

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        return schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))


async def test_stdio_bridge_reaches_hosted_agent_with_remote_cwd(hosted_server):
    base_url, workspace = hosted_server
    ws_url = base_url.replace("http://", "ws://") + "/v1/agents/demo.v1/acp"
    command = (
        "-m", "harness_client", "bridge",
        "--url", base_url, "--agent", "demo.v1", "--token", TOKEN,
        "--remote-cwd", str(workspace),
    )
    client = BridgeClient()
    async with spawn_agent_process(client, sys.executable, *command) as (connection, _):
        hello = await asyncio.wait_for(connection.initialize(protocol_version=1), 20)
        assert hello.agent_capabilities.load_session
        # The local cwd does not exist on the server; the bridge replaces it with
        # the explicitly chosen remote workspace before forwarding.
        session = await asyncio.wait_for(
            connection.new_session(cwd="/definitely/local/only", mcp_servers=[]), 20
        )
        result = await asyncio.wait_for(connection.prompt(
            session_id=session.session_id,
            prompt=[schema.TextContentBlock(type="text", text="Hello through the bridge")],
        ), 20)
        assert result.stop_reason == "end_turn"
        for _ in range(40):
            if any(getattr(update, "content", None) for update in client.updates):
                break
            await asyncio.sleep(0.05)
        text = "".join(
            update.content.text for update in client.updates
            if getattr(update, "session_update", "") == "agent_message_chunk"
        )
        assert "tone=concise" in text
    assert ws_url.endswith("/acp")
