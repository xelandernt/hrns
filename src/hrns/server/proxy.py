"""Bounded ACP-over-WebSocket transport for an in-process runtime host."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState

from hrns.protocol import HrnsError, decode_frame, encode_frame
from hrns.runtime.cli import serve_agent
from hrns.runtime.host import HrnsACPAgent

from .errors import ACP_SUBPROTOCOL, MAX_MESSAGE_BYTES, ApiError
from .models import Principal

if TYPE_CHECKING:
    from .app import AgentServer

logger = logging.getLogger(__name__)


async def _close(socket: WebSocket, code: int = 1000, reason: str = "") -> None:
    if (
        socket.application_state == WebSocketState.CONNECTED
        and socket.client_state != WebSocketState.DISCONNECTED
    ):
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await socket.close(code=code, reason=reason)


async def _deny(socket: WebSocket, error: ApiError) -> None:
    response = JSONResponse(error.envelope(), status_code=error.status_code, headers=error.headers)
    try:
        await socket.send_denial_response(response)
    except RuntimeError:
        await socket.close(
            code={404: 4404, 503: 4503}.get(error.status_code, 4455), reason=error.code
        )


class _ReadFlowControl(asyncio.Transport):
    """Let StreamReader pause the ASGI receive pump when its buffer fills."""

    def __init__(self) -> None:
        super().__init__()
        self.ready = asyncio.Event()
        self.ready.set()

    def pause_reading(self) -> None:
        self.ready.clear()

    def resume_reading(self) -> None:
        self.ready.set()


class _SocketWriter:
    """The ACP SDK writes one newline-framed message then awaits drain()."""

    def __init__(self, socket: WebSocket, reader: asyncio.StreamReader) -> None:
        self.socket = socket
        self.reader = reader
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        if len(self.buffer) + len(data) > MAX_MESSAGE_BYTES + 1:
            error = HrnsError("MESSAGE_TOO_LARGE", "ACP messages may not exceed 16 MiB")
            self.reader.set_exception(error)
            raise error
        self.buffer.extend(data)

    async def drain(self) -> None:
        try:
            while b"\n" in self.buffer:
                line, _, remaining = self.buffer.partition(b"\n")
                self.buffer = bytearray(remaining)
                decode_frame(bytes(line))
                await asyncio.wait_for(self.socket.send_text(line.decode("utf-8")), timeout=30)
        except Exception as exc:
            # A failed SDK send must also wake its receive loop; otherwise a
            # stalled connection could retain its session ownership indefinitely.
            self.reader.set_exception(exc)
            raise


async def _receive(socket: WebSocket, reader: asyncio.StreamReader, flow: _ReadFlowControl) -> None:
    try:
        while True:
            await flow.ready.wait()
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                await socket.close(code=1003, reason="Text frames are required")
                return
            # Normalization removes literal newlines used as JSON whitespace, so
            # one WebSocket frame remains exactly one SDK input line.
            frame = encode_frame(decode_frame(text)).encode("utf-8") + b"\n"
            reader.feed_data(frame)
    finally:
        reader.feed_eof()


async def _serve(socket: WebSocket, agent: HrnsACPAgent) -> None:
    flow = _ReadFlowControl()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    reader.set_transport(flow)
    tasks = [
        asyncio.create_task(_receive(socket, reader, flow)),
        asyncio.create_task(
            serve_agent(agent, input_stream=_SocketWriter(socket, reader), output_stream=reader)
        ),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        reader.feed_eof()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def acp_websocket(
    server: AgentServer, socket: WebSocket, agent_id: str, principal: Principal
) -> None:
    try:
        agent = server.get_agent(agent_id, principal)
        if ACP_SUBPROTOCOL not in socket.scope.get("subprotocols", []):
            raise ApiError("INVALID_REQUEST", f"The {ACP_SUBPROTOCOL} subprotocol is required")
        async with server.connection(socket, agent, principal) as host:
            await socket.accept(subprotocol=ACP_SUBPROTOCOL)
            await _serve(socket, host)
    except ApiError as exc:
        await _deny(socket, exc)
    except WebSocketDisconnect:
        pass
    except HrnsError as exc:
        code = 1009 if exc.code == "MESSAGE_TOO_LARGE" else 1002
        await _close(socket, code=code, reason=exc.code)
    except TimeoutError:
        await _close(socket, code=4512, reason="OUTBOUND_STALLED")
    except asyncio.CancelledError:
        # ASGI cancellation means this connection is gone; its runtime was
        # already released by the connection context manager.
        pass
    except Exception:
        logger.exception("ACP connection failed for agent %s", agent_id)
        await _close(socket, code=1011, reason="INTERNAL_ERROR")
    finally:
        await _close(socket)
