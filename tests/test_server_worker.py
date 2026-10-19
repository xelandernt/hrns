"""Worker process mechanics tested directly, without HTTP or WebSocket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from _server_helpers import (
    deployment,
    fake_host_argv,
    wait_for_dead,
    wait_for_pidfile,
)
from harness_server.config import DeploymentConfig, ServerSettings
from harness_server.worker import WorkerRegistry, WorkerSession, WorkerSpawnError


@pytest.fixture(autouse=True)
def _resource_environment(monkeypatch):
    # Deployment configs reference this resource variable from the host env.
    monkeypatch.setenv("FAKE_MODEL_KEY", "model-ref-value")


def _deployment(*args: str, **overrides) -> DeploymentConfig:
    data = deployment(command=fake_host_argv(*args), **overrides)
    return DeploymentConfig.model_validate(data)


def _settings(**overrides) -> ServerSettings:
    base = {
        "worker_term_grace_seconds": 0.5,
        "worker_kill_grace_seconds": 0.5,
        "stderr_tail_bytes": 4096,
    }
    base.update(overrides)
    return ServerSettings(**base)


def test_spawn_missing_program_is_reported() -> None:
    deployment_config = DeploymentConfig.model_validate(
        deployment(command=["definitely-not-a-real-harness-host-xyz"])
    )
    session = WorkerSession(
        deployment=deployment_config,
        principal_id="alice",
        store_path="/tmp/store",
        settings=_settings(),
    )
    with pytest.raises(WorkerSpawnError):
        asyncio.run(session.start())


def test_spawn_missing_cwd_is_reported(tmp_path: Path) -> None:
    deployment_config = _deployment(cwd=str(tmp_path / "missing-dir"))
    session = WorkerSession(
        deployment=deployment_config,
        principal_id="alice",
        store_path="/tmp/store",
        settings=_settings(),
    )
    with pytest.raises(WorkerSpawnError):
        asyncio.run(session.start())


def test_aclose_is_idempotent_and_reaps_the_process(tmp_path: Path) -> None:
    pidfile = tmp_path / "worker.pid"
    deployment_config = _deployment("--mode", "stay", "--pidfile", str(pidfile))

    async def scenario() -> int:
        session = WorkerSession(
            deployment=deployment_config,
            principal_id="alice",
            store_path=str(tmp_path / "store"),
            settings=_settings(),
        )
        await session.start()
        assert session.pid is not None
        pid = session.pid
        await session.aclose()
        await session.aclose()
        assert session.process is not None
        assert session.process.returncode is not None
        return pid

    pid = asyncio.run(scenario())
    wait_for_dead(pid)
    wait_for_pidfile(pidfile)


def test_registry_shutdown_reaps_every_worker(tmp_path: Path) -> None:
    pids: list[int] = []

    async def scenario() -> None:
        settings = _settings()
        registry = WorkerRegistry()
        for index in range(3):
            session = WorkerSession(
                deployment=_deployment("--mode", "stay"),
                principal_id=f"principal-{index}",
                store_path=str(tmp_path / "store"),
                settings=settings,
            )
            await session.start()
            registry.add(session)
            assert session.pid is not None
            pids.append(session.pid)

        assert len(registry) == 3
        await registry.shutdown_all(settings)
        assert len(registry) == 0

    asyncio.run(scenario())
    for pid in pids:
        wait_for_dead(pid)


def test_stderr_tail_is_bounded_and_retained(tmp_path: Path) -> None:
    async def scenario() -> WorkerSession:
        session = WorkerSession(
            deployment=_deployment("--noise-stderr"),
            principal_id="alice",
            store_path=str(tmp_path / "store"),
            settings=_settings(stderr_tail_bytes=32),
        )
        await session.start()
        assert session.process is not None
        # Give the drain a moment to observe stderr before teardown.
        await asyncio.sleep(0.1)
        await session.aclose()
        return session

    session = asyncio.run(scenario())
    tail = session.stderr_tail
    # The bounded ring keeps only the newest bytes; the declared secret is at
    # the start of the line and must already have been discarded.
    assert b"diagnostic: token=" not in tail
    assert len(tail) <= 32
    assert session.stderr_truncated


# ---------------------------------------------------------------------------
# Bounded relay behaviour under a stalled consumer
# ---------------------------------------------------------------------------


class _RecordingSlowSocket:
    """Minimal WebSocket stand-in whose sender never completes."""

    def __init__(self) -> None:
        self.close_calls: list[tuple[int, str]] = []
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        await asyncio.sleep(3600)
        self.sent.append(data)  # pragma: no cover - never reached

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))


class _UnbufferedProcess:
    """Stand-in exposing only what ``_pump_worker_frames`` reads."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.stdout = reader
        self.stdin = None
        self.returncode = None
        self.pid = 0


def test_slow_websocket_consumer_is_disconnected_with_stall_code(tmp_path: Path) -> None:
    async def scenario() -> _RecordingSlowSocket:
        session = WorkerSession(
            deployment=_deployment(),
            principal_id="alice",
            store_path=str(tmp_path / "store"),
            settings=_settings(
                outbound_queue_limit=8,
                outbound_stall_timeout_seconds=0.2,
            ),
        )
        reader = asyncio.StreamReader()
        for index in range(40):
            reader.feed_data(
                json.dumps(
                    {"jsonrpc": "2.0", "method": "_fake/flood", "params": {"i": index}}
                ).encode()
                + b"\n"
            )
        reader.feed_eof()
        session.process = _UnbufferedProcess(reader)  # type: ignore[assignment]
        ws = _RecordingSlowSocket()
        await asyncio.wait_for(session._pump_worker_frames(ws), timeout=5.0)
        return ws

    ws = asyncio.run(scenario())
    assert ws.close_calls, "the stalled consumer should have been closed"
    assert ws.close_calls[0][0] == 4512


class _HangingStdin:
    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(3600)

    def close(self) -> None:  # pragma: no cover - not used by the pump
        pass

    def is_closing(self) -> bool:  # pragma: no cover - not used by the pump
        return False


class _ScriptedSocket:
    """Yields a fixed script of receive messages, then blocks."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)
        self.close_calls: list[tuple[int, str]] = []

    async def receive(self) -> dict:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))


def test_worker_that_stops_reading_stdin_triggers_stall_close(tmp_path: Path) -> None:
    async def scenario() -> _ScriptedSocket:
        session = WorkerSession(
            deployment=_deployment(),
            principal_id="alice",
            store_path=str(tmp_path / "store"),
            settings=_settings(outbound_stall_timeout_seconds=0.2),
        )
        session.process = _UnbufferedProcess(asyncio.StreamReader())  # type: ignore[assignment]
        session.process.stdin = _HangingStdin()  # type: ignore[attr-defined]
        ws = _ScriptedSocket(
            [
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
                    ),
                }
            ]
        )
        await asyncio.wait_for(session._pump_client_frames(ws), timeout=5.0)
        return ws

    ws = asyncio.run(scenario())
    assert ws.close_calls
    assert ws.close_calls[0][0] == 4512


def test_worker_invalid_json_closes_protocol_error(tmp_path: Path) -> None:
    async def scenario() -> _RecordingSlowSocket:
        session = WorkerSession(
            deployment=_deployment(),
            principal_id="alice",
            store_path=str(tmp_path / "store"),
            settings=_settings(),
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"this is not json\n")
        reader.feed_eof()
        session.process = _UnbufferedProcess(reader)  # type: ignore[assignment]
        ws = _RecordingSlowSocket()
        await asyncio.wait_for(session._pump_worker_frames(ws), timeout=5.0)
        return ws

    ws = asyncio.run(scenario())
    assert ws.close_calls
    assert ws.close_calls[0][0] == 1002
