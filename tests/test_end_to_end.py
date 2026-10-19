"""Cross-package checks: an ordinary ACP SDK client drives the real native host."""

import asyncio
import hashlib
import json
import os
import sys

from acp import schema, spawn_agent_process


class StandardClient:
    def __init__(self):
        self.updates = []
        self.message = asyncio.Event()

    def on_connect(self, connection):
        self.connection = connection

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)
        if update.session_update == "agent_message_chunk":
            self.message.set()

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        return schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))


async def test_native_host_standard_client_and_restart(tmp_path):
    store = tmp_path / "sessions.sqlite3"
    command = ("-m", "harness_runtime.host", "harness_runtime.examples:demo_definition", "--store", str(store))
    client = StandardClient()
    async with spawn_agent_process(client, sys.executable, *command) as (connection, _):
        hello = await asyncio.wait_for(connection.initialize(protocol_version=1), 15)
        assert hello.protocol_version == 1
        assert hello.agent_capabilities.load_session
        session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
        result = await asyncio.wait_for(connection.prompt(
            session_id=session.session_id,
            prompt=[schema.TextContentBlock(type="text", text="Describe your capabilities briefly.")],
        ), 15)
        assert result.stop_reason == "end_turn"
        await asyncio.wait_for(client.message.wait(), 3)
        assert any(event.content.text for event in client.updates
                   if event.session_update == "agent_message_chunk")

    # A new interpreter must restore the host's canonical transcript, not a client cache.
    restored = StandardClient()
    async with spawn_agent_process(restored, sys.executable, *command) as (connection, _):
        await asyncio.wait_for(connection.initialize(protocol_version=1), 15)
        await asyncio.wait_for(connection.load_session(
            session_id=session.session_id, cwd=str(tmp_path), mcp_servers=[]), 15)
        await asyncio.wait_for(restored.message.wait(), 3)
        assert any(event.session_update == "agent_message_chunk" for event in restored.updates)


async def test_headless_demo_accepts_a_positional_prompt(tmp_path):
    env = {
        **os.environ,
        "HARNESS_STORE": str(tmp_path / "cli.sqlite3"),
        "PATH": os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")]),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "harness_client", "run", "--demo", "--json", "Hello from the CLI",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    result = json.loads(stdout)
    assert result["ok"] is True
    assert result["text"]


async def test_headless_attachment_is_explicit_content(tmp_path):
    env = {
        **os.environ,
        "HARNESS_STORE": str(tmp_path / "attachments.sqlite3"),
        "PATH": os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")]),
    }
    attachment = tmp_path / "context.md"
    attachment.write_text("# Attached context\n\nA bounded file.", encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "harness_client", "run", "--demo", "--json",
        "--attach", str(attachment), "Use the attached context",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    result = json.loads(stdout)
    assert result["ok"] is True
    assert result["attachments"] == [{
        "name": "context.md",
        "path": str(attachment.resolve()),
        "size": len("# Attached context\n\nA bounded file."),
        "mimeType": "text/markdown",
    }]


async def test_server_catalog_supplies_client_remote_workspace(tmp_path):
    import httpx
    from harness_client.catalog import CatalogClient
    from harness_server import create_app

    app = create_app({
        "server": {"require_tls": False, "store_path": str(tmp_path / "store")},
        "deployments": [{"id": "demo.v1", "name": "Demo", "version": "1",
                        "command": [sys.executable], "allowed_cwds": [str(tmp_path)],
                        "default_cwd": str(tmp_path)}],
        "principals": [{"id": "alice", "token_hashes": [hashlib.sha256(b"token").hexdigest()],
                       "allowed_agent_ids": ["demo.v1"]}],
    })
    async with CatalogClient("http://127.0.0.1", token="token",
                             http_transport=httpx.ASGITransport(app=app)) as catalog:
        descriptor = await catalog.get_agent("demo.v1")
        assert descriptor.default_working_directory == str(tmp_path)
        assert descriptor.workspaces == (str(tmp_path),)
