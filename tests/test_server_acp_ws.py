"""ACP WebSocket transport: handshake policy, relay, framing, worker cleanup."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from _server_helpers import (
    FAKE_HOST,
    TOKEN_ALICE,
    TOKEN_BOB,
    TOKEN_MALLORY,
    auth_headers,
    denial_envelope,
    deployment,
    expect_close,
    fake_host_argv,
    load_test_config,
    make_client,
    principal,
    recv_json,
    wait_for_dead,
    wait_for_pidfile,
    ws_path,
)
from harness_server.errors import ACP_SUBPROTOCOL, MAX_MESSAGE_BYTES
from starlette.testclient import WebSocketDenialResponse

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    worker_dir = tmp_path / "worker-home"
    worker_dir.mkdir()
    deployments = [deployment(cwd=str(worker_dir))]
    return load_test_config(tmp_path, deployments=deployments)


def connect(client, *, agent: str = "fake.acp.r1", token: str = TOKEN_ALICE,
            subprotocols=(ACP_SUBPROTOCOL,), headers=None):
    merged = auth_headers(token)
    merged.update(headers or {})
    return client.websocket_connect(
        ws_path(agent), subprotocols=list(subprotocols), headers=merged
    )


def denial(client, **kwargs) -> tuple[int, dict]:
    try:
        with connect(client, **kwargs):
            pass
    except WebSocketDenialResponse as exc:
        return denial_envelope(exc)
    raise AssertionError("expected the WebSocket upgrade to be denied")


def wait_until_idle(client, timeout: float = 10.0) -> None:
    state = client.app.state.harness
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(state.registry) == 0 and state.slots.active == 0:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"server did not quiesce: registry={len(state.registry)} "
        f"slots={state.slots.active}"
    )


def request(ws, request_id: int, method: str, params: dict | None = None) -> dict:
    ws.send_json(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    )
    return recv_json(ws)


# ---------------------------------------------------------------------------
# Handshake policy
# ---------------------------------------------------------------------------


def test_ws_requires_bearer_token(config) -> None:
    with make_client(config) as client:
        try:
            with client.websocket_connect(ws_path(), subprotocols=[ACP_SUBPROTOCOL]):
                pass
        except WebSocketDenialResponse as exc:
            status, payload = denial_envelope(exc)
            headers = exc.headers
        else:  # pragma: no cover - the upgrade must be denied
            raise AssertionError("expected a denial")
    assert status == 401
    assert payload["error"]["code"] == "UNAUTHORIZED"
    assert "www-authenticate" in headers


def test_ws_rejects_unknown_token(config) -> None:
    with make_client(config) as client:
        status, payload = denial(client, token=TOKEN_MALLORY)
    assert status == 401
    assert payload["error"]["code"] == "UNAUTHORIZED"


def test_ws_requires_the_acp_subprotocol(config) -> None:
    with make_client(config) as client:
        missing = denial(client, subprotocols=())
        wrong = denial(client, subprotocols=("harness.acp.v2",))
    for status, payload in (missing, wrong):
        assert status == 400
        assert payload["error"]["code"] == "INVALID_REQUEST"
        assert payload["error"]["details"]["param"] == "subprotocol"


def test_ws_unknown_agent_is_404(config) -> None:
    with make_client(config) as client:
        status, payload = denial(client, agent="ghost.agent.r9")
    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"


def test_ws_forbidden_agent_is_indistinguishable_from_unknown(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    config = load_test_config(
        tmp_path,
        deployments=[deployment(id="fake.acp.r1"), deployment(id="other.agent.r2")],
        principals=[principal("bob", token=TOKEN_BOB, agents=["other.agent.r2"])],
    )
    with make_client(config) as client:
        forbidden = denial(client, token=TOKEN_BOB, agent="fake.acp.r1")
        unknown = denial(client, token=TOKEN_BOB, agent="ghost.agent.r9")
    assert forbidden[0] == unknown[0] == 404
    assert forbidden[1]["error"]["code"] == unknown[1]["error"]["code"]


def test_ws_spawn_failure_is_a_structured_denial(tmp_path) -> None:
    config = load_test_config(
        tmp_path,
        deployments=[deployment(command=["definitely-not-a-real-harness-host-xyz"])],
    )
    with make_client(config) as client:
        status, payload = denial(client)
        state = client.app.state.harness
        assert state.slots.active == 0
        assert len(state.registry) == 0
    assert status == 503
    assert payload["error"]["code"] == "WORKER_SPAWN_FAILED"


# ---------------------------------------------------------------------------
# Successful relay and server-owned context
# ---------------------------------------------------------------------------


def test_initialize_round_trip_and_server_owned_environment(config, tmp_path) -> None:
    with make_client(config) as client:
        with connect(client) as ws:
            assert ws.accepted_subprotocol == ACP_SUBPROTOCOL
            reply = request(ws, 1, "initialize")
            assert reply["id"] == 1
            assert reply["result"]["fakeHost"] is True

            context = request(ws, 2, "_fake/context")["result"]
            env = context["env"]
            assert env["HARNESS_PRINCIPAL"] == "alice"
            assert env["HARNESS_DEPLOYMENT_ID"] == "fake.acp.r1"
            assert json.loads(env["HARNESS_ALLOWED_CWDS"]) == ["/workspace/fake"]
            assert env["HARNESS_STORE"] == str(
                Path(config.server.store_path) / "fake.acp.r1.sqlite3"
            )
            assert env["HARNESS_HOSTED"] == "1"
            assert env["FAKE_RESOURCE"] == "resource-value"
            assert env["FAKE_MODEL_KEY"] == "model-ref-value"
            assert env["PATH"]  # passthrough survives

            # Exactly the configured argv: direct exec, no shell wrapper.
            assert context["argv"] == [str(FAKE_HOST), "--mode", "echo"]
            assert context["cwd"] == str(tmp_path / "worker-home")


def test_client_metadata_cannot_control_workspace_or_identity(config) -> None:
    with make_client(config) as client:
        with connect(client) as ws:
            poisoned = {
                "_meta": {
                    "harness": {
                        "principal": "mallory",
                        "deploymentId": "evil.agent.r9",
                        "allowedCwds": ["/etc", "/"],
                    }
                }
            }
            context = request(ws, 1, "_fake/context", poisoned)["result"]
            env = context["env"]
            assert env["HARNESS_PRINCIPAL"] == "alice"
            assert env["HARNESS_DEPLOYMENT_ID"] == "fake.acp.r1"
            assert json.loads(env["HARNESS_ALLOWED_CWDS"]) == ["/workspace/fake"]
            # The frame itself is forwarded verbatim; only the environment is
            # authoritative.
            assert context["request"]["params"]["_meta"]["harness"]["principal"] == "mallory"


def test_notifications_flow_in_both_directions(config) -> None:
    with make_client(config) as client:
        with connect(client) as ws:
            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": "s-1"},
                }
            )
            diagnostics = request(ws, 10, "_fake/diagnostics")["result"]
            assert diagnostics["notifications"][-1]["method"] == "session/cancel"

            ws.send_json(
                {"jsonrpc": "2.0", "id": 11, "method": "_fake/push", "params": {"marker": "m1"}}
            )
            notification = recv_json(ws)
            assert notification["method"] == "_fake/notification"
            assert notification["params"]["marker"] == "m1"
            response = recv_json(ws)
            assert response["id"] == 11


# ---------------------------------------------------------------------------
# Framing rules
# ---------------------------------------------------------------------------


def _expect_violation(config, *, text: str | None = None, payload: bytes | None = None,
                      code: int) -> None:
    with make_client(config) as client:
        with connect(client) as ws:
            if payload is not None:
                ws.send_bytes(payload)
            else:
                ws.send_text(text or "")
            expect_close(ws, code)
        wait_until_idle(client)


def test_binary_frames_are_rejected(config) -> None:
    _expect_violation(config, payload=b'{"jsonrpc":"2.0"}', code=1003)


def test_batch_frames_are_rejected(config) -> None:
    _expect_violation(
        config, text='[{"jsonrpc":"2.0","id":1,"method":"initialize"}]', code=1002
    )


def test_non_object_frames_are_rejected(config) -> None:
    _expect_violation(config, text="42", code=1002)


def test_invalid_json_frames_are_rejected(config) -> None:
    _expect_violation(config, text="this is not json", code=1002)


def test_empty_frames_are_rejected(config) -> None:
    _expect_violation(config, text="   ", code=1002)


def test_oversize_frames_are_rejected(config) -> None:
    padding = "x" * (MAX_MESSAGE_BYTES + 8)
    frame = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"pad":"' + padding + '"}}'
    _expect_violation(config, text=frame, code=1009)


# ---------------------------------------------------------------------------
# Worker failure modes and cleanup
# ---------------------------------------------------------------------------


def test_worker_crash_on_start_closes_with_worker_exited(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    config = load_test_config(
        tmp_path,
        deployments=[deployment(command=fake_host_argv("--mode", "crash-on-start"))],
    )
    with make_client(config) as client:
        with connect(client) as ws:
            expect_close(ws, 4511)
        wait_until_idle(client)


def test_worker_crash_mid_stream_closes_after_response(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    config = load_test_config(
        tmp_path,
        deployments=[deployment(command=fake_host_argv("--mode", "crash-after-first-response"))],
    )
    with make_client(config) as client:
        with connect(client) as ws:
            reply = request(ws, 1, "initialize")
            assert reply["id"] == 1
            expect_close(ws, 4511)
        wait_until_idle(client)


def test_worker_invalid_json_is_terminated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    pidfile = tmp_path / "worker.pid"
    config = load_test_config(
        tmp_path,
        deployments=[
            deployment(command=fake_host_argv("--mode", "bad-line", "--pidfile", str(pidfile)))
        ],
    )
    with make_client(config) as client:
        with connect(client) as ws:
            expect_close(ws, 1002)
        wait_until_idle(client)
    wait_for_dead(wait_for_pidfile(pidfile))


def test_worker_oversize_frame_is_rejected_and_terminated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    pidfile = tmp_path / "worker.pid"
    config = load_test_config(
        tmp_path,
        deployments=[
            deployment(command=fake_host_argv("--mode", "oversize", "--pidfile", str(pidfile)))
        ],
    )
    with make_client(config) as client:
        with connect(client) as ws:
            expect_close(ws, 1009)
        wait_until_idle(client)
    wait_for_dead(wait_for_pidfile(pidfile), timeout=12.0)


def test_disconnect_terminates_the_worker_process(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    pidfile = tmp_path / "worker.pid"
    config = load_test_config(
        tmp_path,
        deployments=[deployment(command=fake_host_argv("--pidfile", str(pidfile)))],
    )
    with make_client(config) as client:
        with connect(client) as ws:
            assert request(ws, 1, "initialize")["id"] == 1
        pid = wait_for_pidfile(pidfile)
        wait_for_dead(pid)
        wait_until_idle(client)


def test_worker_child_process_group_is_killed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    pidfile = tmp_path / "worker.pid"
    child_pidfile = tmp_path / "child.pid"
    config = load_test_config(
        tmp_path,
        deployments=[
            deployment(
                command=fake_host_argv(
                    "--mode",
                    "child",
                    "--pidfile",
                    str(pidfile),
                    "--child-pidfile",
                    str(child_pidfile),
                )
            )
        ],
    )
    with make_client(config) as client:
        with connect(client) as ws:
            assert request(ws, 1, "initialize")["id"] == 1
        parent = wait_for_pidfile(pidfile)
        child = wait_for_pidfile(child_pidfile)
        wait_for_dead(parent)
        wait_for_dead(child)
        wait_until_idle(client)


def test_stubborn_worker_is_terminated_within_the_bound(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    pidfile = tmp_path / "worker.pid"
    config = load_test_config(
        tmp_path,
        deployments=[deployment(command=fake_host_argv("--mode", "stay", "--pidfile", str(pidfile)))],
        server={"worker_term_grace_seconds": 0.4, "worker_kill_grace_seconds": 0.4},
    )
    with make_client(config) as client:
        with connect(client) as ws:
            assert request(ws, 1, "initialize")["id"] == 1
        pid = wait_for_pidfile(pidfile)
        started = time.monotonic()
        wait_for_dead(pid, timeout=8.0)
        elapsed = time.monotonic() - started
        assert elapsed < 6.0
        wait_until_idle(client)


def test_each_connection_gets_its_own_worker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    config = load_test_config(
        tmp_path,
        deployments=[deployment()],
        server={"max_connections_per_principal": 2},
    )
    with make_client(config) as client:
        with connect(client) as first, connect(client) as second:
            pid_one = request(first, 1, "_fake/context")["result"]["pid"]
            pid_two = request(second, 2, "_fake/context")["result"]["pid"]
        assert pid_one != pid_two
        wait_until_idle(client)


# ---------------------------------------------------------------------------
# Capacity limits
# ---------------------------------------------------------------------------


def test_per_principal_connection_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    config = load_test_config(
        tmp_path,
        deployments=[deployment()],
        server={"max_connections_per_principal": 1},
    )
    with make_client(config) as client:
        with connect(client) as ws:
            assert request(ws, 1, "initialize")["id"] == 1
            status, payload = denial(client)
            assert status == 503
            assert payload["error"]["code"] == "SERVER_BUSY"
            assert payload["error"]["details"]["reason"] == "principal_limit"
        # Slot release after disconnect allows a new connection.
        wait_until_idle(client)
        with connect(client) as ws:
            assert request(ws, 2, "initialize")["id"] == 2
        wait_until_idle(client)


def test_global_worker_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    config = load_test_config(
        tmp_path,
        deployments=[deployment()],
        principals=[
            principal("alice", token=TOKEN_ALICE),
            principal("bob", token=TOKEN_BOB, agents=["fake.acp.r1"]),
        ],
        server={"max_worker_count": 1},
    )
    with make_client(config) as client:
        with connect(client) as ws:
            assert request(ws, 1, "initialize")["id"] == 1
            status, payload = denial(client, token=TOKEN_BOB)
            assert status == 503
            assert payload["error"]["details"]["reason"] == "worker_limit"
        wait_until_idle(client)
        with connect(client, token=TOKEN_BOB) as ws:
            assert request(ws, 2, "initialize")["id"] == 2
        wait_until_idle(client)


# ---------------------------------------------------------------------------
# Diagnostics hygiene
# ---------------------------------------------------------------------------


def test_stderr_is_drained_but_never_forwarded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")
    config = load_test_config(
        tmp_path,
        deployments=[deployment(command=fake_host_argv("--noise-stderr"))],
    )
    with make_client(config) as client:
        with connect(client) as ws:
            reply = request(ws, 1, "initialize")
            assert reply["id"] == 1
            second = request(ws, 2, "initialize")
            assert second["id"] == 2
            assert "SUPER-SECRET" not in json.dumps(reply)
        wait_until_idle(client)
