"""Client-side CONCEPT §10/§11/§12/§14/§16 verification regressions.

These tests target protocol edges that the broader UI/core suites do not
exercise: extension negotiation strictness, pagination bounds, connection-loss
state, UTF-8 frame sizing, WebSocket framing, headless recovery and bridge
profile behavior.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from harness_client import (
    HarnessAcpClient,
    HeadlessInteractions,
    Profile,
    StdioAgentTransport,
    TransportError,
    WebSocketAgentTransport,
)
from harness_client import client as client_module
from harness_client.attachments import build_prompt_blocks
from harness_client.bus import EventBus
from harness_client.cli import _bridge_remote_cwd
from harness_client.errors import (
    ConnectionLost,
    HarnessClientError,
    InteractionRejected,
    ProtocolError,
)
from harness_client.extension import HarnessExtension, parse_advertisement
from harness_client.headless import run_headless
from harness_client.interactions import PermissionRequest
from test_client_support import EXTENSION_AGENT_SOURCE, INDEPENDENT_AGENT, write_extension_agent
from websockets.asyncio.server import serve


def stdio_transport(script: Path, *, name: str = "spec") -> StdioAgentTransport:
    return StdioAgentTransport(sys.executable, [str(script)], name=name)


# --------------------------------------------------------- extension §10.2


def test_version_marker_without_methods_grants_nothing():
    advertisement = parse_advertisement({"harness": {"version": 1}})
    assert advertisement.enabled
    assert advertisement.methods == frozenset()
    assert not advertisement.supports("_harness/configuration/update")
    assert not advertisement.supports("_harness/capabilities/list")


def test_methods_without_extension_version_are_not_negotiated():
    advertisement = parse_advertisement(
        {"harness": {"methods": ["_harness/configuration/update"]}}
    )
    assert advertisement.enabled is False
    assert not advertisement.supports("_harness/configuration/update")


async def test_extension_pagination_rejects_out_of_range_limits(tmp_path: Path):
    client = HarnessAcpClient(stdio_transport(write_extension_agent(tmp_path)))
    await client.connect()
    try:
        with pytest.raises(HarnessClientError, match="between 1 and 100"):
            await client.harness_capabilities("session", limit=0)
        with pytest.raises(HarnessClientError, match="between 1 and 100"):
            await client.harness_plugins(limit=101)
    finally:
        await client.disconnect()


async def test_add_requires_advertised_local_binding_creation(tmp_path: Path):
    client = HarnessAcpClient(stdio_transport(write_extension_agent(tmp_path)))
    await client.connect()
    session = await client.new_session(str(tmp_path))
    client.extension = HarnessExtension(
        enabled=True,
        version=1,
        methods=frozenset(
            {
                "_harness/capabilities/list",
                "_harness/configuration/get",
                "_harness/configuration/update",
            }
        ),
        local_binding_creation=False,
    )
    changes = [
        {
            "type": "add",
            "bindingId": "new.search",
            "pluginId": "example.custom",
            "config": {},
            "enabled": True,
        }
    ]
    with pytest.raises(HarnessClientError, match="local binding creation"):
        await session.update_configuration(changes, expected_revision=1)
    with pytest.raises(HarnessClientError, match="local binding creation"):
        await client.new_session(str(tmp_path), initial_configuration=changes)
    await client.disconnect()


# ----------------------------------------------------------- errors §10.3


async def test_close_does_not_silently_degrade_to_cancel(tmp_path: Path):
    client = HarnessAcpClient(stdio_transport(INDEPENDENT_AGENT))
    await client.connect()
    try:
        session = await client.new_session(str(tmp_path))
        with pytest.raises(ProtocolError, match="session/close"):
            await client.close_session(session.session_id)
    finally:
        await client.disconnect()


async def test_delete_application_error_is_mapped(tmp_path: Path):
    client = HarnessAcpClient(stdio_transport(write_extension_agent(tmp_path)))
    await client.connect()
    try:
        with pytest.raises(HarnessClientError, match="Resource not found"):
            await client.delete_session("missing-session")
    finally:
        await client.disconnect()


# ----------------------------------------------------- messages/UTF-8 §11.2


async def test_prompt_limit_counts_serialized_utf8_bytes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(client_module, "_TOTAL_PROMPT_BYTES", 400)
    client = HarnessAcpClient(stdio_transport(write_extension_agent(tmp_path)))
    await client.connect()
    try:
        session = await client.new_session(str(tmp_path))
        # 100 characters, but ASCII-escaped JSON is > 600 bytes and the envelope
        # is larger still; a character-count check would let this through.
        with pytest.raises(TransportError, match="at most"):
            await client.prompt(session.session_id, "é" * 100)
    finally:
        await client.disconnect()


async def test_websocket_rejects_batched_frames():
    async def handler(websocket):
        request = json.loads(await websocket.recv())
        response = {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {},
                "agentInfo": {"name": "batch", "version": "1"},
                "authMethods": [],
            },
        }
        extra = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "second object"},
                },
            },
        }
        await websocket.send(json.dumps(response) + "\n" + json.dumps(extra))
        await websocket.wait_closed()

    async with serve(handler, "127.0.0.1", 0, subprotocols=["harness.acp.v1"]) as server:
        port = server.sockets[0].getsockname()[1]
        transport = WebSocketAgentTransport(f"ws://127.0.0.1:{port}/v1/agents/x/acp")
        client = HarnessAcpClient(lambda: transport, connect_timeout=3)
        with pytest.raises(TransportError):
            await client.connect()
        await client.disconnect()


# -------------------------------------------------- connection loss §14.2


async def test_connection_loss_is_visible_and_reconnectable(tmp_path: Path):
    transports: list[StdioAgentTransport] = []

    def factory() -> StdioAgentTransport:
        transport = stdio_transport(INDEPENDENT_AGENT, name=f"spec-{len(transports)}")
        transports.append(transport)
        return transport

    client = HarnessAcpClient(factory, connect_timeout=5)
    await client.connect()
    session = await client.new_session(str(tmp_path))
    with pytest.raises(ConnectionLost):
        await client.prompt(session.session_id, "disconnect")
    await asyncio.wait_for(client.wait_disconnected(), timeout=5)
    assert client.connection_state == "lost"
    assert client.connected is False

    await client.connect()
    assert client.connected is True
    assert len(transports) == 2
    replacement = await client.new_session(str(tmp_path))
    response = await client.prompt(replacement.session_id, "after reconnect")
    assert response.stop_reason == "end_turn"
    await client.disconnect()


async def test_headless_load_replays_history_without_including_it_in_output(
    tmp_path: Path, capsys
):
    client = HarnessAcpClient(stdio_transport(write_extension_agent(tmp_path)))
    await client.connect()
    try:
        session = await client.new_session(str(tmp_path))
        await client.prompt(session.session_id, "first turn")
        await client.drain_events()
        outcome = await run_headless(
            client,
            prompt="second turn",
            cwd=str(tmp_path),
            session_id=session.session_id,
            json_output=True,
            connect=False,
        )
    finally:
        await client.disconnect()
    capsys.readouterr()
    assert outcome.ok is True
    assert outcome.text == "ext reply: second turn"
    assert "first turn" not in outcome.text


async def test_load_failure_never_falls_back_to_resume(tmp_path: Path, capsys):
    client = HarnessAcpClient(stdio_transport(INDEPENDENT_AGENT))
    await client.connect()
    resume_calls: list[str] = []

    async def fail_load(*args, **kwargs):
        raise HarnessClientError("authoritative history is unavailable")

    async def unexpected_resume(*args, **kwargs):
        resume_calls.append("called")
        raise AssertionError("resume must not be used without a verified cache")

    client.load_session = fail_load  # type: ignore[method-assign]
    client.resume_session = unexpected_resume  # type: ignore[method-assign]
    try:
        outcome = await run_headless(
            client,
            prompt="continue",
            cwd=str(tmp_path),
            session_id="saved-session",
            json_output=True,
            connect=False,
        )
    finally:
        await client.disconnect()
    capsys.readouterr()
    assert outcome.ok is False
    assert outcome.error is not None and outcome.error["code"] == "run-failed"
    assert resume_calls == []


# -------------------------------------------------- headless and files §12


async def test_attachment_resource_requires_advertised_embedded_context(tmp_path: Path):
    source = EXTENSION_AGENT_SOURCE.replace(
        '"promptCapabilities": {"image": True, "audio": False, "embeddedContext": False},',
        '"promptCapabilities": {},',
    )
    assert '"embeddedContext": False' not in source
    script = tmp_path / "no_embedded_context.py"
    script.write_text(source, encoding="utf-8")
    attached = tmp_path / "notes.md"
    attached.write_text("context", encoding="utf-8")

    client = HarnessAcpClient(stdio_transport(script))
    await client.connect()
    try:
        session = await client.new_session(str(tmp_path))
        with pytest.raises(ProtocolError, match="embedded context"):
            await session.prompt_blocks(
                build_prompt_blocks("read this", [str(attached)]), display_text="read this"
            )
        # Plain text remains usable on the same peer.
        response = await session.prompt("plain text")
        assert response.stop_reason == "end_turn"
    finally:
        await client.disconnect()


async def test_attachment_validation_precedes_connection(tmp_path: Path, capsys):
    client = HarnessAcpClient(stdio_transport(INDEPENDENT_AGENT))
    outcome = await run_headless(
        client,
        prompt="review",
        cwd=str(tmp_path),
        attachments=[str(tmp_path / "missing.txt")],
        json_output=True,
    )
    await client.disconnect()
    capsys.readouterr()
    assert outcome.ok is False
    assert outcome.session_id is None
    assert outcome.error is not None


async def test_headless_permission_never_crosses_option_kinds():
    only_reject = PermissionRequest(
        session_id="s",
        tool_call_id="t",
        title="Risky call",
        kind="execute",
        status="pending",
        options=({"optionId": "r", "name": "Reject", "kind": "reject_once"},),
    )
    with pytest.raises(InteractionRejected, match="no allow option"):
        await HeadlessInteractions("allow").choose_permission(only_reject)

    only_allow = PermissionRequest(
        session_id="s",
        tool_call_id="t",
        title="Risky call",
        kind="execute",
        status="pending",
        options=({"optionId": "a", "name": "Allow", "kind": "allow_once"},),
    )
    with pytest.raises(InteractionRejected, match="no reject option"):
        await HeadlessInteractions("deny").choose_permission(only_allow)


async def test_timeout_requests_turn_cancellation(capsys):
    class FakeSession:
        session_id = "fake-session"
        cwd = "/workspace"
        cancelled = False

        async def prompt_blocks(self, blocks, *, display_text):
            await asyncio.Event().wait()

        async def cancel(self):
            self.cancelled = True

    class FakeClient:
        connected = True
        bus = EventBus()
        agent_title = "fake"
        agent_version = "0"

        def __init__(self):
            self.session = FakeSession()

        async def new_session(self, cwd):
            return self.session

    fake = FakeClient()
    outcome = await run_headless(fake, prompt="slow", cwd="/workspace", timeout=0.05, json_output=True)
    capsys.readouterr()
    assert outcome.ok is False
    assert fake.session.cancelled is True


# ----------------------------------------------------- cwd/workspace §11.5


async def test_loading_validates_cwd_and_advertised_additional_directories(tmp_path: Path):
    client = HarnessAcpClient(stdio_transport(write_extension_agent(tmp_path)))
    await client.connect()
    try:
        with pytest.raises(HarnessClientError, match="absolute path"):
            await client.load_session("unknown", "relative/path")
        with pytest.raises(HarnessClientError, match="absolute path"):
            await client.resume_session("unknown", "relative/path")
        with pytest.raises(HarnessClientError, match="absolute path"):
            await client.list_sessions(cwd="relative/path")
        with pytest.raises(ProtocolError, match="additionalDirectories"):
            await client.new_session(str(tmp_path), additional_directories=[str(tmp_path)])
    finally:
        await client.disconnect()


# ------------------------------------------------------ bridge profile §11.4


def test_bridge_remote_cwd_prefers_cli_then_profile():
    profile = Profile(
        name="team",
        transport="server",
        url="https://agents.example",
        agent="research",
        remote_cwd="/workspace",
    )
    assert _bridge_remote_cwd(profile, None) == "/workspace"
    assert _bridge_remote_cwd(profile, "/other") == "/other"
    assert _bridge_remote_cwd(Profile(name="local"), None) is None
