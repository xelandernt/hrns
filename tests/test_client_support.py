"""Shared fixtures for client tests.

* The coordinator's SDK-free ``independent_acp_agent.py`` exercises plain ACP.
* ``EXTENSION_AGENT_SOURCE`` is a second SDK-free peer that advertises the
  ``_harness`` extension, standard config options, session list/delete/close/
  resume, permissions and structured elicitation.
* ``relay_websocket_server`` serves a real ``harness.acp.v1`` WebSocket relay
  in front of either fixture so the client's transport is tested end to end.

This module contains no tests; it is imported by ``tests/test_client_*.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from websockets.asyncio.server import Server, ServerConnection, serve

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEPENDENT_AGENT = Path(__file__).parent / "fixtures" / "independent_acp_agent.py"
EXTENSION_VERSION = 1


EXTENSION_AGENT_SOURCE = r'''
"""SDK-free ACP v1 fixture advertising the versioned Harness extension."""
import json
import os
import sys
import uuid

EXTENSION_ENABLED = os.environ.get("FIXTURE_EXTENSION", "1") == "1"
sessions = {}
revisions = {"selected": 1, "active": 1}
request_log = []

def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False), flush=True)

def update(session_id, value):
    send({"method": "session/update", "params": {"sessionId": session_id, "update": value}})

BINDINGS = [
    {
        "schemaVersion": 1, "bindingId": "web", "pluginId": "example.web_search",
        "name": "Web search", "description": "Search the public web for sources.",
        "version": "1.4.0", "enabled": True, "policy": {
            "available": True, "enabledByDefault": True,
            "clientCanEnable": True, "clientCanDisable": True,
            "editableFields": ["max_results"], "changeMode": "next_turn",
        },
        "configSchema": {"type": "object", "properties": {
            "max_results": {"type": "integer", "title": "Max results", "default": 5, "minimum": 1, "maximum": 20},
            "region": {"type": "string", "title": "Region", "enum": ["global", "eu", "us"], "default": "global"},
        }},
        "config": {"max_results": 5, "region": "global"},
        "changeMode": "next_turn", "stateMode": "session",
    },
    {
        "schemaVersion": 1, "bindingId": "shell", "pluginId": "example.shell",
        "name": "Shell", "description": "Run workspace commands.",
        "version": "2.0.1", "enabled": False, "policy": {
            "available": True, "enabledByDefault": False,
            "clientCanEnable": True, "clientCanDisable": True,
            "editableFields": [], "changeMode": "next_turn",
        },
        "config": {}, "changeMode": "next_turn", "stateMode": "run",
    },
    {
        "schemaVersion": 1, "bindingId": "policy.guard", "pluginId": "internal.guardrails",
        "name": "Deployment guardrails", "description": "Locked by host policy.",
        "version": "1.0.0", "enabled": True, "policy": {
            "available": False, "enabledByDefault": True,
            "clientCanEnable": False, "clientCanDisable": False,
            "editableFields": [], "changeMode": "new_session",
        },
        "availabilityReason": "Mandatory capability enforced by the deployment",
        "config": {}, "changeMode": "new_session",
    },
]

CONFIG_OPTIONS = [
    {"id": "model", "name": "Model", "type": "select", "category": "model",
     "description": "Model used for the next turn.",
     "currentValue": "test-fast",
     "options": [
         {"value": "test-fast", "name": "Test Fast", "description": "Small deterministic test model"},
         {"value": "test-deep", "name": "Test Deep", "description": "Larger reasoning test model"},
     ]},
    {"id": "stream", "name": "Stream output", "type": "boolean", "category": "mode",
     "description": "Emit chunks as they are produced.", "currentValue": True},
]


def snapshot(session_id, status="applied"):
    return {
        "schemaVersion": 1, "sessionId": session_id,
        "selectedRevision": revisions["selected"], "activeRevision": revisions["active"],
        "policyRevision": 3, "applicationStatus": status,
        "bindings": {b["bindingId"]: {"enabled": b["enabled"], "config": b.get("config", {})} for b in BINDINGS},
    }


def set_config(current_value):
    options = json.loads(json.dumps(CONFIG_OPTIONS))
    options[0]["currentValue"] = current_value
    return options


def handle_extension(method, params, request_id):
    session_id = params.get("sessionId") or next(iter(sessions), None)
    if method == "_harness/capabilities/list":
        send({"id": request_id, "result": {
            "schemaVersion": 1,
            "bindings": json.loads(json.dumps(BINDINGS)),
            "selectedRevision": revisions["selected"],
            "activeRevision": revisions["active"],
            "policyRevision": 3,
        }})
        return
    if method == "_harness/plugins/list":
        send({"id": request_id, "result": {"items": [{
            "schemaVersion": 1, "pluginId": "example.custom",
            "version": "0.2.0", "description": "Local binding creation example",
            "changeMode": "next_turn", "stateMode": "run", "managed": True,
            "runtimeCompatible": True, "configSchema": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "title": "Label", "default": "custom"},
                },
            },
        }], "nextCursor": None}})
        return
    if method == "_harness/configuration/get":
        send({"id": request_id, "result": snapshot(session_id)})
        return
    if method == "_harness/configuration/update":
        request_log.append(params)
        if params.get("expectedRevision") != revisions["selected"]:
            send({"id": request_id, "error": {"code": -32000, "message": "Revision conflict", "data": {
                "code": "REVISION_CONFLICT",
                "details": {"currentRevision": revisions["selected"]},
            }}})
            return
        for change in params.get("changes", []):
            kind = change.get("type")
            binding_id = change.get("bindingId")
            if kind == "add":
                BINDINGS.append({
                    "schemaVersion": 1,
                    "bindingId": binding_id,
                    "pluginId": change.get("pluginId"),
                    "name": str(binding_id),
                    "description": "Locally added fixture capability.",
                    "version": "0.2.0",
                    "enabled": bool(change.get("enabled", True)),
                    "policy": {
                        "available": True, "enabledByDefault": bool(change.get("enabled", True)),
                        "clientCanEnable": True, "clientCanDisable": True,
                        "editableFields": [], "changeMode": "next_turn",
                    },
                    "config": dict(change.get("config") or {}),
                    "changeMode": "next_turn",
                    "stateMode": "run",
                })
                continue
            target = next((b for b in BINDINGS if b["bindingId"] == binding_id), None)
            if target is None:
                send({"id": request_id, "error": {"code": -32000, "message": "Unknown binding", "data": {
                    "code": "CAPABILITY_UNAVAILABLE", "details": {"bindingId": binding_id}}}})
                return
            if kind == "enable":
                target["enabled"] = bool(change.get("enabled"))
            elif kind == "configure":
                target.setdefault("config", {}).update(change.get("fields") or {})
        revisions["selected"] += 1
        changed = snapshot(session_id, status="pending")
        send({"method": "_harness/configuration/changed", "params": changed})
        send({"id": request_id, "result": changed})
        return
    send({"id": request_id, "error": {"code": -32601, "message": "Method not found"}})


def request_client(request_id, method, params):
    send({"id": request_id, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            raise SystemExit(0)
        message = json.loads(line)
        if message.get("id") == request_id and ("result" in message or "error" in message):
            return message
        if message.get("method") in ("session/cancel",):
            continue


def stream_chunks(session_id, text, message_id):
    for chunk in (text[:len(text) // 2], text[len(text) // 2:]):
        if chunk:
            update(session_id, {"sessionUpdate": "agent_message_chunk", "messageId": message_id,
                              "content": {"type": "text", "text": chunk}})


next_id = [0]
def new_session_id(prefix="ext"):
    next_id[0] += 1
    return f"{prefix}-{next_id[0]}"

for line in sys.stdin:
    try:
        message = json.loads(line)
    except Exception:
        continue
    method = message.get("method")
    params = message.get("params") or {}
    request_id = message.get("id")
    if method is None:
        continue
    if method == "initialize":
        send({"id": request_id, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": {"list": {}, "delete": {}, "close": {}, "resume": {}},
                "promptCapabilities": {"image": True, "audio": False, "embeddedContext": False},
            },
            "agentInfo": {"name": "Extension fixture", "title": "Extension fixture", "version": "1.0.0"},
            "authMethods": [{
                "id": "fixture-login",
                "name": "Fixture login",
                "description": "Deterministic test authentication",
            }],
            "_meta": ({"harness": {
                "version": 1,
                "methods": ["_harness/capabilities/list", "_harness/plugins/list",
                            "_harness/configuration/get", "_harness/configuration/update",
                            "_harness/configuration/changed"],
                "localBindingCreation": True,
            }} if EXTENSION_ENABLED else {}),
        }})
        continue
    if method == "authenticate":
        if params.get("methodId") == "fixture-login":
            send({"id": request_id, "result": {}})
        else:
            send({"id": request_id, "error": {"code": -32602, "message": "Unknown auth method"}})
        continue
    if method == "session/new":
        session_id = new_session_id()
        sessions[session_id] = {"cwd": params.get("cwd"), "messages": [], "title": "Fixture session"}
        revisions["selected"] = max(revisions["selected"], 1)
        send({"id": request_id, "result": {
            "sessionId": session_id,
            "configOptions": set_config("test-fast"),
            "modes": {"currentModeId": "chat", "availableModes": [
                {"id": "chat", "name": "Chat", "description": "Conversational mode"},
                {"id": "plan", "name": "Plan first", "description": "Plan before acting"},
            ]},
        }})
        continue
    if method == "session/load":
        session_id = params.get("sessionId")
        if session_id not in sessions:
            send({"id": request_id, "result": {}})
            continue
        for stored in sessions[session_id]["messages"]:
            update(session_id, {"sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": stored}})
        send({"id": request_id, "result": {"configOptions": set_config("test-fast"),
                                           "modes": {"currentModeId": "chat", "availableModes": [
                                               {"id": "chat", "name": "Chat"},
                                               {"id": "plan", "name": "Plan first"}]}}})
        continue
    if method == "session/resume":
        session_id = params.get("sessionId")
        if session_id not in sessions:
            send({"id": request_id, "error": {"code": -32002, "message": "Resource not found"}})
        else:
            send({"id": request_id, "result": {"configOptions": set_config("test-fast")}})
        continue
    if method == "session/list":
        items = [{"sessionId": sid, "cwd": data["cwd"] or "/tmp", "title": data["title"],
                  "updatedAt": "2026-10-17T12:00:00Z"} for sid, data in sessions.items()]
        send({"id": request_id, "result": {"sessions": items}})
        continue
    if method == "session/delete":
        session_id = params.get("sessionId")
        if session_id in sessions:
            sessions.pop(session_id)
            send({"id": request_id, "result": {}})
        else:
            send({"id": request_id, "error": {"code": -32002, "message": "Resource not found"}})
        continue
    if method == "session/close":
        send({"id": request_id, "result": {}})
        continue
    if method == "session/set_mode":
        send({"id": request_id, "result": {}})
        continue
    if method == "session/set_config_option":
        value = params.get("value")
        if value is True or value is False:
            options = set_config("test-fast")
            options[1]["currentValue"] = value
        else:
            options = set_config(str(value))
        send({"id": request_id, "result": {"configOptions": options}})
        continue
    if method == "session/cancel":
        continue
    if method == "session/prompt":
        session_id = params.get("sessionId")
        text = " ".join(block.get("text", "") for block in params.get("prompt") or [] if block.get("type") == "text")
        revisions["active"] = revisions["selected"]
        message_id = uuid.uuid4().hex
        if "permission" in text:
            rid = new_session_id("perm")
            response = request_client(rid, "session/request_permission", {
                "sessionId": session_id,
                "toolCall": {"toolCallId": "ext-op", "title": "Fixture operation", "kind": "other", "status": "pending"},
                "options": [
                    {"optionId": "allow-ext", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject-ext", "name": "Reject", "kind": "reject_once"},
                ],
            })
            outcome = (response.get("result") or {}).get("outcome") or {}
            verdict = "approved" if outcome.get("optionId") == "allow-ext" else "not approved"
            stream_chunks(session_id, f"fixture {verdict}", message_id)
            sessions[session_id]["messages"].append(f"fixture {verdict}")
            send({"id": request_id, "result": {"stopReason": "end_turn"}})
            continue
        if "elicit" in text:
            rid = new_session_id("elicit")
            response = request_client(rid, "elicitation/create", {
                "mode": "form", "message": "What name should the fixture use?",
                "sessionId": session_id, "toolCallId": "ext-elicit",
                "requestedSchema": {"type": "object", "properties": {
                    "name": {"type": "string", "title": "Name", "description": "Display name"},
                    "count": {"type": "integer", "title": "Count", "default": 2},
                }, "required": ["name"]},
            })
            content = (response.get("result") or {}).get("content") or {}
            stream_chunks(session_id, f"elicited name={content.get('name')} count={content.get('count')}", message_id)
            sessions[session_id]["messages"].append("elicited")
            send({"id": request_id, "result": {"stopReason": "end_turn"}})
            continue
        if "plan" in text:
            update(session_id, {"sessionUpdate": "plan", "entries": [
                {"content": "Inspect the workspace", "priority": "high", "status": "completed"},
                {"content": "Apply the change", "priority": "medium", "status": "in_progress"},
            ]})
            update(session_id, {"sessionUpdate": "available_commands_update", "availableCommands": [
                {"name": "research", "description": "Research a topic in depth"},
                {"name": "summarize", "description": "Summarize the current context"},
                {"name": "help", "description": "Agent-side help on collision"},
            ]})
            update(session_id, {"sessionUpdate": "agent_thought_chunk",
                                "content": {"type": "text", "text": "Considering the plan."}})
        if "tool" in text:
            update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "ext-tool",
                                "title": "Fetch fixture data", "kind": "fetch", "status": "in_progress",
                                "rawInput": {"query": "fixture"}})
            update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "ext-tool",
                                "status": "completed",
                                "content": [{"type": "content", "content": {"type": "text", "text": "fixture data loaded"}}]})
        response_text = f"ext reply: {text}"
        stream_chunks(session_id, response_text, message_id)
        sessions[session_id]["messages"].append(response_text)
        send({"id": request_id, "result": {"stopReason": "end_turn", "usage": {
            "totalTokens": 42, "inputTokens": 30, "outputTokens": 12}}})
        continue
    if method.startswith("_harness/"):
        handle_extension(method, params, request_id)
        continue
    if request_id is not None:
        send({"id": request_id, "error": {"code": -32601, "message": "Method not found"}})
'''


def write_extension_agent(tmp_path: Path) -> Path:
    path = tmp_path / "harness_extension_agent.py"
    path.write_text(EXTENSION_AGENT_SOURCE, encoding="utf-8")
    return path


class RelayServer:
    """A real WebSocket server relaying harness.acp.v1 frames to a fixture subprocess."""

    def __init__(self, script: Path, *, token: str | None = "test-token", subprotocol: str | None = "harness.acp.v1") -> None:
        self.script = script
        self.token = token
        self.subprotocol = subprotocol
        self.server: Server | None = None
        self.port = 0
        self.requests: list[dict[str, str]] = []
        self.error: BaseException | None = None

    async def _handle(self, websocket: ServerConnection) -> None:
        self.requests.append(dict(websocket.request.headers))
        if self.token is not None:
            header = websocket.request.headers.get("Authorization", "")
            if header != f"Bearer {self.token}":
                await websocket.close(code=4401, reason="unauthorized")
                return
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self.script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stdin is not None

        async def client_to_agent() -> None:
            try:
                async for message in websocket:
                    if isinstance(message, bytes):
                        await websocket.close(code=4400, reason="binary unsupported")
                        return
                    process.stdin.write(message.rstrip().encode("utf-8") + b"\n")
                    await process.stdin.drain()
            except Exception as exc:
                self.error = exc
            finally:
                with contextlib.suppress(Exception):
                    process.stdin.close()

        async def agent_to_client() -> None:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                await websocket.send(line.decode("utf-8").rstrip("\n"))
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)

        tasks = [
            asyncio.create_task(client_to_agent()),
            asyncio.create_task(agent_to_client()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=2)
            if process.returncode is None:
                with contextlib.suppress(Exception):
                    process.kill()

    async def __aenter__(self) -> RelayServer:
        self.server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            subprotocols=[self.subprotocol] if self.subprotocol else None,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/agents/ext-fixture/acp"


@contextlib.asynccontextmanager
async def relay_websocket_server(
    script: Path, *, token: str | None = "test-token", subprotocol: str | None = "harness.acp.v1"
) -> AsyncIterator[RelayServer]:
    async with RelayServer(script, token=token, subprotocol=subprotocol) as server:
        yield server


def json_lines(messages: list[dict]) -> list[str]:
    return [json.dumps(message) for message in messages]
