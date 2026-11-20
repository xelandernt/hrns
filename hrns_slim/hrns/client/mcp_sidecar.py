"""A stdio sidecar that serves configured MCP servers over MCP-over-ACP.

The native client spawns this process and speaks newline-delimited JSON over
its stdin and stdout. The sidecar owns the FastMCP upstreams through
:class:`~hrns.client.mcp_provider.ClientMcpProvider`, so the Rust client only
has to forward ``mcp/message`` frames and relay consent prompts to its UI.

stdout is protocol-only; every log line goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from acp.exceptions import RequestError
from pydantic import JsonValue, TypeAdapter, ValidationError

from hrns.shared.acp_mcp import MCP_MESSAGE_METHOD, AcpChannel, outcome_to_wire
from hrns.shared.acp_mcp.core import JsonObject
from hrns.shared.config import HrnsConfig, McpServerSettings
from hrns.shared.config.mcp import server_target

from .mcp_provider import ClientMcpProvider, ConsentDecision

logger = logging.getLogger(__name__)

__all__ = ["Sidecar", "SidecarChannel", "main"]

_JSON_OBJECT = TypeAdapter(JsonObject)
_JSON_VALUE = TypeAdapter(JsonValue)
_JSON_OBJECTS = TypeAdapter(list[JsonObject])

Emit = Callable[[Mapping[str, object]], Awaitable[None]]
"""Writes one protocol line to the client."""


class SidecarChannel(AcpChannel):
    """An :class:`AcpChannel` that forwards provider traffic to stdout.

    The provider only sends request-scoped notifications, so requests are not
    supported: there is nothing in the sidecar that could answer them.
    """

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._key = object()

    @property
    def key(self) -> object:
        return self._key

    async def send_request(self, method: str, params: JsonObject) -> JsonValue:
        raise RuntimeError(f"the MCP sidecar cannot send the ACP request {method!r}")

    async def send_notification(self, method: str, params: JsonObject) -> None:
        if method != MCP_MESSAGE_METHOD:
            logger.warning("dropping provider notification %r: not an mcp/message", method)
            return
        await self._emit({"type": "notify", **params})


class Sidecar:
    """Hosts the configured MCP servers and answers the client's protocol lines."""

    def __init__(self, config: HrnsConfig) -> None:
        self._provider = ClientMcpProvider(
            config.mcp,
            consent=self._consent,
            persist=config.save,
        )
        self._channel = SidecarChannel(self._emit)
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self._output_lock = asyncio.Lock()
        self._declare_lock = asyncio.Lock()
        self._consent_ids: Iterator[int] = itertools.count(1)
        self._consent_waiters: dict[int, asyncio.Future[ConsentDecision]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """Serve until stdin closes or the client asks the sidecar to shut down."""
        self._start_stdin_pump()
        try:
            await self._provider.start(channel=self._channel)
            await self._emit({"type": "ready"})
            await self._read_loop()
        finally:
            await self._shutdown()

    # ------------------------------------------------------------- protocol

    async def _read_loop(self) -> None:
        while True:
            line = await self._incoming.get()
            if line is None:
                logger.debug("stdin closed; stopping the MCP sidecar")
                return
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = _JSON_OBJECT.validate_python(json.loads(stripped))
            except (json.JSONDecodeError, ValidationError):
                logger.warning("ignoring malformed sidecar input: %s", _preview(stripped))
                continue
            action = message.get("type")
            if not isinstance(action, str):
                logger.warning("ignoring sidecar input without a type: %s", _preview(stripped))
                continue
            if action == "shutdown":
                logger.debug("shutdown requested")
                return
            if action == "start":
                logger.debug("start accepted; the sidecar loaded its configuration at launch")
            elif action == "declare":
                self._spawn(self._declare())
            elif action == "consent":
                self._resolve_consent(message)
            elif action == "request":
                self._spawn(self._request(message))
            elif action == "notification":
                self._notification(message)
            elif action == "health":
                await self._emit({"type": "health", "servers": self._health()})
            else:
                logger.warning("ignoring unknown sidecar message type %r", action)

    async def _declare(self) -> None:
        # Serialize declarations so an impatient second request cannot start
        # the same upstream twice.
        async with self._declare_lock:
            declarations = await self._provider.declarations()
            health = self._health()
        servers = _JSON_OBJECTS.validate_python(
            [{"name": item.name, "serverId": item.server_id} for item in declarations]
        )
        await self._emit({"type": "declarations", "servers": servers, "health": health})

    async def _request(self, message: JsonObject) -> None:
        request_id = message.get("id")
        try:
            outcome = await self._provider.handle_request(message.get("params"))
        except RequestError as exc:
            await self._emit(
                {
                    "type": "outer_error",
                    "id": request_id,
                    "code": exc.code,
                    "message": str(exc),
                    "data": _wire_value(exc.data),
                }
            )
        except Exception as exc:
            logger.exception("MCP-over-ACP request %r failed", request_id)
            await self._emit(
                {
                    "type": "outer_error",
                    "id": request_id,
                    "code": -32603,
                    "message": str(exc) or type(exc).__name__,
                    "data": None,
                }
            )
        else:
            await self._emit(
                {
                    "type": "response",
                    "id": request_id,
                    "outcome": outcome_to_wire(outcome),
                }
            )

    def _notification(self, message: JsonObject) -> None:
        try:
            self._provider.handle_notification(message.get("params"))
        except Exception:
            logger.warning("ignoring malformed mcp/message notification", exc_info=True)

    async def _consent(self, name: str, server: McpServerSettings) -> ConsentDecision:
        request_id = next(self._consent_ids)
        future: asyncio.Future[ConsentDecision] = asyncio.get_running_loop().create_future()
        self._consent_waiters[request_id] = future
        await self._emit(
            {
                "type": "consent",
                "id": request_id,
                "name": name,
                "transport": server.transport,
                "target": server_target(server),
                "export": server.export,
            }
        )
        try:
            return await future
        finally:
            self._consent_waiters.pop(request_id, None)

    def _resolve_consent(self, message: JsonObject) -> None:
        raw_id = message.get("id")
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            logger.warning("ignoring consent response without an integer id")
            return
        future = self._consent_waiters.get(raw_id)
        if future is None:
            logger.warning("ignoring consent response for unknown id %r", raw_id)
            return
        if not future.done():
            future.set_result(_decision(message.get("decision")))

    def _health(self) -> list[JsonObject]:
        return _JSON_OBJECTS.validate_python([asdict(item) for item in self._provider.health()])

    # --------------------------------------------------------------- plumbing

    async def _emit(self, message: Mapping[str, object]) -> None:
        line = json.dumps(dict(message), separators=(",", ":")) + "\n"
        async with self._output_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def _start_stdin_pump(self) -> None:
        """Read stdin on a daemon thread so shutdown never waits for a read."""
        loop = asyncio.get_running_loop()

        def pump() -> None:
            try:
                while True:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    loop.call_soon_threadsafe(self._incoming.put_nowait, line)
            except (OSError, ValueError):
                pass
            finally:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._incoming.put_nowait, None)

        threading.Thread(target=pump, name="mcp-sidecar-stdin", daemon=True).start()

    def _spawn(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        if isinstance(error, BrokenPipeError):
            logger.debug("stdout closed; stopping the MCP sidecar")
            self._incoming.put_nowait(None)
            return
        logger.error("sidecar task failed", exc_info=error)

    async def _shutdown(self) -> None:
        for future in self._consent_waiters.values():
            if not future.done():
                future.set_result(ConsentDecision.DENY)
        self._consent_waiters.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._provider.close()


def _decision(value: JsonValue) -> ConsentDecision:
    if isinstance(value, str):
        try:
            return ConsentDecision(value)
        except ValueError:
            logger.warning("unknown MCP consent decision %r; denying", value)
    else:
        logger.warning("missing MCP consent decision; denying")
    return ConsentDecision.DENY


def _wire_value(value: object) -> JsonValue:
    try:
        return _JSON_VALUE.validate_python(value)
    except ValidationError:
        return str(value)


def _preview(text: str) -> str:
    return text if len(text) <= 200 else text[:197] + "..."


def _silence_stdout() -> None:
    """Point stdout at ``/dev/null`` so interpreter shutdown cannot traceback."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hrns-mcp-sidecar",
        description="Serve configured MCP servers to the hrns client over stdio.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Read one configuration file instead of merging the user and project documents.",
    )
    return parser


def _load_config(path: str | None) -> HrnsConfig:
    if path is None:
        return HrnsConfig()
    return HrnsConfig(config_file=Path(path))


async def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parser().parse_args()
    try:
        config = _load_config(args.config)
    except Exception:
        logger.exception("cannot load the hrns configuration; not exporting any MCP server")
        return 2
    sidecar = Sidecar(config)
    try:
        await sidecar.run()
    except BrokenPipeError:
        logger.debug("stdout closed; stopping the MCP sidecar")
        _silence_stdout()
    except Exception:
        logger.exception("the MCP sidecar failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
