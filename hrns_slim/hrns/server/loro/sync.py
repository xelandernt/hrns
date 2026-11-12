"""Transport-neutral peer synchronization for one session document.

Each connected peer exchanges a version vector, receives only the operations it
is missing, and then receives live local updates. Imported updates are fanned out
to the other peers, persisted before an acknowledgment is sent, and never trigger
execution by themselves. Slow peers are disconnected rather than blocking the
executor.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from loro import VersionVector

from .document import SessionDocument
from .protocol import (
    CLOSE_OVERFLOW,
    CLOSE_PERSISTENCE,
    CLOSE_SCHEMA,
    MAX_FRAME_BYTES,
    PEER_QUEUE_FRAMES,
    AckFrame,
    AckPayload,
    ErrorFrame,
    ErrorPayload,
    Frame,
    ProtocolError,
    SnapshotFrame,
    UpdateFrame,
    VersionFrame,
    VersionPayload,
    decode,
    encode,
    snapshot_frames,
)


class SyncTransport(Protocol):
    """The small surface a WebSocket adapter must provide."""

    async def send(self, data: bytes) -> None: ...
    async def receive(self) -> bytes | None: ...
    async def close(self, code: int = 1000, message: str = "") -> None: ...


@dataclass(eq=False)
class _Peer:
    transport: SyncTransport
    peer_id: int | None = None
    queue: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=PEER_QUEUE_FRAMES)
    )
    writer: asyncio.Task[None] | None = None
    snapshot: dict[int, bytes] = field(default_factory=dict)
    snapshot_count: int = 0
    closed: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionSync:
    """Synchronize one document with any number of connected peers."""

    def __init__(
        self,
        document: SessionDocument,
        *,
        persist: Callable[[], Awaitable[None]],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._document = document
        self._persist = persist
        self._on_change = on_change or (lambda: None)
        self._peers: set[_Peer] = set()
        self._seq = 0
        self._closed = False
        self._subscription = document.subscribe_local_updates(self._local_update)

    async def connect(self, transport: SyncTransport) -> None:
        if self._closed:
            await transport.close(CLOSE_SCHEMA, "session sync is closed")
            return
        peer = _Peer(transport)
        self._peers.add(peer)
        peer.writer = asyncio.create_task(self._write_loop(peer))
        try:
            await self._handshake(peer)
            await self._read_loop(peer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            await self._drop(peer, close=True)

    async def close(self) -> None:
        self._closed = True
        self._subscription.unsubscribe()
        for peer in tuple(self._peers):
            await self._drop(peer, close=True)

    # -- handshake ------------------------------------------------------------

    async def _handshake(self, peer: _Peer) -> None:
        version = base64.b64encode(self._document.version()).decode("ascii")
        self._send(
            peer,
            VersionFrame(
                VersionPayload(
                    schema_version=self._document.schema_version,
                    vv=version,
                    session_id=self._document.session_metadata().session_id,
                    peer_id=self._document.doc.peer_id,
                )
            ),
        )
        frame = await self._receive(peer)
        if not isinstance(frame, VersionFrame):
            await self._fail(peer, CLOSE_SCHEMA, "expected a version frame")
            return
        payload = frame.payload
        if payload.schema_version != self._document.schema_version:
            await self._fail(peer, CLOSE_SCHEMA, "unsupported document schema version")
            return
        metadata = self._document.session_metadata()
        if payload.session_id and payload.session_id != metadata.session_id:
            await self._fail(peer, CLOSE_SCHEMA, "session id does not match")
            return
        if payload.peer_id is not None:
            if payload.peer_id == self._document.doc.peer_id:
                await self._fail(peer, CLOSE_SCHEMA, "peer id collides with the server")
                return
            if any(other is not peer and other.peer_id == payload.peer_id for other in self._peers):
                await self._fail(peer, CLOSE_SCHEMA, "peer id is already connected")
                return
            peer.peer_id = payload.peer_id
        await self._send_missing(peer, payload)

    async def _send_missing(self, peer: _Peer, payload: VersionPayload) -> None:
        if not payload.vv:
            for frame in snapshot_frames(self._document.snapshot()):
                self._send(peer, frame)
            return
        try:
            version = VersionVector.decode(base64.b64decode(payload.vv))
        except ValueError:
            await self._fail(peer, CLOSE_SCHEMA, "version vector is not base64")
            return
        if version == VersionVector():
            for frame in snapshot_frames(self._document.snapshot()):
                self._send(peer, frame)
            return
        try:
            update = self._document.export_updates(version.encode())
        except Exception:
            await self._fail(peer, CLOSE_SCHEMA, "version vector is not known to this document")
            return
        if update:
            self._send_update(peer, update)

    # -- peer loop ------------------------------------------------------------

    async def _read_loop(self, peer: _Peer) -> None:
        while not peer.closed and not self._closed:
            frame = await self._receive(peer)
            if frame is None:
                return
            if isinstance(frame, UpdateFrame):
                await self._accept(peer, frame.data)
            elif isinstance(frame, SnapshotFrame):
                peer.snapshot[frame.index] = frame.data
                if peer.snapshot_count and peer.snapshot_count != frame.count:
                    peer.snapshot.clear()
                peer.snapshot_count = frame.count
                if len(peer.snapshot) == frame.count:
                    data = b"".join(peer.snapshot[index] for index in range(frame.count))
                    peer.snapshot.clear()
                    await self._accept(peer, data)
            elif isinstance(frame, VersionFrame):
                await self._send_missing(peer, frame.payload)
            elif isinstance(frame, ErrorFrame):
                return
            else:
                continue

    async def _accept(self, peer: _Peer, data: bytes) -> None:
        before = self._document.session_metadata()
        try:
            self._document.import_update(data, origin=f"peer:{peer.peer_id}")
            after = self._document.validate()
        except Exception:
            self._document.mark_poisoned()
            await self._fail(peer, CLOSE_SCHEMA, "update does not match this document")
            return
        if after.agent_id != before.agent_id or after.session_id != before.session_id:
            self._document.mark_poisoned()
            await self._fail(peer, CLOSE_SCHEMA, "update changed session identity")
            return
        # Fan out imported state to the other replicas; never back to the sender.
        for other in tuple(self._peers):
            if other is not peer:
                self._send_update(other, data)
        try:
            await self._persist()
        except Exception:
            await self._fail(peer, CLOSE_PERSISTENCE, "could not persist the update")
            return
        self._seq += 1
        self._send(peer, AckFrame(AckPayload(durable=True, seq=self._seq)))
        self._on_change()

    # -- delivery -------------------------------------------------------------

    def _local_update(self, data: bytes) -> bool:
        for peer in tuple(self._peers):
            self._send_update(peer, data)
        return True

    def _send_update(self, peer: _Peer, data: bytes) -> None:
        """Keep outbound updates within the frame budget; snapshot chunks otherwise."""
        if len(data) + 1 <= MAX_FRAME_BYTES:
            self._send(peer, UpdateFrame(data))
            return
        for frame in snapshot_frames(self._document.snapshot()):
            self._send(peer, frame)

    def _send(self, peer: _Peer, frame: Frame) -> None:
        if peer.closed:
            return
        try:
            peer.queue.put_nowait(encode(frame))
        except asyncio.QueueFull:
            asyncio.create_task(self._drop(peer, code=CLOSE_OVERFLOW))

    async def _write_loop(self, peer: _Peer) -> None:
        try:
            while True:
                data = await peer.queue.get()
                async with peer.send_lock:
                    await peer.transport.send(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._drop(peer, close=False)

    async def _receive(self, peer: _Peer) -> Frame | None:
        try:
            data = await peer.transport.receive()
        except Exception:
            return None
        if data is None:
            return None
        if len(data) > MAX_FRAME_BYTES:
            await self._fail(peer, CLOSE_SCHEMA, "frame too large")
            return None
        try:
            return decode(data)
        except ProtocolError:
            await self._fail(peer, CLOSE_SCHEMA, "malformed frame")
            return None

    async def _fail(self, peer: _Peer, code: int, message: str) -> None:
        if not peer.closed:
            try:
                async with peer.send_lock:
                    await peer.transport.send(
                        encode(ErrorFrame(ErrorPayload(code=str(code), message=message)))
                    )
            except Exception:
                pass
        await self._drop(peer, code=code, message=message)

    async def _drop(
        self, peer: _Peer, *, close: bool = True, code: int = 1000, message: str = ""
    ) -> None:
        if peer.closed:
            return
        peer.closed = True
        self._peers.discard(peer)
        writer, peer.writer = peer.writer, None
        if writer is not None and writer is not asyncio.current_task():
            writer.cancel()
            try:
                await writer
            except (asyncio.CancelledError, Exception):
                pass
        if close:
            try:
                await peer.transport.close(code=code, message=message)
            except Exception:
                pass


__all__ = ["SessionSync", "SyncTransport"]
