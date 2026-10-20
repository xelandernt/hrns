"""Worker process lifecycle and the bounded WebSocket <-> stdio relay.

Each authenticated WebSocket connection gets its own worker process, launched
directly from the deployment's immutable argv (never through a shell) inside
a fresh process group.  The worker speaks newline-delimited JSON-RPC on
stdin/stdout; stderr is drained continuously into a bounded tail and is never
forwarded to clients or logged verbatim.

The worker environment is owned by the server.  Clients cannot contribute to
it: principal identity, deployment identity, allowed workspace roots and the
session-store path all come from server configuration.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any, Mapping

from harness.protocol import HarnessError, decode_frame, encode_frame

from .config import DeploymentConfig, ServerSettings
from .errors import (
    CLOSE_INVALID_PAYLOAD,
    CLOSE_MESSAGE_TOO_BIG,
    CLOSE_OUTBOUND_STALLED,
    CLOSE_PROTOCOL_ERROR,
    CLOSE_UNSUPPORTED_DATA,
    CLOSE_WORKER_EXITED,
    MAX_MESSAGE_BYTES,
)

__all__ = [
    "WorkerSpawnError",
    "WorkerSession",
    "WorkerSlots",
    "WorkerRegistry",
    "build_worker_env",
    "CONTRACT_ENV_KEYS",
]

#: Environment variables owned by the server for every worker.
CONTRACT_ENV_KEYS = (
    "HARNESS_PRINCIPAL",
    "HARNESS_DEPLOYMENT_ID",
    "HARNESS_ALLOWED_CWDS",
    "HARNESS_STORE",
    "HARNESS_HOSTED",
)

_STDIO_READER_LIMIT = MAX_MESSAGE_BYTES + 4096
_MAX_CLOSE_REASON_CHARS = 120


class WorkerSpawnError(Exception):
    """The configured worker could not be started."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})


class WorkerSlots:
    """Synchronous capacity accounting for active worker connections.

    All mutation happens on the server's single event loop, so plain integers
    are sufficient; acquisition happens before the first ``await`` in the
    WebSocket handler, which makes the limit race-free for this process.
    """

    def __init__(self, max_total: int, max_per_principal: int) -> None:
        if max_total < 1 or max_per_principal < 1:  # pragma: no cover - config guards
            raise ValueError("worker limits must be positive")
        self._max_total = max_total
        self._max_per_principal = max_per_principal
        self._total = 0
        self._per_principal: dict[str, int] = {}

    @property
    def active(self) -> int:
        return self._total

    def acquire(self, principal_id: str) -> None:
        """Reserve a slot or raise ``ApiError(SERVER_BUSY)``.

        Imported lazily to keep this module free of HTTP concerns at import
        time; the error is the same predictable envelope used elsewhere.
        """

        from .errors import ApiError

        if self._total >= self._max_total:
            raise ApiError(
                "SERVER_BUSY",
                "the server is at its worker limit; retry shortly",
                details={
                    "reason": "worker_limit",
                    "limit": self._max_total,
                    "active": self._total,
                },
                headers={"Retry-After": "1"},
            )
        current = self._per_principal.get(principal_id, 0)
        if current >= self._max_per_principal:
            raise ApiError(
                "SERVER_BUSY",
                "this principal has reached its concurrent connection limit",
                details={
                    "reason": "principal_limit",
                    "limit": self._max_per_principal,
                    "active": current,
                    "principal": principal_id,
                },
                headers={"Retry-After": "1"},
            )
        self._total += 1
        self._per_principal[principal_id] = current + 1

    def release(self, principal_id: str) -> None:
        self._total = max(0, self._total - 1)
        current = self._per_principal.get(principal_id, 0)
        if current <= 1:
            self._per_principal.pop(principal_id, None)
        else:
            self._per_principal[principal_id] = current - 1


class WorkerSession:
    """One worker process and its protocol relay for a single connection."""

    def __init__(
        self,
        *,
        deployment: DeploymentConfig,
        principal_id: str,
        store_path: str,
        settings: ServerSettings,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.deployment = deployment
        self.principal_id = principal_id
        self.store_path = store_path
        self.settings = settings
        self._environ = dict(os.environ if environ is None else environ)
        self.process: asyncio.subprocess.Process | None = None
        self.exit_reason: str | None = None
        self._stderr_tail = bytearray()
        self._stderr_truncated = False
        self._stderr_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._ws_close_sent = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def stderr_tail(self) -> bytes:
        return bytes(self._stderr_tail)

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr_truncated

    def worker_env(self) -> dict[str, str]:
        return build_worker_env(
            self.deployment,
            principal_id=self.principal_id,
            store_path=self.store_path,
            passthrough=self.settings.worker_env_passthrough,
            environ=self._environ,
        )

    async def start(self) -> None:
        """Spawn the worker process group and begin draining stderr."""

        argv = list(self.deployment.command)
        try:
            env = self.worker_env()
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.deployment.cwd,
                env=env,
                limit=_STDIO_READER_LIMIT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise WorkerSpawnError(
                "the worker executable was not found",
                details={"deployment": self.deployment.id, "program": argv[0]},
            ) from exc
        except (PermissionError, NotADirectoryError) as exc:
            raise WorkerSpawnError(
                "the worker executable or working directory is not accessible",
                details={
                    "deployment": self.deployment.id,
                    "program": argv[0],
                    "cwd": self.deployment.cwd,
                },
            ) from exc
        except OSError as exc:
            raise WorkerSpawnError(
                "the worker process could not be started",
                details={"deployment": self.deployment.id, "program": argv[0], "errno": exc.errno},
            ) from exc
        except (KeyError, ValueError) as exc:
            raise WorkerSpawnError(
                str(exc),
                details={"deployment": self.deployment.id},
            ) from exc

        self.exit_reason = None
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"harness-stderr-{self.pid}"
        )

    async def aclose(self) -> None:
        """Idempotently shut the worker down, bounded in time.

        Sequence: stdin EOF -> wait ``worker_term_grace_seconds`` -> SIGTERM
        to the whole process group -> wait ``worker_kill_grace_seconds`` ->
        SIGKILL.  A wedged worker can therefore delay shutdown by at most the
        two configured graces plus a short fixed reap window.
        """

        await asyncio.shield(self._ensure_close_task())

    async def aclose_uninterruptible(self) -> None:
        """Finish cleanup even while the calling task is being cancelled.

        ASGI servers may cancel a connection task as soon as the client
        disconnects.  The worker still needs a bounded, complete teardown, so
        cancellation is swallowed (at most once per await) until the close
        task finishes or the hard deadline expires.
        """

        task = self._ensure_close_task()
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time()
            + self.settings.worker_term_grace_seconds
            + self.settings.worker_kill_grace_seconds
            + 8.0
        )
        cancelled = False
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:  # pragma: no cover - hard last resort
                self.kill_now()
                break
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:  # pragma: no cover - hard last resort
                self.kill_now()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except (TimeoutError, asyncio.CancelledError):
                    pass
                break
            except asyncio.CancelledError:
                cancelled = True
                continue
        if cancelled:
            raise asyncio.CancelledError()

    def _ensure_close_task(self) -> asyncio.Task[None]:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_impl(), name=f"harness-worker-close-{self.deployment.id}"
            )
        return self._close_task

    def kill_now(self) -> None:
        """Synchronously SIGKILL the worker group (last-resort server shutdown)."""

        self._signal_group(signal.SIGKILL, include_orphans=True)

    async def _close_impl(self) -> None:
        process = self.process
        if process is None:
            await self._stop_stderr_pump()
            return

        if process.stdin is not None:
            try:
                if not process.stdin.is_closing():
                    process.stdin.close()
            except Exception:  # pragma: no cover - pipe already gone
                pass

        leader_exited = await self._wait_for_exit(self.settings.worker_term_grace_seconds)
        # Signal the group even when the leader exited: children that outlive
        # a clean disconnect are outside the connection's lifetime.  The group
        # may contain orphans after the leader is reaped, so liveness is checked
        # against the process group rather than ``process.returncode``.
        self._signal_group(signal.SIGTERM, include_orphans=True)
        if not leader_exited:
            self.exit_reason = self.exit_reason or "terminated"
            leader_exited = await self._wait_for_exit(
                self.settings.worker_kill_grace_seconds
            )
        group_grace = self.settings.worker_kill_grace_seconds if leader_exited else 0.0
        if not await self._wait_for_group_exit(group_grace):
            self.exit_reason = self.exit_reason or "killed"
            self._signal_group(signal.SIGKILL, include_orphans=True)
            await self._wait_for_group_exit(5.0)

        # Let the transport observe process exit and close its pipe handles
        # before the surrounding event loop can be torn down.
        await asyncio.sleep(0)
        transport = getattr(process, "_transport", None)
        if transport is not None:
            try:
                transport.close()  # idempotent; marks the transport closed
            except Exception:  # pragma: no cover - already closed
                pass
        await asyncio.sleep(0)

        await self._stop_stderr_pump()

    async def _stop_stderr_pump(self) -> None:
        task = self._stderr_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            pass

    def _signal_group(self, sig: int, *, include_orphans: bool = False) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is not None and not include_orphans:
            return
        try:
            if os.name == "posix" and hasattr(os, "killpg"):
                # start_new_session=True made the worker a process-group
                # leader, so the group id equals its pid.
                os.killpg(process.pid, sig)
            else:  # pragma: no cover - supported platforms are POSIX
                process.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _group_alive(self) -> bool:
        """Whether any process remains in the worker's process group."""

        process = self.process
        if process is None:
            return False
        if os.name == "posix" and hasattr(os, "killpg"):
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:  # pragma: no cover - other user's process
                return True
            except OSError:  # pragma: no cover - invalid/reaped group
                return False
            return True
        return process.returncode is None  # pragma: no cover - supported platforms are POSIX

    async def _wait_for_group_exit(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for every member of the worker group to exit.

        Waiting on the leader alone is insufficient: a worker may spawn children
        and exit while they remain in its process group.  Teardown must reap the
        whole group before the control claim is considered released.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            if not self._group_alive():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))

    async def _wait_for_exit(self, timeout: float) -> bool:
        process = self.process
        if process is None:
            return True
        if process.returncode is not None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=timeout)
        except TimeoutError:
            return process.returncode is not None
        return True

    async def _drain_stderr(self) -> None:
        """Continuously drain stderr into a bounded ring; never forward/log it."""

        process = self.process
        if process is None or process.stderr is None:  # pragma: no cover - defensive
            return
        capacity = self.settings.stderr_tail_bytes
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                if capacity <= 0:
                    continue
                self._stderr_tail.extend(chunk)
                overflow = len(self._stderr_tail) - capacity
                if overflow > 0:
                    del self._stderr_tail[:overflow]
                    self._stderr_truncated = True
        except (asyncio.CancelledError, Exception):
            # A dead pipe ends the drain; content is diagnostics only.
            return

    # -- protocol relay ----------------------------------------------------

    async def relay(self, ws: Any) -> None:
        """Forward ACP frames in both directions until either side ends.

        One pump task per direction runs concurrently (stderr is drained from
        the moment the process starts); the first to finish tears down the
        other, and the worker shutdown sequence runs in the caller's
        ``finally`` block.
        """

        tasks = [
            asyncio.create_task(self._pump_client_frames(ws), name="harness-ws-in"),
            asyncio.create_task(self._pump_worker_frames(ws), name="harness-ws-out"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_close(self, ws: Any, code: int, reason: str = "") -> None:
        if self._ws_close_sent:
            return
        self._ws_close_sent = True
        try:
            await ws.close(code=code, reason=reason[:_MAX_CLOSE_REASON_CHARS])
        except Exception:  # pragma: no cover - socket already gone
            pass

    async def _pump_client_frames(self, ws: Any) -> None:
        """WebSocket -> worker stdin with strict framing rules."""

        while True:
            try:
                message = await ws.receive()
            except Exception:
                self.exit_reason = self.exit_reason or "client_disconnected"
                return
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                self.exit_reason = self.exit_reason or "client_disconnected"
                return
            if message_type != "websocket.receive":  # pragma: no cover - defensive
                continue

            text = message.get("text")
            if text is None:
                # Binary frames are outside the ACP-over-WebSocket contract.
                await self._safe_close(
                    ws, CLOSE_UNSUPPORTED_DATA, "binary frames are not supported"
                )
                self.exit_reason = self.exit_reason or "binary_frame"
                return

            try:
                payload = text.encode("utf-8")
            except UnicodeEncodeError:  # pragma: no cover - Python strings encode
                await self._safe_close(ws, CLOSE_INVALID_PAYLOAD, "invalid UTF-8")
                self.exit_reason = self.exit_reason or "invalid_encoding"
                return

            if len(payload) > MAX_MESSAGE_BYTES:
                await self._safe_close(
                    ws, CLOSE_MESSAGE_TOO_BIG, "message exceeds the 16 MiB limit"
                )
                self.exit_reason = self.exit_reason or "oversize_frame"
                return

            try:
                decoded = decode_frame(text)
                # The stdio worker contract is newline-delimited, while a
                # WebSocket text frame may legally contain insignificant JSON
                # whitespace (including newlines).  Re-serialize the validated
                # object as one compact line so pretty-printed JSON-RPC cannot
                # be split by the worker's line framing.
                payload = encode_frame(decoded).encode("utf-8")
            except HarnessError as exc:
                too_large = exc.code == "MESSAGE_TOO_LARGE"
                await self._safe_close(
                    ws,
                    CLOSE_MESSAGE_TOO_BIG if too_large else CLOSE_PROTOCOL_ERROR,
                    str(exc),
                )
                self.exit_reason = self.exit_reason or (
                    "oversize_frame" if too_large else "invalid_json"
                )
                return

            process = self.process
            if process is None or process.stdin is None:  # pragma: no cover - defensive
                self.exit_reason = self.exit_reason or "worker_unavailable"
                await self._safe_close(ws, CLOSE_WORKER_EXITED, "worker is not available")
                return

            try:
                process.stdin.write(payload + b"\n")
                await asyncio.wait_for(
                    process.stdin.drain(),
                    timeout=self.settings.outbound_stall_timeout_seconds,
                )
            except TimeoutError:
                await self._safe_close(
                    ws, CLOSE_OUTBOUND_STALLED, "worker is not consuming frames"
                )
                self.exit_reason = self.exit_reason or "worker_stalled"
                return
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.exit_reason = self.exit_reason or "worker_closed_stdin"
                await self._safe_close(ws, CLOSE_WORKER_EXITED, "worker closed its input")
                return

    async def _pump_worker_frames(self, ws: Any) -> None:
        """Worker stdout -> WebSocket with bounded buffering and validation."""

        process = self.process
        if process is None or process.stdout is None:  # pragma: no cover - defensive
            return

        queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=self.settings.outbound_queue_limit
        )

        async def produce() -> None:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError:
                    # StreamReader limit tripped: the worker emitted a line
                    # larger than the transport allows without a newline.
                    await self._safe_close(
                        ws, CLOSE_MESSAGE_TOO_BIG, "worker frame exceeds the 16 MiB limit"
                    )
                    self.exit_reason = self.exit_reason or "worker_oversize_frame"
                    return
                except Exception:
                    self.exit_reason = self.exit_reason or "worker_stdout_error"
                    await self._safe_close(
                        ws, CLOSE_WORKER_EXITED, "worker output stream failed"
                    )
                    return
                if not line:
                    self.exit_reason = self.exit_reason or "worker_exited"
                    await self._safe_close(ws, CLOSE_WORKER_EXITED, "worker exited")
                    return

                stripped = line.rstrip(b"\r\n")
                if not stripped.strip():
                    continue  # tolerate blank keepalives without corrupting frames

                if len(stripped) > MAX_MESSAGE_BYTES:
                    await self._safe_close(
                        ws, CLOSE_MESSAGE_TOO_BIG, "worker frame exceeds the 16 MiB limit"
                    )
                    self.exit_reason = self.exit_reason or "worker_oversize_frame"
                    return

                try:
                    stripped.decode("utf-8")
                except UnicodeDecodeError:
                    await self._safe_close(
                        ws, CLOSE_INVALID_PAYLOAD, "worker produced invalid UTF-8"
                    )
                    self.exit_reason = self.exit_reason or "worker_invalid_utf8"
                    return

                try:
                    decode_frame(stripped)
                except HarnessError as exc:
                    too_large = exc.code == "MESSAGE_TOO_LARGE"
                    await self._safe_close(
                        ws,
                        CLOSE_MESSAGE_TOO_BIG if too_large else CLOSE_PROTOCOL_ERROR,
                        str(exc),
                    )
                    self.exit_reason = self.exit_reason or (
                        "worker_oversize_frame" if too_large else "worker_invalid_frame"
                    )
                    return

                try:
                    queue.put_nowait(stripped)
                except asyncio.QueueFull:
                    try:
                        await asyncio.wait_for(
                            queue.put(stripped),
                            timeout=self.settings.outbound_stall_timeout_seconds,
                        )
                    except TimeoutError:
                        await self._safe_close(
                            ws, CLOSE_OUTBOUND_STALLED, "client is not consuming frames"
                        )
                        self.exit_reason = self.exit_reason or "client_stalled"
                        return

        async def consume() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                try:
                    await ws.send_text(item.decode("utf-8"))
                except Exception:
                    self.exit_reason = self.exit_reason or "client_disconnected"
                    return

        producer = asyncio.create_task(produce(), name="harness-worker-stdout")
        consumer = asyncio.create_task(consume(), name="harness-ws-send")
        try:
            done, pending = await asyncio.wait(
                [producer, consumer], return_when=asyncio.FIRST_COMPLETED
            )
            if producer.done() and consumer not in done:
                # Give the sender a bounded chance to flush what was already
                # accepted, rather than dropping frames mid-stream.
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    try:
                        await asyncio.wait_for(
                            queue.put(None),
                            timeout=min(self.settings.outbound_stall_timeout_seconds, 5.0),
                        )
                    except TimeoutError:
                        pass
                try:
                    await asyncio.wait_for(
                        asyncio.shield(consumer),
                        timeout=min(self.settings.outbound_stall_timeout_seconds, 5.0),
                    )
                except TimeoutError:
                    consumer.cancel()
            for task in pending:
                task.cancel()
        finally:
            for task in (producer, consumer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)


def build_worker_env(
    deployment: DeploymentConfig,
    *,
    principal_id: str,
    store_path: str,
    passthrough: list[str],
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Build the server-owned worker environment.

    Precedence is deliberate: host passthrough (excluding the reserved
    ``HARNESS_`` namespace) < deployment resource values < referenced host
    environment values < server-owned contract variables.  Nothing here is
    derived from client-supplied ACP metadata.
    """

    env: dict[str, str] = {}
    for name in passthrough:
        if name.startswith("HARNESS_"):
            continue  # contract namespace is server-owned
        if name in environ:
            env[name] = environ[name]

    for key, value in deployment.env.items():
        env[key] = value

    for name in deployment.env_from_environment:
        if name not in environ:
            raise WorkerSpawnError(
                "a referenced resource environment variable is missing",
                details={"deployment": deployment.id, "variable": name},
            )
        env[name] = environ[name]

    env.update(
        {
            "HARNESS_PRINCIPAL": principal_id,
            "HARNESS_DEPLOYMENT_ID": deployment.id,
            "HARNESS_ALLOWED_CWDS": json.dumps(list(deployment.allowed_cwds)),
            "HARNESS_STORE": store_path,
            "HARNESS_HOSTED": "1",
        }
    )
    return env


class WorkerRegistry:
    """Tracks live worker sessions so shutdown can reap every process group."""

    def __init__(self) -> None:
        self._sessions: dict[int, WorkerSession] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def add(self, session: WorkerSession) -> None:
        self._sessions[id(session)] = session

    def discard(self, session: WorkerSession) -> None:
        self._sessions.pop(id(session), None)

    async def shutdown_all(self, settings: ServerSettings) -> None:
        """Terminate every worker within a bounded wall-clock budget."""

        sessions = list(self._sessions.values())
        if not sessions:
            return
        budget = (
            settings.worker_term_grace_seconds
            + 2 * settings.worker_kill_grace_seconds
            + 8.0
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(session.aclose() for session in sessions), return_exceptions=True
                ),
                timeout=budget,
            )
        except TimeoutError:  # pragma: no cover - defensive last resort
            for session in sessions:
                session.kill_now()
            await asyncio.sleep(0)
        finally:
            self._sessions.clear()
