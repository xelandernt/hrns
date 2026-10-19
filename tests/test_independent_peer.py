"""Prove the standard-only fixture itself conforms to the pinned official SDK."""

import asyncio
import sys
from pathlib import Path

from acp import schema, spawn_agent_process

PEER = Path(__file__).parent / "fixtures" / "independent_acp_agent.py"


class RecordingClient:
    def __init__(self):
        self.updates = []
        self.permissions = []
        self.received = asyncio.Queue()

    def on_connect(self, connection):
        self.connection = connection

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)
        self.received.put_nowait(update)

    async def wait_updates(self, count):
        for _ in range(count):
            await asyncio.wait_for(self.received.get(), timeout=3)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.permissions.append(options)
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id="reject-this")
        )


async def test_independent_standard_peer_streams_and_loads(tmp_path):
    client = RecordingClient()
    async with spawn_agent_process(client, sys.executable, str(PEER)) as (connection, _):
        hello = await connection.initialize(protocol_version=1)
        assert hello.agent_capabilities.load_session
        assert not hello.agent_capabilities.field_meta
        session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
        result = await connection.prompt(session_id=session.session_id,
                                         prompt=[schema.TextContentBlock(type="text", text="tool hello")])
        assert result.stop_reason == "end_turn"
        await client.wait_updates(4)
        types = [event.session_update for event in client.updates]
        assert "tool_call" in types
        assert "tool_call_update" in types
        chunks = [event.content.text for event in client.updates if event.session_update == "agent_message_chunk"]
        assert "".join(chunks) == "Independent reply: tool hello"
        client.updates.clear()
        await connection.load_session(session_id=session.session_id, cwd=str(tmp_path), mcp_servers=[])
        await client.wait_updates(1)
        assert client.updates[0].content.text == "Independent reply: tool hello"


async def test_independent_permission_uses_agent_options(tmp_path):
    client = RecordingClient()
    async with spawn_agent_process(client, sys.executable, str(PEER)) as (connection, _):
        await connection.initialize(protocol_version=1)
        session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
        await connection.prompt(session_id=session.session_id,
                                prompt=[schema.TextContentBlock(type="text", text="permission")])
        await client.wait_updates(1)
        assert [option.option_id for option in client.permissions[0]] == ["allow-this", "reject-this"]
        assert client.updates[-1].content.text == "Rejected"
