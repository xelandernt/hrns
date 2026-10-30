"""Live agent connection registry for embedding applications.

The registry is the single place an embedding application consults to
enumerate or revoke active connections.  Revocation is synchronous from
the caller's perspective: when a ``revoke_*`` coroutine returns, every
matching socket has been closed and its handler task has ended (or was
cancelled after the bounded 10 second grace period).  Revoked
credentials are remembered, so they cannot open new connections even if
they race with the revocation itself.

The registry stores plain metadata plus a narrow
:class:`ConnectionSocket` protocol, keeping it independent of Starlette
and of how the ASGI handler is implemented.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["ConnectionInfo", "ConnectionRegistry", "ConnectionSocket"]

#: Upper bound on how long revocation waits for handler tasks to finish.
_TASK_TIMEOUT = 10.0


@runtime_checkable
class ConnectionSocket(Protocol):
    """Minimal socket surface needed to terminate a connection."""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame and shut the underlying transport down."""
        ...


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    """Immutable snapshot of one live agent connection."""

    connection_id: str
    principal: str
    credential_id: str | None
    agent_id: str
    opened_at: int
    socket: ConnectionSocket
    task: asyncio.Task[object] | None = None


class ConnectionRegistry:
    """Track live connections so an embedding application can revoke them.

    Instances are bound to the event loop they are first used on.  All
    mutating async methods serialise through an :class:`asyncio.Lock`;
    revocation releases that lock before closing sockets or awaiting
    handler tasks, so registering new connections is never blocked on a
    slow shutdown.

    Example:
        registry = ConnectionRegistry()
        info = await registry.register(
            websocket,
            asyncio.current_task(),
            principal="alice",
            credential_id="token-1",
            agent_id="assistant",
        )
        ...
        await registry.revoke_credential("token-1")
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._connections: dict[str, ConnectionInfo] = {}
        self._principal_counts: dict[str, int] = {}
        self._revoked_credentials: set[str] = set()

    async def register(
        self,
        socket: ConnectionSocket,
        task: asyncio.Task[object] | None,
        *,
        principal: str,
        credential_id: str | None,
        agent_id: str,
    ) -> ConnectionInfo:
        """Record a live connection and return its metadata.

        Raises:
            PermissionError: If ``credential_id`` has already been revoked.
        """
        async with self._lock:
            if credential_id is not None and credential_id in self._revoked_credentials:
                raise PermissionError(f"credential {credential_id!r} has been revoked")
            info = ConnectionInfo(
                connection_id=uuid.uuid4().hex,
                principal=principal,
                credential_id=credential_id,
                agent_id=agent_id,
                opened_at=time.time_ns() // 1_000_000,
                socket=socket,
                task=task,
            )
            self._connections[info.connection_id] = info
            self._principal_counts[principal] = self._principal_counts.get(principal, 0) + 1
            return info

    def unregister(self, connection_id: str) -> None:
        """Forget a connection; safe to call more than once."""
        info = self._connections.pop(connection_id, None)
        if info is None:
            return
        remaining = self._principal_counts.get(info.principal, 1) - 1
        if remaining > 0:
            self._principal_counts[info.principal] = remaining
        else:
            self._principal_counts.pop(info.principal, None)

    def get(self, connection_id: str) -> ConnectionInfo | None:
        """Return the connection with ``connection_id``, if still live."""
        return self._connections.get(connection_id)

    def list_connections(
        self,
        *,
        principal: str | None = None,
        credential_id: str | None = None,
    ) -> tuple[ConnectionInfo, ...]:
        """Return matching connections ordered by open time, then ID."""
        selected = (
            info
            for info in self._connections.values()
            if (principal is None or info.principal == principal)
            and (credential_id is None or info.credential_id == credential_id)
        )
        return tuple(sorted(selected, key=lambda info: (info.opened_at, info.connection_id)))

    def count_for_principal(self, principal: str) -> int:
        """Return the number of live connections owned by ``principal``."""
        return self._principal_counts.get(principal, 0)

    def is_revoked(self, credential_id: str | None) -> bool:
        """Return whether ``credential_id`` has been revoked; ``None`` never is."""
        return credential_id is not None and credential_id in self._revoked_credentials

    async def revoke_credential(
        self,
        credential_id: str,
        *,
        reason: str = "CREDENTIAL_REVOKED",
        code: int = 4401,
    ) -> int:
        """Revoke a credential and close every connection using it.

        The credential is marked revoked before the connection snapshot is
        taken, so registrations racing with this call either appear in the
        snapshot or fail with :class:`PermissionError`.  Returns the number
        of connections closed.
        """
        async with self._lock:
            self._revoked_credentials.add(credential_id)
            targets = tuple(
                info for info in self._connections.values() if info.credential_id == credential_id
            )
        return await self._close_many(targets, reason=reason, code=code)

    async def revoke_principal(
        self,
        principal: str,
        *,
        reason: str = "PRINCIPAL_REVOKED",
        code: int = 4401,
    ) -> int:
        """Close every connection of ``principal``, across all credentials.

        Credentials themselves stay valid: the application decides whether
        the principal may authenticate again.  Returns the number closed.
        """
        async with self._lock:
            targets = tuple(
                info for info in self._connections.values() if info.principal == principal
            )
        return await self._close_many(targets, reason=reason, code=code)

    async def close_all(
        self,
        *,
        reason: str = "SERVER_SHUTDOWN",
        code: int = 1001,
    ) -> int:
        """Close every live connection; intended for server shutdown."""
        async with self._lock:
            targets = tuple(self._connections.values())
        return await self._close_many(targets, reason=reason, code=code)

    async def wait_closed(self, connection_id: str, timeout: float = 10.0) -> bool:
        """Wait up to ``timeout`` seconds for a handler task to finish.

        Returns ``True`` when the connection is unknown (already closed) or
        its task has ended, and ``False`` when it is still running.
        """
        info = self._connections.get(connection_id)
        if info is None:
            return True
        task = info.task
        if task is None or task.done():
            return True
        if task is asyncio.current_task():
            return False
        _, pending = await asyncio.wait({task}, timeout=timeout)
        return not pending

    async def _close_many(
        self,
        targets: tuple[ConnectionInfo, ...],
        *,
        reason: str,
        code: int,
    ) -> int:
        # Called after the registry lock is released.
        if not targets:
            return 0
        tasks = {info.task for info in targets if info.task is not None}
        try:
            await asyncio.gather(
                *(self._close_socket(info.socket, code=code, reason=reason) for info in targets)
            )
            await asyncio.gather(*(self._settle_task(task) for task in tasks))
        finally:
            for info in targets:
                self.unregister(info.connection_id)
        return len(targets)

    @staticmethod
    async def _close_socket(socket: ConnectionSocket, *, code: int, reason: str) -> None:
        with suppress(Exception):
            await socket.close(code=code, reason=reason)

    @staticmethod
    async def _settle_task(task: asyncio.Task[object]) -> None:
        if task is asyncio.current_task():
            return
        if not task.done():
            _, pending = await asyncio.wait({task}, timeout=_TASK_TIMEOUT)
            if pending:
                task.cancel()
        await asyncio.gather(task, return_exceptions=True)
