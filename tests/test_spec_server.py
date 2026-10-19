"""Independent spec checks for server transport and worker lifecycle gaps.

These probes are deliberately separate from the shipped suites.  They cover
CONCEPT.md obligations that those suites only appeared to exercise:

* section 11.2: every WebSocket text message carries exactly one *JSON-RPC 2.0*
  object in both directions (worker output is not merely "some JSON object"),
  finite JSON numbers, and one stdio line per frame;
* section 11.2 / docs/SERVER.md "Worker lifecycle": disconnecting removes the
  worker process *group*, including children left behind when the group leader
  exits cleanly;
* section 11.2/11.3: the HTTP error envelope always carries structured details;
* section 14.1: a non-URI ``store_url`` is rejected rather than silently
  becoming a SQLite path inside the worker.
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import time

import pytest
from _server_helpers import (
    TOKEN_ALICE,
    TOKEN_MALLORY,
    auth_headers,
    deployment,
    expect_close,
    load_test_config,
    make_client,
    recv_json,
    wait_for_dead,
    wait_for_pidfile,
    ws_path,
)
from harness_protocol import HarnessError, decode_frame
from harness_server.config import ConfigError
from harness_server.errors import ACP_SUBPROTOCOL


@pytest.fixture(autouse=True)
def _resource_environment(monkeypatch):
    # The base deployment references this server-side resource variable.
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")


INVALID_FRAME_WORKER = textwrap.dedent(
    """
    import sys

    sys.stdout.write(sys.argv[1] + "\\n")
    sys.stdout.flush()
    for _ in sys.stdin.buffer:
        pass
    """
)

ORPHANING_WORKER = textwrap.dedent(
    """
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))

    # Exit as soon as the server closes stdin.  The child stays in the worker's
    # process group but is not waited for by the leader.
    while sys.stdin.buffer.readline():
        pass
    """
)


def connect(client, agent: str = "fake.acp.r1"):
    return client.websocket_connect(
        ws_path(agent),
        subprotocols=[ACP_SUBPROTOCOL],
        headers=auth_headers(TOKEN_ALICE),
    )


def wait_until_idle(client, timeout: float = 10.0) -> None:
    state = client.app.state.harness
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(state.registry) == 0 and state.slots.active == 0:
            return
        time.sleep(0.02)
    pytest.fail(
        f"server did not quiesce: registry={len(state.registry)} "
        f"slots={state.slots.active}"
    )


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"jsonrpc":"2.0","method":"x","method":"y"}',
        '{"jsonrpc":"2.0","method":"x","params":null}',
    ],
    ids=["not-json-rpc", "duplicate-key", "null-params"],
)
def test_worker_output_must_be_one_complete_json_rpc_object(tmp_path, payload) -> None:
    script = tmp_path / "invalid_worker.py"
    script.write_text(INVALID_FRAME_WORKER, encoding="utf-8")
    config = load_test_config(
        tmp_path,
        deployments=[
            deployment(cwd=str(tmp_path), command=[sys.executable, str(script), payload])
        ],
    )
    with make_client(config) as client:
        with connect(client) as ws:
            expect_close(ws, 1002)
        wait_until_idle(client)


@pytest.mark.parametrize(
    "raw",
    [
        '{"jsonrpc":"2.0","id":1,"method":"x","params":{"n":1e999}}',
        '{"jsonrpc":"2.0","id":1,"method":"x","params":{"n":-1e999}}',
        '{"jsonrpc":"2.0","id":1,"method":"x","params":{"nested":[1e999]}}',
    ],
)
def test_json_rpc_rejects_overflowing_non_finite_numbers(raw) -> None:
    """Finite-number enforcement covers numeric overflow, not just NaN literals."""

    with pytest.raises(HarnessError):
        decode_frame(raw)


def test_keyword_store_dsn_is_rejected_instead_of_becoming_sqlite(tmp_path) -> None:
    """A non-URI DSN cannot safely reach workers: they would treat it as a path."""

    with pytest.raises(ConfigError, match="store_url"):
        load_test_config(
            tmp_path,
            deployments=[deployment(cwd=str(tmp_path))],
            server={"store_url": "host=db.example dbname=harness"},
        )


def test_http_error_envelope_always_has_structured_details(tmp_path) -> None:
    config = load_test_config(tmp_path, deployments=[deployment(cwd=str(tmp_path))])
    with make_client(config) as client:
        response = client.get("/v1/agents", headers=auth_headers(TOKEN_MALLORY))
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "UNAUTHORIZED"
    assert error["details"] == {}


def test_pretty_printed_json_rpc_is_one_stdio_line(tmp_path) -> None:
    """Valid JSON whitespace must not be split by the worker's line framing."""

    config = load_test_config(
        tmp_path, deployments=[deployment(cwd=str(tmp_path))]
    )
    frame = '{\n  "jsonrpc": "2.0",\n  "id": 7,\n  "method": "initialize"\n}'
    with make_client(config) as client:
        with connect(client) as ws:
            ws.send_text(frame)
            reply = recv_json(ws)
            assert reply["id"] == 7
            assert reply["result"]["fakeHost"] is True
        wait_until_idle(client)


def test_clean_leader_exit_still_reaps_the_worker_process_group(tmp_path) -> None:
    script = tmp_path / "orphaning_worker.py"
    script.write_text(ORPHANING_WORKER, encoding="utf-8")
    pidfile = tmp_path / "child.pid"
    config = load_test_config(
        tmp_path,
        deployments=[
            deployment(
                cwd=str(tmp_path),
                command=[sys.executable, str(script), str(pidfile)],
            )
        ],
        server={
            "worker_term_grace_seconds": 0.4,
            "worker_kill_grace_seconds": 0.4,
        },
    )

    child_pid: int | None = None
    try:
        with make_client(config) as client:
            with connect(client) as ws:
                # The worker writes the child pid before reading stdin; keeping
                # the connection open gives it time to do so.
                child_pid = wait_for_pidfile(pidfile)
                assert ws is not None
            # The leader observes stdin EOF and exits without signalling its
            # child.  The server must still kill the remaining group.
            wait_for_dead(child_pid, timeout=10.0)
            wait_until_idle(client)
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
