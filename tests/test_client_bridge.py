"""Stdio bridge tests: framing, remote-cwd rewrite and failure reporting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from harness_client.bridge import (
    EXIT_BRIDGE_FAILURE,
    EXIT_BRIDGE_OK,
    EXIT_BRIDGE_PROTOCOL,
    rewrite_session_cwd,
    run_bridge_pump,
)
from test_client_support import relay_websocket_server, write_extension_agent
from websockets.asyncio.client import connect


class FakeLocalPeer:
    """A minimal stdio ACP client driven by asyncio queues."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict] = asyncio.Queue()
        self._buffer = b""

    async def read_line(self) -> bytes:
        return await self.inbox.get()

    async def write(self, data: bytes) -> None:
        self._buffer += data
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            if line.strip():
                self.outbox.put_nowait(json.loads(line))

    def send(self, message: dict) -> None:
        self.inbox.put_nowait(json.dumps(message).encode("utf-8") + b"\n")

    async def receive(self, request_id: int, timeout: float = 5.0) -> dict:
        while True:
            message = await asyncio.wait_for(self.outbox.get(), timeout=timeout)
            if message.get("id") == request_id:
                return message


def test_remote_cwd_rewrite_targets_only_session_setup():
    for method in ("session/new", "session/load", "session/resume"):
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"cwd": "/local/project"}}
        assert rewrite_session_cwd(message, "/workspace") is True
        assert message["params"]["cwd"] == "/workspace"
    untouched = {"jsonrpc": "2.0", "method": "session/prompt", "params": {"cwd": "/local"}}
    assert rewrite_session_cwd(untouched, "/workspace") is False
    assert untouched["params"]["cwd"] == "/local"


async def _run_pump(server, local: FakeLocalPeer, remote_cwd: str | None, report_lines: list[str]):
    async def connect_remote():
        return await connect(server.url, subprotocols=["harness.acp.v1"])

    task = asyncio.create_task(
        run_bridge_pump(
            local.read_line,
            local.write,
            connect_remote,
            remote_cwd=remote_cwd,
            stderr=report_lines.append,
        )
    )
    return task


async def test_bridge_preserves_acp_and_rewrites_remote_cwd(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    async with relay_websocket_server(script, token=None) as server:
        local = FakeLocalPeer()
        reports: list[str] = []
        task = await _run_pump(server, local, "/workspace", reports)
        local.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        initialized = await local.receive(1)
        assert initialized["result"]["protocolVersion"] == 1

        local.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "/local/project", "mcpServers": []},
            }
        )
        created = await local.receive(2)
        session_id = created["result"]["sessionId"]

        local.send({"jsonrpc": "2.0", "id": 3, "method": "session/list", "params": {}})
        listing = await local.receive(3)
        assert listing["result"]["sessions"][0]["cwd"] == "/workspace"

        local.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/prompt",
                "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "hello"}]},
            }
        )
        prompted = await local.receive(4)
        assert prompted["result"]["stopReason"] == "end_turn"

        local.inbox.put_nowait(b"")
        assert await asyncio.wait_for(task, timeout=5) == EXIT_BRIDGE_OK
        assert any("rewritten" in line for line in reports)


async def test_bridge_passes_cwd_through_without_option(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    async with relay_websocket_server(script, token=None) as server:
        local = FakeLocalPeer()
        task = await _run_pump(server, local, None, [])
        local.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        await local.receive(1)
        local.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "/local/project", "mcpServers": []},
            }
        )
        await local.receive(2)
        local.send({"jsonrpc": "2.0", "id": 3, "method": "session/list", "params": {}})
        listing = await local.receive(3)
        assert listing["result"]["sessions"][0]["cwd"] == "/local/project"
        local.inbox.put_nowait(b"")
        assert await asyncio.wait_for(task, timeout=5) == EXIT_BRIDGE_OK


async def test_bridge_rejects_malformed_local_frames(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    async with relay_websocket_server(script, token=None) as server:
        local = FakeLocalPeer()
        reports: list[str] = []
        task = await _run_pump(server, local, None, reports)
        local.inbox.put_nowait(b"{not json}\n")
        assert await asyncio.wait_for(task, timeout=5) == EXIT_BRIDGE_PROTOCOL
        assert any("invalid local ACP frame" in line for line in reports)


async def test_bridge_reports_unreachable_remote(tmp_path: Path):
    local = FakeLocalPeer()
    reports: list[str] = []

    async def connect_remote():
        raise OSError("connection refused")

    code = await run_bridge_pump(
        local.read_line, local.write, connect_remote, stderr=reports.append
    )
    assert code == EXIT_BRIDGE_FAILURE
    assert any("cannot connect" in line for line in reports)
