"""Shared helpers for ``tests/test_server_*.py``.

Not a test module itself (leading underscore).  Provides TOML serialization
for generated deployment configs, a fake-worker argv builder, TestClient
construction with deterministic loopback/remote peer addresses, and small
assertion helpers for the WebSocket contract.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from harness_server import sha256_hex
from harness_server.config import HarnessServerConfig, load_config
from starlette.testclient import TestClient

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
EXAMPLES = REPO_ROOT / "examples"
FAKE_HOST = TESTS_DIR / "_fake_hosts.py"

TOKEN_ALICE = "alice-token-3f8b0e6a-test-only"
TOKEN_BOB = "bob-token-c41d90aa-test-only"
TOKEN_MALLORY = "mallory-unassigned-token"

LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.5", 50000)


# ---------------------------------------------------------------------------
# Minimal TOML writer for the configuration subset used by the server.
# ---------------------------------------------------------------------------


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML test value: {value!r}")


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) for item in value
    )


def _toml_table(
    path: tuple[str, ...], table: dict[str, Any], lines: list[str], *, array: bool = False
) -> None:
    dotted = ".".join(path)
    lines.append(f"[[{dotted}]]" if array else f"[{dotted}]")
    for key, value in table.items():
        if value is None:
            continue
        if isinstance(value, dict) or _is_table_array(value):
            continue
        lines.append(f"{key} = {_toml_scalar(value)}")
    for key, value in table.items():
        if value is None:
            continue
        if isinstance(value, dict):
            _toml_table((*path, key), value, lines)
        elif _is_table_array(value):
            for item in value:
                _toml_table((*path, key), item, lines, array=True)


def toml_dump(document: dict[str, Any]) -> str:
    lines: list[str] = []
    # Root scalar keys first (none are used today but keep it general).
    for key, value in document.items():
        if value is None:
            continue
        if isinstance(value, dict) or _is_table_array(value):
            continue
        lines.append(f"{key} = {_toml_scalar(value)}")
    for key, value in document.items():
        if value is None:
            continue
        if isinstance(value, dict):
            _toml_table((key,), value, lines)
        elif _is_table_array(value):
            for item in value:
                _toml_table((key,), item, lines, array=True)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


def fake_host_argv(*args: str) -> list[str]:
    return [sys.executable, str(FAKE_HOST), *args]


def deployment(**overrides: Any) -> dict[str, Any]:
    """Base fake deployment, overridable field by field."""

    data: dict[str, Any] = {
        "id": "fake.acp.r1",
        "name": "Fake ACP Agent",
        "version": "1.0.0",
        "description": "Deterministic fake worker used by server tests.",
        "command": fake_host_argv("--mode", "echo"),
        "allowed_cwds": ["/workspace/fake"],
        "default_cwd": "/workspace/fake",
        "env": {"FAKE_RESOURCE": "resource-value"},
        "env_from_environment": ["FAKE_MODEL_KEY"],
    }
    data.update(overrides)
    return data


def principal(
    principal_id: str = "alice",
    *,
    token: str = TOKEN_ALICE,
    agents: Iterable[str] = ("fake.acp.r1",),
) -> dict[str, Any]:
    return {
        "id": principal_id,
        "token_hashes": [sha256_hex(token)],
        "allowed_agent_ids": list(agents),
    }


def config_document(
    tmp_path: Path,
    *,
    deployments: list[dict[str, Any]] | None = None,
    principals: list[dict[str, Any]] | None = None,
    server: dict[str, Any] | None = None,
) -> dict[str, Any]:
    server_settings: dict[str, Any] = {"store_path": str(tmp_path / "store")}
    server_settings.update(server or {})
    return {
        "server": server_settings,
        "deployment": deployments if deployments is not None else [deployment()],
        "principal": principals if principals is not None else [principal()],
    }


def write_config(tmp_path: Path, document: dict[str, Any], name: str = "deployments.toml") -> Path:
    path = tmp_path / name
    path.write_text(toml_dump(document), encoding="utf-8")
    return path


def load_test_config(
    tmp_path: Path,
    *,
    deployments: list[dict[str, Any]] | None = None,
    principals: list[dict[str, Any]] | None = None,
    server: dict[str, Any] | None = None,
) -> HarnessServerConfig:
    document = config_document(
        tmp_path,
        deployments=deployments,
        principals=principals,
        server=server,
    )
    return load_config(write_config(tmp_path, document))


def make_client(config: HarnessServerConfig, *, remote: bool = False) -> TestClient:
    from harness_server import create_app

    app = create_app(config)
    return TestClient(app, client=REMOTE if remote else LOOPBACK)


# ---------------------------------------------------------------------------
# HTTP / WebSocket helpers
# ---------------------------------------------------------------------------


def auth_headers(token: str = TOKEN_ALICE) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def ws_path(agent_id: str = "fake.acp.r1") -> str:
    return f"/v1/agents/{agent_id}/acp"


def recv_json(ws) -> dict[str, Any]:
    message = ws.receive()
    assert "text" in message, f"expected a text frame, got {message!r}"
    return json.loads(message["text"])


def expect_close(ws, code: int | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 10.0
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "timed out waiting for a close frame"
        message = ws.receive()
        if message["type"] == "websocket.close":
            if code is not None:
                assert message["code"] == code, message
            return message
        if message["type"] == "websocket.send":
            continue  # drain frames that raced ahead of the close
        raise AssertionError(f"unexpected websocket message: {message!r}")


def denial_envelope(exc: Exception) -> tuple[int, dict[str, Any]]:
    from starlette.testclient import WebSocketDenialResponse

    assert isinstance(exc, WebSocketDenialResponse), f"unexpected exception: {exc!r}"
    payload = json.loads(exc.content.decode("utf-8"))
    return exc.status_code, payload


def error_body(response) -> dict[str, Any]:
    payload = response.json()
    assert set(payload) == {"error"}, payload
    error = payload["error"]
    assert {"code", "message"} <= set(error), error
    return error


def wait_for_pidfile(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.02)
    raise AssertionError(f"pidfile {path} was not written within {timeout}s")


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - never in tests
        return True
    return True


def wait_for_dead(pid: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"process {pid} was still alive after {timeout}s")
