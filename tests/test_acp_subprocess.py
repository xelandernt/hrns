"""Full official-SDK stdio roundtrip against the ``harness-host`` subprocess.

The host is launched exactly as a client would launch it (``python -m
harness_runtime.host`` with ``--runtime-module test_acp_runtime_double``), so these
tests cover process separation, JSON-RPC framing, initialize/new/prompt/update/cancel/
load/list/delete, the ``_harness/*`` extension over the wire, permissions, and
transcript replay across a restart. No provider credentials are used: the demo agent
runs on Pydantic AI ``FunctionModel``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from acp import schema
from acp.stdio import spawn_agent_process

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

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


class WireClient:
    """An ACP client that records updates and answers permissions deterministically."""

    def __init__(self) -> None:
        self.updates: list[schema.SessionUpdate] = []
        self.permissions: list[dict[str, Any]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.permission_choice = "allow_once"

    async def session_update(self, session_id: str, update: schema.SessionUpdate, **kwargs: Any) -> None:
        self.updates.append(update)

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
        option = next(
            (item for item in options if item.kind == self.permission_choice), options[0]
        )
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id=option.option_id)
        )

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))

    # --- helpers -------------------------------------------------------

    def updates_of(self, kind: str) -> list[schema.SessionUpdate]:
        return [update for update in self.updates if update.session_update == kind]

    def text(self) -> str:
        return "".join(
            chunk.content.text
            for chunk in self.updates_of("agent_message_chunk")
            if getattr(chunk.content, "text", None)
        )


def subprocess_env() -> dict[str, str]:
    env = {
        "PYTHONPATH": os.pathsep.join(
            [str(TESTS_DIR)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
        ),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYDANTIC_AI_NO_BANNER": "1",
        "PYTHONWARNINGS": "ignore",
    }
    return env


@asynccontextmanager
async def spawn_host(
    db_path: Path, *, hosted: bool = False, allowed: tuple[str, ...] | None = None
) -> AsyncIterator[tuple[Any, WireClient, Any]]:
    client = WireClient()
    command = [
        sys.executable,
        "-m",
        "harness_runtime.host",
        "test_acp_runtime_double:demo_definition",
        "--runtime-module",
        "test_acp_runtime_double",
        "--log-level",
        "warning",
    ]
    env = subprocess_env()
    if hosted:
        work = allowed[0] if allowed else str(db_path.parent)
        command += ["--hosted"]
        env.update(
            {
                "HARNESS_HOSTED": "1",
                "HARNESS_PRINCIPAL": "hosted-user",
                "HARNESS_DEPLOYMENT_ID": "dep-hosted",
                "HARNESS_STORE": str(db_path),
                "HARNESS_ALLOWED_CWDS": json.dumps(list(allowed or [work])),
            }
        )
    else:
        command += ["--store", str(db_path)]
    async with spawn_agent_process(
        client,
        *command,
        env=env,
        cwd=str(REPO_ROOT),
        transport_kwargs={"stderr": None},
    ) as (connection, process):
        yield connection, client, process


async def initialize(connection: Any) -> schema.InitializeResponse:
    return await connection.initialize(
        1,
        client_capabilities=boolean_capabilities(),
        client_info=schema.Implementation(name="pytest-wire", version="0.0.1"),
        harness={"version": 1, "methods": EXTENSION_METHODS},
    )


async def wait_until(predicate: Any, *, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not reached in time")


async def test_stdio_roundtrip_permissions_configuration_and_reload(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite3"
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    canonical_work = os.path.realpath(work)

    async with spawn_host(db_path) as (connection, client, _process):
        init = await initialize(connection)
        assert init.protocol_version == 1
        assert init.agent_capabilities.load_session is True
        advertisement = init.field_meta["harness"]
        assert advertisement["version"] == 1
        assert "_harness/capabilities/list" in advertisement["methods"]

        session = await connection.new_session(
            cwd=str(work),
            harness={
                "initialConfiguration": [
                    {"type": "enable", "bindingId": "tickets", "enabled": True}
                ]
            },
        )
        session_id = session.session_id
        options = {option.id: option for option in session.config_options}
        assert options["binding:tickets"].current_value is True

        listing = await connection.ext_method(
            "harness/capabilities/list", {"sessionId": session_id}
        )
        assert [entry["bindingId"] for entry in listing["items"]] == [
            "tickets",
            "locked",
            "session_tool",
        ]

        read_result = await connection.prompt(
            prompt=[schema.TextContentBlock(type="text", text="read files please")],
            session_id=session_id,
        )
        assert read_result.stop_reason == "end_turn"
        assert read_result.usage is not None and read_result.usage.total_tokens > 0
        assert "tool result: contents of notes.md" in client.text()
        starts = client.updates_of("tool_call")
        assert starts and starts[0].kind == "read"

        record_result = await connection.prompt(
            prompt=[schema.TextContentBlock(type="text", text="record please")],
            session_id=session_id,
        )
        assert record_result.stop_reason == "end_turn"
        assert len(client.permissions) == 1
        assert {option.kind for option in client.permissions[0]["options"]} == {
            "allow_once",
            "allow_always",
            "reject_once",
            "reject_always",
        }
        live_text = client.text()
        assert "tool result: note(record please)" in live_text

        update = await connection.ext_method(
            "harness/configuration/update",
            {
                "sessionId": session_id,
                "requestId": "wire-toggle",
                "expectedRevision": listing["selectedRevision"],
                "changes": [{"type": "enable", "bindingId": "tickets", "enabled": False}],
            },
        )
        assert update["selectedRevision"] == listing["selectedRevision"] + 1
        assert update["bindings"]["tickets"]["enabled"] is False

        tool_calls_before = len(client.updates_of("tool_call"))
        cancel_task = asyncio.create_task(
            connection.prompt(
                prompt=[schema.TextContentBlock(type="text", text="please wait now")],
                session_id=session_id,
            )
        )
        await wait_until(lambda: len(client.updates_of("tool_call")) > tool_calls_before)
        await connection.cancel(session_id)
        cancelled = await asyncio.wait_for(cancel_task, timeout=10)
        assert cancelled.stop_reason == "cancelled"

        follow_up = await connection.prompt(
            prompt=[schema.TextContentBlock(type="text", text="echo after wire cancel")],
            session_id=session_id,
        )
        assert follow_up.stop_reason == "end_turn"
        assert "echo: echo after wire cancel" in client.text()

        listed = await connection.list_sessions()
        assert [entry.session_id for entry in listed.sessions] == [session_id]
        assert listed.sessions[0].cwd == canonical_work

    async with spawn_host(db_path) as (connection, replayed, _process):
        await initialize(connection)
        loaded = await connection.load_session(cwd=str(other), session_id=session_id)
        assert loaded is not None
        # Transcript replay arrives during load, before the response.
        assert replayed.text()
        assert live_text in replayed.text()
        assert {update.tool_call_id for update in replayed.updates_of("tool_call")} >= {
            "call-read",
            "call-note",
        }
        listed = await connection.list_sessions()
        assert listed.sessions[0].session_id == session_id
        assert listed.sessions[0].cwd == canonical_work  # stored cwd wins over `other`

        continued = await connection.prompt(
            prompt=[schema.TextContentBlock(type="text", text="echo after reload")],
            session_id=session_id,
        )
        assert continued.stop_reason == "end_turn"
        assert "echo: echo after reload" in replayed.text()

        # The SDK client has no typed session/delete helper yet; the host registers the
        # standard route explicitly, so exercise it on the underlying connection.
        deleted = await connection._conn.send_request("session/delete", {"sessionId": session_id})
        assert deleted == {}
        remaining = await connection.list_sessions()
        assert remaining.sessions == []


async def test_hosted_worker_rejects_client_mcp_and_out_of_root_cwds(tmp_path: Path) -> None:
    db_path = tmp_path / "hosted.sqlite3"
    work = tmp_path / "hosted-work"
    work.mkdir()
    outside = tmp_path / "hosted-outside"
    outside.mkdir()

    async with spawn_host(db_path, hosted=True, allowed=(str(work),)) as (connection, _client, _process):
        init = await initialize(connection)
        assert init.field_meta["harness"]["localBindingCreation"] is False
        assert "_harness/plugins/list" not in init.field_meta["harness"]["methods"]

        with pytest.raises(Exception) as denied:
            await connection.new_session(cwd=str(outside))
        assert getattr(denied.value, "code", None) == -32000
        assert denied.value.data["code"] == "WORKSPACE_UNAVAILABLE"

        with pytest.raises(Exception) as mcp:
            await connection.new_session(
                cwd=str(work),
                mcp_servers=[
                    schema.McpServerStdio(
                        name="local", command="python", args=["-m", "x"], env=[]
                    )
                ],
            )
        assert mcp.value.data["code"] == "POLICY_DENIED"

        accepted = await connection.new_session(cwd=str(work))
        assert accepted.session_id
