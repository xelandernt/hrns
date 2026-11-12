"""Binary frame contract for session-document synchronization.

Every frame is one WebSocket binary message: a one-byte tag followed by its
payload. JSON payloads are UTF-8. Sizes are deliberately fixed here so the
server and the published TypeScript example share one contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1

MAX_FRAME_BYTES = 4 * 1024 * 1024
SNAPSHOT_CHUNK_BYTES = 256 * 1024
PEER_QUEUE_FRAMES = 128

VERSION = 0x01
UPDATE = 0x02
SNAPSHOT = 0x03
ACK = 0x04
ERROR = 0x05

#: WebSocket close codes this contract uses.
CLOSE_SCHEMA = 1008
CLOSE_OVERFLOW = 1013
CLOSE_PERSISTENCE = 1011


class VersionPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: int = Field(alias="schemaVersion", serialization_alias="schemaVersion")
    vv: str
    session_id: str = Field(default="", alias="sessionId", serialization_alias="sessionId")
    peer_id: int | None = Field(default=None, alias="peerId", serialization_alias="peerId")


class AckPayload(BaseModel):
    durable: bool
    seq: int


class ErrorPayload(BaseModel):
    code: str
    message: str


class SnapshotHeader(BaseModel):
    index: int
    count: int


@dataclass(frozen=True)
class VersionFrame:
    payload: VersionPayload


@dataclass(frozen=True)
class UpdateFrame:
    data: bytes


@dataclass(frozen=True)
class SnapshotFrame:
    index: int
    count: int
    data: bytes


@dataclass(frozen=True)
class AckFrame:
    payload: AckPayload


@dataclass(frozen=True)
class ErrorFrame:
    payload: ErrorPayload


Frame: TypeAlias = VersionFrame | UpdateFrame | SnapshotFrame | AckFrame | ErrorFrame


class ProtocolError(ValueError):
    """A frame violated the wire contract."""


def _json(payload: BaseModel) -> bytes:
    return payload.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")


def encode(frame: Frame) -> bytes:
    if isinstance(frame, VersionFrame):
        return bytes([VERSION]) + _json(frame.payload)
    if isinstance(frame, UpdateFrame):
        return bytes([UPDATE]) + frame.data
    if isinstance(frame, SnapshotFrame):
        header = SnapshotHeader(index=frame.index, count=frame.count).model_dump_json()
        encoded = header.encode("utf-8")
        # u32 header length keeps the chunk framing unambiguous.
        return bytes([SNAPSHOT]) + len(encoded).to_bytes(4, "big") + encoded + frame.data
    if isinstance(frame, AckFrame):
        return bytes([ACK]) + _json(frame.payload)
    return bytes([ERROR]) + _json(frame.payload)


def decode(data: bytes) -> Frame:
    if not data:
        raise ProtocolError("empty frame")
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds the maximum size")
    tag, payload = data[0], data[1:]
    try:
        if tag == VERSION:
            return VersionFrame(VersionPayload.model_validate_json(payload))
        if tag == UPDATE:
            return UpdateFrame(payload)
        if tag == SNAPSHOT:
            if len(payload) < 4:
                raise ProtocolError("snapshot frame has no header")
            header_length = int.from_bytes(payload[:4], "big")
            header = SnapshotHeader.model_validate_json(payload[4 : 4 + header_length])
            if header.count <= 0 or header.index < 0 or header.index >= header.count:
                raise ProtocolError("snapshot header is invalid")
            return SnapshotFrame(header.index, header.count, payload[4 + header_length :])
        if tag == ACK:
            return AckFrame(AckPayload.model_validate_json(payload))
        if tag == ERROR:
            return ErrorFrame(ErrorPayload.model_validate_json(payload))
    except ValueError as exc:
        raise ProtocolError(str(exc)) from exc
    raise ProtocolError(f"unknown frame tag {tag}")


def snapshot_frames(snapshot: bytes) -> list[SnapshotFrame]:
    chunks = [
        snapshot[offset : offset + SNAPSHOT_CHUNK_BYTES]
        for offset in range(0, len(snapshot), SNAPSHOT_CHUNK_BYTES)
    ] or [b""]
    count = len(chunks)
    return [SnapshotFrame(index, count, chunk) for index, chunk in enumerate(chunks)]


__all__ = [
    "ACK",
    "CLOSE_OVERFLOW",
    "CLOSE_PERSISTENCE",
    "CLOSE_SCHEMA",
    "ERROR",
    "MAX_FRAME_BYTES",
    "PEER_QUEUE_FRAMES",
    "PROTOCOL_VERSION",
    "SNAPSHOT",
    "SNAPSHOT_CHUNK_BYTES",
    "UPDATE",
    "VERSION",
    "AckFrame",
    "AckPayload",
    "ErrorFrame",
    "ErrorPayload",
    "Frame",
    "ProtocolError",
    "SnapshotFrame",
    "SnapshotHeader",
    "UpdateFrame",
    "VersionFrame",
    "VersionPayload",
    "decode",
    "encode",
    "snapshot_frames",
]
