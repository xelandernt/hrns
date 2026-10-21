"""Bounded, content-addressed attachment storage outside ephemeral workspaces.

References are scoped to an owner/session namespace; a digest is not an authorization
token. Session deletion can remove its namespace without deleting shared memory.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from pydantic import Field

from harness.protocol import MAX_MESSAGE_BYTES, HarnessError, WireModel


class BlobRef(WireModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    media_type: str = "application/octet-stream"
    name: str | None = None


class FileBlobStore:
    """Host-local blobs with atomic publication and explicit namespace ownership."""

    def __init__(self, root: str | Path, *, max_bytes: int = MAX_MESSAGE_BYTES):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes

    def _directory(self, principal: str, session_id: str) -> Path:
        # Hash each opaque identity separately: neither can escape the storage root.
        owner = hashlib.sha256(principal.encode()).hexdigest()
        session = hashlib.sha256(session_id.encode()).hexdigest()
        return self.root / owner / session

    def put(
        self,
        principal: str,
        session_id: str,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        name: str | None = None,
    ) -> BlobRef:
        if len(content) > self.max_bytes:
            raise HarnessError(
                "MESSAGE_TOO_LARGE",
                "Attachment exceeds the configured storage limit",
                {"limit": self.max_bytes, "size": len(content)},
            )
        digest = hashlib.sha256(content).hexdigest()
        directory = self._directory(principal, session_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / digest
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return BlobRef(
            sha256=digest,
            size=len(content),
            media_type=media_type,
            name=Path(name).name if name else None,
        )

    def get(self, principal: str, session_id: str, ref: BlobRef) -> bytes:
        path = self._directory(principal, session_id) / ref.sha256
        try:
            # Bound reads even if the reference or on-disk file was corrupted.
            with path.open("rb") as handle:
                content = handle.read(self.max_bytes + 1)
        except FileNotFoundError as exc:
            raise HarnessError(
                "ATTACHMENT_UNAVAILABLE", "Attachment is unavailable in this session"
            ) from exc
        if (
            len(content) > self.max_bytes
            or len(content) != ref.size
            or hashlib.sha256(content).hexdigest() != ref.sha256
        ):
            raise HarnessError("ATTACHMENT_UNAVAILABLE", "Attachment failed its integrity check")
        return content

    def delete_session(self, principal: str, session_id: str) -> None:
        directory = self._directory(principal, session_id)
        if directory.exists():
            shutil.rmtree(directory)
