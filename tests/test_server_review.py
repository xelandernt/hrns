"""Independent security review of ``harness-server`` (CONCEPT.md §11, docs/SERVER.md).

Every test here drives the **official Starlette TestClient** (and, where
needed, focused subprocess fake workers) against one requirement of the
security/transport contract, written independently of the shipped suites:

* bearer authentication on HTTP and the WebSocket upgrade (§11.3),
* principal -> deployment scoping and unknown/forbidden 404 parity (§11.2/3),
* catalog responses leak no server-internal paths, DSN, or resource secrets,
* the WebSocket subprotocol ``harness.acp.v1`` is required and selected (§11.2),
* 16 MiB message limit (both directions, boundary inclusive), binary-frame,
  JSON-RPC batch and non-object rejection (§11.2),
* the worker environment is server-owned: client-supplied ACP metadata cannot
  override principal/deployment identity, workspace roots, or the store,
* ``allowed_cwds`` reach the worker verbatim from configuration only (§11.5),
* worker process groups (including orphans) are killed on disconnect; denied
  handshakes never spawn a worker,
* per-principal and global connection capacity with observable 503s,
* the PostgreSQL DSN reaches workers as ``HARNESS_STORE`` only and never
  appears in catalog or error payloads,
* TLS policy: plaintext from non-loopback peers is refused; forwarded proto is
  honoured only from trusted peers,
* pagination hardening: bounded limits, HMAC cursors, no offset errors.

Reviewer disposition (recorded in the review report): no Critical or Required
issue found.  Two informational observations: (1) a 503 ``WORKER_SPAWN_FAILED``
WS denial includes the worker argv program ``program``/``cwd`` detail fields —
server filesystem layout exposure to authenticated callers only; (2) the proxy
is content-neutral, so ``session cwd`` enforcement lives in the harness-host
worker via ``HARNESS_ALLOWED_CWDS`` (verified in ``harness_runtime.host``);
third-party worker binaries must honour that contract themselves.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from _server_helpers import (
    REMOTE,
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
from harness_server import create_app, sha256_hex
from harness_server.errors import ACP_SUBPROTOCOL, MAX_MESSAGE_BYTES
from starlette.testclient import TestClient, WebSocketDenialResponse

DSN = "postgresql://harness:hunter2-dsn-88@db.internal:5432/harness?sslmode=require"

BAD_FRAMES = {
    "json-rpc batch": '[{"jsonrpc":"2.0","id":1,"method":"initialize"}]',
    "non-object int": "42",
    "non-object null": "null",
    "non-object string": '"an ACP message is a JSON-RPC object"',
    "not json at all": "<xml>sneaky</xml>",
}


# ---------------------------------------------------------------------------
# Fixtures and small helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployment defaults reference this server environment variable."""
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")


def short_graces(**extra) -> dict:
    return {
        "worker_term_grace_seconds": 1.0,
        "worker_kill_grace_seconds": 1.0,
        **extra,
    }


def wait_idle(state, timeout: float = 15.0) -> None:
    """The connection must release its slot and reap its worker promptly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(state.registry) == 0 and state.slots.active == 0:
            return
        time.sleep(0.02)
    pytest.fail(
        f"connection state did not quiesce: slots={state.slots.active} "
        f"registry={len(state.registry)}"
    )


def denial(client: TestClient, *, agent: str = "fake.acp.r1",
           token: str = TOKEN_ALICE, headers: dict | None = None, **ws_kwargs):
    """Run a WS handshake that must fail with an HTTP denial response.

    Unless ``subprotocols`` is passed explicitly, the handshake offers exactly
    ``harness.acp.v1`` so the asserted denial is the one after negotiation.
    """
    merged = auth_headers(token) if headers is None else dict(headers)
    ws_kwargs.setdefault("subprotocols", [ACP_SUBPROTOCOL])
    try:
        with client.websocket_connect(ws_path(agent), headers=merged, **ws_kwargs):
            pass
    except WebSocketDenialResponse as exc:
        return denial_envelope(exc), exc
    raise AssertionError("expected the WebSocket upgrade to be denied")


# ---------------------------------------------------------------------------
# §11.3 bearer authentication (HTTP catalog + WebSocket upgrade)
# ---------------------------------------------------------------------------


def test_http_catalog_rejects_bad_credentials(tmp_path):
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    with make_client(config) as client:
        missing = client.get("/v1/agents")
        assert missing.status_code == 401
        error = missing.json()["error"]
        assert error["code"] == "UNAUTHORIZED"
        assert missing.headers.get("www-authenticate") == "Bearer"

        for authz, label in (
            (f"Bearer {TOKEN_MALLORY}", "unknown token"),
            ("Bearer  ", "empty credential"),
            ("Basic Zm9vOmJhcg==", "wrong scheme"),
            ("Bearer NOT-A-VALID-TOKEN", "invalid credential"),
        ):
            response = client.get("/v1/agents", headers={"Authorization": authz})
            assert response.status_code == 401, label
            assert response.json()["error"]["code"] == "UNAUTHORIZED", label
            assert "www-authenticate" in response.headers, label

        # RFC 7235: the scheme is case-insensitive.
        ok = client.get("/v1/agents", headers={"Authorization": f"bearer {TOKEN_ALICE}"})
        assert ok.status_code == 200

        # The catalog is read-only discovery; writes are method violations.
        post = client.post("/v1/agents", headers=auth_headers(), json={})
        assert post.status_code == 405
        assert post.json()["error"]["code"] == "METHOD_NOT_ALLOWED"

    # /healthz is the single deliberate unauthenticated endpoint (§11.2).
    with make_client(config) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["service"] == "harness-server"


def test_ws_upgrade_uses_http_layer_authentication(tmp_path):
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    with make_client(config) as client:
        for headers, label in (
            ({}, "no authorization header"),
            ({"Authorization": "Basic Zm9v"}, "wrong scheme"),
            ({"Authorization": f"Bearer {TOKEN_MALLORY}"}, "unknown token"),
        ):
            (status, payload), exc = denial(client, headers=headers)
            assert status == 401, label
            assert payload["error"]["code"] == "UNAUTHORIZED", label
            assert exc.headers.get("www-authenticate") == "Bearer", label

        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            assert ws.accepted_subprotocol == ACP_SUBPROTOCOL


# ---------------------------------------------------------------------------
# §11.2/§11.3 principal -> deployment scoping (cannot probe the catalog)
# ---------------------------------------------------------------------------


def scoped_config(tmp_path):
    return load_test_config(
        tmp_path,
        deployments=[deployment(id="alpha.agent.r1", cwd=str(tmp_path)),
                     deployment(id="beta.agent.r2", cwd=str(tmp_path))],
        principals=[principal("alice", token=TOKEN_ALICE, agents=["alpha.agent.r1"]),
                    principal("bob", token=TOKEN_BOB, agents=["beta.agent.r2"]),
                    principal("carol", token=TOKEN_MALLORY, agents=["alpha.agent.r1"])],
    )


def test_catalog_lists_only_the_principal_allowed_revisions(tmp_path):
    with make_client(scoped_config(tmp_path)) as client:
        ids = {item["agentId"]
               for item in client.get("/v1/agents",
                                      headers=auth_headers(TOKEN_ALICE)).json()["items"]}
        assert ids == {"alpha.agent.r1"}

        ids = {item["agentId"]
               for item in client.get("/v1/agents",
                                      headers=auth_headers(TOKEN_BOB)).json()["items"]}
        assert ids == {"beta.agent.r2"}

        # Scoping applies within pagination as well; limit cannot widen it.
        page = client.get("/v1/agents?limit=100", headers=auth_headers(TOKEN_BOB)).json()
        assert {item["agentId"] for item in page["items"]} == {"beta.agent.r2"}


def test_unknown_and_forbidden_agents_share_one_404(tmp_path):
    with make_client(scoped_config(tmp_path)) as client:
        unknown = client.get("/v1/agents/ghost.agent.r9", headers=auth_headers(TOKEN_ALICE))
        forbidden = client.get("/v1/agents/beta.agent.r2", headers=auth_headers(TOKEN_ALICE))
    assert unknown.status_code == forbidden.status_code == 404
    assert unknown.json()["error"]["code"] == forbidden.json()["error"]["code"] == "NOT_FOUND"
    assert unknown.json()["error"]["message"] == forbidden.json()["error"]["message"]
    assert set(unknown.json()["error"]["details"]) == set(forbidden.json()["error"]["details"])


def test_ws_agent_scoping_is_indistinguishable_for_unknown_and_forbidden(tmp_path):
    with make_client(scoped_config(tmp_path)) as client:
        (unknown_status, unknown_payload), _ = denial(client, agent="ghost.agent.r9")
        (forbidden_status, forbidden_payload), _ = denial(
            client, agent="beta.agent.r2", token=TOKEN_MALLORY)
    assert unknown_status == forbidden_status == 404
    assert unknown_payload["error"]["code"] == forbidden_payload["error"]["code"]
    assert unknown_payload["error"]["message"] == forbidden_payload["error"]["message"]


# ---------------------------------------------------------------------------
# Catalog information leaks
# ---------------------------------------------------------------------------


def test_descriptors_leak_no_server_filesystem_or_resource_secrets(tmp_path):
    lockfile = tmp_path / "locks" / "revision.lock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    lockfile.write_text("lock content", encoding="utf-8")
    config = load_test_config(
        tmp_path,
        deployments=[deployment(
            cwd=str(tmp_path),
            lockfile_path=str(lockfile),
            lockfile_sha256=sha256_hex("lock content"),
            env={"FAKE_RESOURCE": "resource-value-5611", "DB_SECRET": "db-secret-4242"},
            env_from_environment=["FAKE_MODEL_KEY"],
        )],
        server=short_graces(store_url=DSN),
    )
    secrets = (DSN, "db-secret-4242", "resource-value-5611", "model-ref-value",
               str(tmp_path), str(lockfile), "harness-store")

    with make_client(config) as client:
        responses = [
            client.get("/v1/agents", headers=auth_headers()),
            client.get("/v1/agents/fake.acp.r1", headers=auth_headers()),
        ]
        descriptors = [response.json() for response in responses]
        blob = "".join(response.text for response in responses)
        blob += json.dumps(dict(responses[1].headers))
        for secret in secrets:
            assert secret not in blob, f"leaked {secret!r} through the catalog"

    descriptor = descriptors[1]
    assert sorted(descriptor) == [
        "agentId", "allowedWorkspaces", "connection", "defaultWorkspace",
        "description", "lockfile", "name", "recommended", "version",
    ]
    # The lockfile is identified by basename and content digest only; the
    # server's absolute layout (lock directory, worker cwd) stays private.
    assert descriptor["lockfile"]["name"] == "revision.lock"
    assert "/" not in descriptor["lockfile"]["name"]
    assert set(descriptor["connection"]) == {"url", "subprotocol", "maxMessageBytes"}
    assert descriptor["connection"]["maxMessageBytes"] == MAX_MESSAGE_BYTES


# ---------------------------------------------------------------------------
# §11.2 WebSocket subprotocol negotiation
# ---------------------------------------------------------------------------


def test_ws_requires_and_selects_harness_subprotocol(tmp_path):
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    with make_client(config) as client:
        for offered, label in (((), "none offered"),
                               (("harness.acp.v0",), "wrong protocol"),
                               (("harness.acp.v9",), "other protocol")):
            (status, payload), _ = denial(client, subprotocols=list(offered))
            assert status == 400, f"expected HTTP 400 denial with no/wrong subprotocol ({label})"
            assert payload["error"]["code"] == "INVALID_REQUEST", offered
            assert payload["error"]["details"]["param"] == "subprotocol", offered

        for offered in ([ACP_SUBPROTOCOL], ["harness.acp.v0", ACP_SUBPROTOCOL]):
            with client.websocket_connect(
                ws_path(), headers=auth_headers(), subprotocols=list(offered)
            ) as ws:
                # The server always selects exactly its own transport version.
                assert ws.accepted_subprotocol == ACP_SUBPROTOCOL, offered


# ---------------------------------------------------------------------------
# §11.2 framing: 16 MiB limit, binary, batch, non-object, invalid JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frame,code", [(text, 1002) for text in BAD_FRAMES.values()],
                         ids=list(BAD_FRAMES))
def test_client_protocol_violations_close_with_the_mapped_code(tmp_path, frame, code):
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            ws.send_text(frame)
            expect_close(ws, code)
        wait_idle(client.app.state.harness)


def test_binary_frames_close_with_unsupported_data(tmp_path):
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            ws.send_bytes(b"\x00\x01binary does not belong on this transport")
            expect_close(ws, 1003)
        wait_idle(client.app.state.harness)


def test_oversize_frames_close_with_status_1009(tmp_path):
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    frame = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"pad":"'
             + "x" * (MAX_MESSAGE_BYTES + 8) + '"}}')
    assert len(frame.encode("utf-8")) > MAX_MESSAGE_BYTES
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            ws.send_text(frame)
            expect_close(ws, 1009)
        # The relay ends the session and the server still frees its resources.
        assert client.app.state.harness.slots.active == 0
        wait_idle(client.app.state.harness)


def test_boundary_message_of_exactly_16_mib_is_relayed(tmp_path):
    """The 16 MiB limit is inclusive: a valid full-size frame reaches the worker."""
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    prefix = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"pad":"'
    fixed = len(prefix) + len('"}}')
    frame = prefix + "x" * (MAX_MESSAGE_BYTES - fixed) + '"}}'
    assert len(frame.encode("utf-8")) == MAX_MESSAGE_BYTES
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            ws.send_text(frame)
            reply = recv_json(ws)
            assert reply["id"] == 1
            assert reply["result"]["fakeHost"] is True
        wait_idle(client.app.state.harness)


def test_worker_oversize_frame_closes_with_status_1009(tmp_path):
    config = load_test_config(
        tmp_path,
        deployments=[deployment(cwd=str(tmp_path),
                                command=fake_host_argv("--mode", "oversize"))],
    )
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            expect_close(ws, 1009)
        wait_idle(client.app.state.harness)


def test_worker_invalid_utf8_frame_closes_with_status_1007(tmp_path):
    script = tmp_path / "bad_utf8_worker.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(bytes("
        "[0x7b, 0x22, 0x61, 0x22, 0x3a, 0xff, 0xfe, 0x7d, 0x0a]))\n"
        "sys.stdout.buffer.flush()\n"
        "while sys.stdin.buffer.readline():\n"
        "    pass\n",
        encoding="utf-8",
    )
    config = load_test_config(
        tmp_path,
        deployments=[deployment(cwd=str(tmp_path),
                                command=[sys.executable, str(script)])],
    )
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            expect_close(ws, 1007)
        wait_idle(client.app.state.harness)


# ---------------------------------------------------------------------------
# Worker environment identity: client metadata cannot override the contract
# ---------------------------------------------------------------------------


def poisons() -> list[dict]:
    """Client-supplied metadata vectors that must never touch the worker env."""
    return [
        {"env": {"HARNESS_PRINCIPAL": "root", "HARNESS_DEPLOYMENT_ID": "evil.r9",
                 "HARNESS_STORE": "/tmp/attacker.sqlite3",
                 "HARNESS_ALLOWED_CWDS": ["/"], "HARNESS_HOSTED": "0"}},
        {"principal": "mallory", "deploymentId": "evil.r9", "cwd": "/etc"},
        {"_meta": {"harness": {"principal": "mallory", "deploymentId": "evil.r9",
                               "allowedCwds": ["/"], "store": DSN}}},
        {"initialize": {"principal": "root", "env": {"HARNESS_HOSTED": "0"}}},
    ]


def test_worker_env_identity_is_server_owned_per_deployment(tmp_path):
    alpha = deployment(id="alpha.agent.r1", cwd=str(tmp_path),
                       allowed_cwds=["/ws/alpha"], default_cwd="/ws/alpha")
    beta = deployment(id="beta.agent.r2", cwd=str(tmp_path),
                      allowed_cwds=["/ws/beta"], default_cwd="/ws/beta")
    config = load_test_config(
        tmp_path, deployments=[alpha, beta],
        principals=[principal("alice", token=TOKEN_ALICE,
                              agents=["alpha.agent.r1", "beta.agent.r2"])],
    )
    contract_keys = ["HARNESS_ALLOWED_CWDS", "HARNESS_DEPLOYMENT_ID",
                     "HARNESS_HOSTED", "HARNESS_PRINCIPAL", "HARNESS_STORE"]
    with make_client(config) as client:
        identity = None
        for agent_id, allowed, store_tail in (
            ("alpha.agent.r1", "/ws/alpha", "alpha.agent.r1.sqlite3"),
            ("beta.agent.r2", "/ws/beta", "beta.agent.r2.sqlite3"),
        ):
            with client.websocket_connect(ws_path(agent_id), headers=auth_headers(),
                                          subprotocols=[ACP_SUBPROTOCOL]) as ws:
                for poison in poisons():
                    ws.send_json({"jsonrpc": "2.0", "id": 7,
                                  "method": "_fake/context", "params": poison})
                    result = recv_json(ws)["result"]
                    env = result["env"]
                    assert env["HARNESS_PRINCIPAL"] == "alice"
                    assert env["HARNESS_DEPLOYMENT_ID"] == agent_id
                    assert json.loads(env["HARNESS_ALLOWED_CWDS"]) == [allowed]
                    assert env["HARNESS_STORE"] == str(
                        Path(config.server.store_path) / store_tail)
                    assert env["HARNESS_HOSTED"] == "1"
                    # The reserved HARNESS_ namespace carries exactly the five
                    # server-owned contract variables and nothing injected.
                    assert sorted(result["harnessEnv"]) == contract_keys
                    for value in poison.values():
                        assert json.dumps(value) not in json.dumps(result["harnessEnv"])
                # Distinct revisions never share a store file.
                if identity is None:
                    identity = {"principal": env["HARNESS_PRINCIPAL"],
                                "store": env["HARNESS_STORE"]}
                else:
                    assert env["HARNESS_STORE"] != identity["store"]


# ---------------------------------------------------------------------------
# §11.5 workspace bindings advertised to the client, delivered to the worker
# ---------------------------------------------------------------------------


def test_descriptor_exports_only_configured_workspace_roots(tmp_path):
    config = load_test_config(
        tmp_path,
        deployments=[deployment(cwd=str(tmp_path),
                                allowed_cwds=["/srv/alpha", "/srv/beta"],
                                default_cwd="/srv/alpha")],
    )
    with make_client(config) as client:
        descriptor = client.get("/v1/agents/fake.acp.r1",
                                headers=auth_headers()).json()
        assert descriptor["allowedWorkspaces"] == ["/srv/alpha", "/srv/beta"]
        assert descriptor["defaultWorkspace"] == "/srv/alpha"


def test_default_cwd_must_be_one_of_the_allowed_roots(tmp_path):
    document = {
        "server": {"store_path": str(tmp_path / "store")},
        "deployments": [deployment(allowed_cwds=["/workspace/fake"],
                                   default_cwd="/etc/passwd")],
        "principals": [principal()],
    }
    with pytest.raises(ValueError, match="default_cwd"):
        create_app(document)


# ---------------------------------------------------------------------------
# Worker lifecycle: process groups die on disconnect; denials spawn nothing
# ---------------------------------------------------------------------------


def test_worker_and_orphaned_child_are_killed_after_disconnect(tmp_path):
    worker_pid = tmp_path / "worker.pid"
    child_pid = tmp_path / "child.pid"
    config = load_test_config(
        tmp_path,
        deployments=[deployment(
            cwd=str(tmp_path),
            command=fake_host_argv("--mode", "child", "--pidfile", str(worker_pid),
                                   "--child-pidfile", str(child_pid)),
        )],
        server=short_graces(),
    )
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            assert recv_json(ws)["result"]["fakeHost"] is True
            leader = wait_for_pidfile(worker_pid)
            orphan = wait_for_pidfile(child_pid)
        # The upgrade is over: the whole process group (leader + sleeper child)
        # must be reaped within the configured graces.
        wait_for_dead(leader, timeout=10.0)
        wait_for_dead(orphan, timeout=10.0)
        wait_idle(client.app.state.harness)


def test_stubborn_worker_is_killed_when_it_ignores_stdin_eof(tmp_path):
    worker_pid = tmp_path / "worker.pid"
    config = load_test_config(
        tmp_path,
        deployments=[deployment(
            cwd=str(tmp_path),
            command=fake_host_argv("--mode", "stay", "--pidfile", str(worker_pid)),
        )],
        server=short_graces(),
    )
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as ws:
            ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            assert recv_json(ws)["result"]["fakeHost"] is True
        pid = wait_for_pidfile(worker_pid)
        # The worker ignores stdin EOF, so the server must SIGTERM the group.
        wait_for_dead(pid, timeout=10.0)
        wait_idle(client.app.state.harness)


def test_denied_handshakes_never_spawn_a_worker(tmp_path):
    config = load_test_config(
        tmp_path,
        deployments=[deployment(command=["no-such-worker-binary-here-xyz"])],
    )
    with make_client(config) as client:
        denial(client, headers={})  # 401 happens before any spawn attempt
        (status, payload), _ = denial(client)  # reachable: 503 spawn failure
        assert status == 503
        assert payload["error"]["code"] == "WORKER_SPAWN_FAILED"
        state = client.app.state.harness
        assert len(state.registry) == 0
        assert state.slots.active == 0
        # Spawn failures must not expose resource bindings in the denial body.
        assert DSN not in json.dumps(payload)


def test_denied_bearer_or_agent_handshakes_spawn_nothing(tmp_path):
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    with make_client(config) as client:
        denial(client, headers={})
        denial(client, token=TOKEN_MALLORY)
        denial(client, agent="ghost.agent.r9")
        state = client.app.state.harness
        assert len(state.registry) == 0 and state.slots.active == 0


# ---------------------------------------------------------------------------
# Capacity: per-principal and global connection slots
# ---------------------------------------------------------------------------


def test_per_principal_connection_limit_is_enforced_and_released(tmp_path):
    config = load_test_config(
        tmp_path,
        deployments=[deployment(cwd=str(tmp_path))],
        server=short_graces(max_connections_per_principal=2),
    )
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as first, \
             client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]) as second:
            assert first.accepted_subprotocol and second.accepted_subprotocol
            assert client.app.state.harness.slots.active == 2
            # The HTTP catalog stays usable while every slot is busy.
            assert client.get("/v1/agents", headers=auth_headers()).status_code == 200
            (status, payload), exc = denial(client)
            assert status == 503
            assert payload["error"]["code"] == "SERVER_BUSY"
            assert payload["error"]["details"]["reason"] == "principal_limit"
            assert exc.headers.get("retry-after") == "1"
        wait_idle(client.app.state.harness)
        # Limits are live accounting, not a lock-out: reconnecting works.
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]):
            assert client.app.state.harness.slots.active == 1


def test_global_worker_limit_blocks_other_principals(tmp_path):
    config = load_test_config(
        tmp_path,
        deployments=[deployment(cwd=str(tmp_path))],
        principals=[principal("alice", token=TOKEN_ALICE, agents=["fake.acp.r1"]),
                    principal("bob", token=TOKEN_BOB, agents=["fake.acp.r1"])],
        server=short_graces(max_worker_count=1),
    )
    with make_client(config) as client:
        with client.websocket_connect(ws_path(), headers=auth_headers(),
                                      subprotocols=[ACP_SUBPROTOCOL]):
            assert client.app.state.harness.slots.active == 1
            (status, payload), _ = denial(client, token=TOKEN_BOB)
            assert status == 503
            assert payload["error"]["code"] == "SERVER_BUSY"
            assert payload["error"]["details"]["reason"] == "worker_limit"
        wait_idle(client.app.state.harness)
        assert client.app.state.harness.slots.active == 0


# ---------------------------------------------------------------------------
# DSN handling: server.store_url reaches workers only
# ---------------------------------------------------------------------------


def test_store_url_precedence_and_secret_containment(tmp_path):
    store_dir = tmp_path / "never-created"
    config = load_test_config(
        tmp_path,
        deployments=[deployment(id="alpha.agent.r1", cwd=str(tmp_path),
                                allowed_cwds=["/ws/alpha"], default_cwd="/ws/alpha"),
                     deployment(id="beta.agent.r2", cwd=str(tmp_path),
                                allowed_cwds=["/ws/beta"], default_cwd="/ws/beta")],
        principals=[principal("alice", token=TOKEN_ALICE,
                              agents=["alpha.agent.r1", "beta.agent.r2"])],
        server=short_graces(store_path=str(store_dir), store_url=DSN),
    )
    with make_client(config) as client:
        for agent_id in ("alpha.agent.r1", "beta.agent.r2"):
            with client.websocket_connect(ws_path(agent_id), headers=auth_headers(),
                                          subprotocols=[ACP_SUBPROTOCOL]) as ws:
                ws.send_json({"jsonrpc": "2.0", "id": 3, "method": "_fake/context"})
                env = recv_json(ws)["result"]["env"]
                # The DSN overrides the per-revision SQLite file for workers.
                assert env["HARNESS_STORE"] == DSN
                assert not env["HARNESS_STORE"].endswith(".sqlite3")
                assert env["HARNESS_PRINCIPAL"] == "alice"
        sentinel = json.dumps(client.get("/v1/agents", headers=auth_headers()).json())
        sentinel += client.get("/v1/agents/alpha.agent.r1", headers=auth_headers()).text
    # The DSN never leaves the worker environment contract.
    assert DSN not in sentinel
    # A shared database means the store_path directory is never created.
    assert not store_dir.exists()


# ---------------------------------------------------------------------------
# TLS policy (§11.3): plaintext peers refused, trusted proxies honoured
# ---------------------------------------------------------------------------


def tls_configs(tmp_path):
    plain_dir, lax_dir, trusting_dir = (tmp_path / name for name in
                                        ("plain", "lax", "trusting"))
    for directory in (plain_dir, lax_dir, trusting_dir):
        directory.mkdir(parents=True, exist_ok=True)
    plain = load_test_config(
        plain_dir, deployments=[deployment(cwd=str(tmp_path))],
        server=short_graces(),  # forwarded_allow_ips default ["127.0.0.1"]
    )
    lax = load_test_config(
        lax_dir, deployments=[deployment(cwd=str(tmp_path))],
        server=short_graces(require_tls=False),
    )
    trusting = load_test_config(
        trusting_dir, deployments=[deployment(cwd=str(tmp_path))],
        server=short_graces(forwarded_allow_ips=[REMOTE[0]]),
    )
    return plain, lax, trusting


def test_plain_http_from_remote_peer_is_refused(tmp_path):
    plain, lax, _trusting = tls_configs(tmp_path)
    with TestClient(create_app(plain), client=REMOTE) as client:
        response = client.get("/v1/agents", headers=auth_headers())
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "TLS_REQUIRED"
        # Transport policy runs before authentication: without a token the
        # verdict is the same, so plaintext probing learns nothing new.
        anonymous = client.get("/v1/agents")
        assert anonymous.status_code == 403
        assert anonymous.json()["error"]["code"] == "TLS_REQUIRED"

    # require_tls off: the internal-network exemption in docs/SERVER.md.
    with TestClient(create_app(lax), client=REMOTE) as client:
        assert client.get("/v1/agents", headers=auth_headers()).status_code == 200


def test_forwarded_proto_is_trusted_only_from_configured_peers(tmp_path):
    plain, _lax, trusting = tls_configs(tmp_path)
    with TestClient(create_app(plain), client=REMOTE) as client:
        spoofed = client.get("/v1/agents",
                             headers={**auth_headers(), "X-Forwarded-Proto": "https"})
        assert spoofed.status_code == 403
        assert spoofed.json()["error"]["code"] == "TLS_REQUIRED"

    with TestClient(create_app(trusting), client=REMOTE) as client:
        for proto in ("https", "wss"):
            response = client.get("/v1/agents",
                                  headers={**auth_headers(), "X-Forwarded-Proto": proto})
            assert response.status_code == 200, proto


def test_tls_policy_applies_before_ws_auth_for_remote_peers(tmp_path):
    config = load_test_config(
        tmp_path, deployments=[deployment(cwd=str(tmp_path))],
        server=short_graces(forwarded_allow_ips=["127.0.0.1"]),
    )
    with TestClient(create_app(config), client=REMOTE) as client:
        # Bearer token present but plaintext: refused for TLS reasons, not
        # authenticated first and not silently downgraded.
        (status, payload), _ = denial(client, token=TOKEN_ALICE)
        assert status == 403
        assert payload["error"]["code"] == "TLS_REQUIRED"


# ---------------------------------------------------------------------------
# §11.2 pagination hardening
# ---------------------------------------------------------------------------


def paginate_config(tmp_path):
    return load_test_config(
        tmp_path,
        deployments=[deployment(id=f"agent.r{i}", cwd=str(tmp_path))
                     for i in range(5)],
        principals=[principal("alice", token=TOKEN_ALICE,
                              agents=[f"agent.r{i}" for i in range(5)])],
    )


def test_limit_is_bounded_and_cursors_are_authentic(tmp_path):
    with make_client(paginate_config(tmp_path)) as client:
        for raw in ("0", "101", "-3", "abc", "1.5", "1e2", "9" * 50):
            response = client.get(f"/v1/agents?limit={raw}", headers=auth_headers())
            error = response.json()["error"]
            assert response.status_code == 400, raw
            assert error["code"] == "INVALID_REQUEST", raw
            assert error["details"]["param"] == "limit", raw

        cursor = client.get("/v1/agents?limit=1",
                            headers=auth_headers()).json()["nextCursor"]
        assert cursor, "five deployments at limit 1 must return a next cursor"
        replacement = "B" if cursor[-1] != "B" else "C"
        tampered = client.get(
            f"/v1/agents?limit=1&cursor={cursor[:-1]}{replacement}",
            headers=auth_headers(),
        )
        assert tampered.status_code == 400
        assert tampered.json()["error"]["details"]["param"] == "cursor"

        # A cursor signed by a different server instance must not decode either.
        with make_client(paginate_config(tmp_path)) as other:
            foreign_cursor = other.get("/v1/agents?limit=1",
                                       headers=auth_headers()).json()["nextCursor"]
        foreign = client.get(f"/v1/agents?limit=1&cursor={foreign_cursor}",
                             headers=auth_headers())
        assert foreign.status_code == 400
        assert foreign.json()["error"]["details"]["param"] == "cursor"

        # Happy path: continue from a cursor, then terminate on the last page.
        page = client.get("/v1/agents?limit=2", headers=auth_headers()).json()
        assert [item["agentId"] for item in page["items"]] == ["agent.r0", "agent.r1"]
        page = client.get(f"/v1/agents?limit=2&cursor={page['nextCursor']}",
                          headers=auth_headers()).json()
        assert [item["agentId"] for item in page["items"]] == ["agent.r2", "agent.r3"]
        cursor = page["nextCursor"]
        tail = client.get(f"/v1/agents?limit=2&cursor={cursor}",
                          headers=auth_headers()).json()
        assert [item["agentId"] for item in tail["items"]] == ["agent.r4"]
        assert tail["nextCursor"] is None
        # Reusing the cursor that fetched the final page is accepted and
        # replays that page (docs/SERVER.md describes this as an "empty page";
        # the observable, harmless behavior is a repeated final page).
        replay = client.get(f"/v1/agents?limit=2&cursor={cursor}",
                            headers=auth_headers())
        assert replay.status_code == 200
        assert [item["agentId"] for item in replay.json()["items"]] == ["agent.r4"]
        assert replay.json()["nextCursor"] is None


def test_unknown_route_uses_the_standard_not_found_envelope(tmp_path):
    with make_client(paginate_config(tmp_path)) as client:
        missing_route = client.get("/v1/nope", headers=auth_headers())
        assert missing_route.status_code == 404
        assert missing_route.json()["error"]["code"] == "NOT_FOUND"
