"""Multiplexing ACP client for many viewers of one authoritative agent host.

A hrns host runs a single agent turn, but several WebSocket connections may be
watching it. This module splits the ACP client surface into two behaviours:

* **Session calls** -- ``session_update`` and session-scoped extension notifications --
  are delivered only to connections subscribed to that session. A local stdio host may
  fall back to its sole client before a subscription exists; a hosted actor never does.
  A viewer that fails is logged and skipped so it cannot break the turn for everyone
  else.
* **Routed calls** -- ``request_permission``, ``ext_method``, ``send_request``, and
  ``send_notification`` -- are delivered to exactly one connection. A response can only
  come from one place, so the host selects the connection that initiated the turn via
  :data:`current_connection`. Only a local stdio host falls back to its sole client;
  a hosted actor never guesses between viewers.

Every method runs on the event loop and snapshots targets before awaiting, so mutation
from a callback during an await is safe.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from acp import schema
from acp.interfaces import Agent, Client

from .request_context import current_connection_id

__all__ = ["FanoutClient", "current_connection"]

_LOG = logging.getLogger(__name__)

current_connection: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_connection", default=None
)


class FanoutClient:
    """Broadcast session updates; route request/response calls to one connection."""

    def __init__(self, *, local: bool = False) -> None:
        self._clients: dict[str, Client] = {}
        self._subscriptions: dict[str, set[str]] = {}
        self._connection_sessions: dict[str, set[str]] = {}
        self._approval_guard: Callable[[str], Awaitable[None]] | None = None
        #: A local stdio host has one client and no connection registry, so the sole
        #: client is a legitimate default. Hosted servers never use that shortcut.
        self.local = local

    def set_approval_guard(self, guard: Callable[[str], Awaitable[None]]) -> None:
        """Install the host's policy check for approval requests."""
        self._approval_guard = guard

    # -- registry -----------------------------------------------------------

    def attach(self, connection_id: str, client: Client) -> None:
        """Register a live client, replacing any previous client with the same id."""
        self._clients[connection_id] = client

    def detach(self, connection_id: str) -> None:
        """Remove a client and all of its session subscriptions."""
        self._clients.pop(connection_id, None)
        for session_id in self._connection_sessions.pop(connection_id, set()):
            subscribers = self._subscriptions.get(session_id)
            if subscribers is not None:
                subscribers.discard(connection_id)
                if not subscribers:
                    self._subscriptions.pop(session_id, None)

    def subscribe(self, connection_id: str, session_id: str) -> None:
        """Subscribe one connection to one session's update stream."""
        if connection_id not in self._clients:
            return
        self._subscriptions.setdefault(session_id, set()).add(connection_id)
        self._connection_sessions.setdefault(connection_id, set()).add(session_id)

    def unsubscribe(self, connection_id: str, session_id: str) -> None:
        """Remove one connection from one session's update stream."""
        subscribers = self._subscriptions.get(session_id)
        if subscribers is not None:
            subscribers.discard(connection_id)
            if not subscribers:
                self._subscriptions.pop(session_id, None)
        sessions = self._connection_sessions.get(connection_id)
        if sessions is not None:
            sessions.discard(session_id)
            if not sessions:
                self._connection_sessions.pop(connection_id, None)

    def unsubscribe_session(self, session_id: str) -> None:
        """Drop every subscriber of a session (after close/delete)."""
        for connection_id in self._subscriptions.pop(session_id, set()):
            sessions = self._connection_sessions.get(connection_id)
            if sessions is not None:
                sessions.discard(session_id)
                if not sessions:
                    self._connection_sessions.pop(connection_id, None)

    def subscribers(self, session_id: str) -> tuple[Client, ...]:
        """The clients currently watching a session, in attachment order."""
        ids = self._subscriptions.get(session_id, set())
        return tuple(
            client for connection_id, client in self._clients.items() if connection_id in ids
        )

    def subscribed_connections(self, session_id: str) -> tuple[str, ...]:
        """The attached connection ids currently watching a session."""
        ids = self._subscriptions.get(session_id, set())
        return tuple(connection_id for connection_id in self._clients if connection_id in ids)

    def connections(self) -> tuple[str, ...]:
        """The ids currently attached, in attachment order."""
        return tuple(self._clients)

    def close_all(self) -> None:
        """Forget every client; the server owns the underlying sockets."""
        self._clients.clear()
        self._subscriptions.clear()
        self._connection_sessions.clear()

    # -- per-request selection ---------------------------------------------

    def select(self, connection_id: str) -> contextvars.Token[str | None]:
        """Route response-bearing calls to ``connection_id`` for the current context."""
        return current_connection.set(connection_id)

    def reset(self, token: contextvars.Token[str | None]) -> None:
        """Undo a previous :meth:`select`."""
        current_connection.reset(token)

    def selected(self) -> str | None:
        """The connection selected for the current context, if any."""
        return current_connection.get()

    def current(self) -> Client | None:
        """The client a routed call would use right now, without raising."""
        return self._routed_client()

    # -- broadcast ----------------------------------------------------------

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Deliver a session update only to that session's subscribers.

        A hosted session has no fallback: after access revocation or an explicit
        unsubscribe, an in-flight turn cannot leak an update back to the connection
        that started it. Only a local stdio host may use its sole client before the
        session has a subscriber.
        """

        async def deliver(client: Client) -> None:
            await client.session_update(session_id=session_id, update=update, **kwargs)

        await self._deliver(self._session_targets(session_id), "session_update", deliver)

    async def session_notification(
        self, session_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """Send a session-scoped extension notification to that session's viewers."""

        async def deliver(client: Client) -> None:
            await client.ext_notification(method, params)

        await self._deliver(self._session_targets(session_id), "session_notification", deliver)

    async def session_notification_connections(
        self,
        connection_ids: tuple[str, ...],
        method: str,
        params: dict[str, Any],
    ) -> None:
        """Send a notification to an already-authorized connection snapshot."""

        targets = tuple(
            client
            for connection_id, client in self._clients.items()
            if connection_id in connection_ids
        )

        async def deliver(client: Client) -> None:
            await client.ext_notification(method, params)

        await self._deliver(targets, "session_notification", deliver)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a host notification to the initiating connection only."""
        client = self.current()
        if client is None:
            return
        await client.ext_notification(method, params)

    # -- routed -------------------------------------------------------------

    async def request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        """Ask the selected client to approve a tool call, with policy checks at both boundaries."""
        if self._approval_guard is not None:
            await self._approval_guard(session_id)
        client = self._require_client()
        response = await client.request_permission(
            session_id=session_id,
            tool_call=tool_call,
            options=options,
            **kwargs,
        )
        if response.outcome.outcome == "selected" and self._approval_guard is not None:
            await self._approval_guard(session_id)
        return response

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> schema.WriteTextFileResponse | None:
        return await self._require_client().write_text_file(session_id, path, content, **kwargs)

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> schema.ReadTextFileResponse:
        return await self._require_client().read_text_file(
            session_id, path, line=line, limit=limit, **kwargs
        )

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[schema.EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> schema.CreateTerminalResponse:
        return await self._require_client().create_terminal(
            session_id,
            command,
            args=args,
            env=env,
            cwd=cwd,
            output_byte_limit=output_byte_limit,
            **kwargs,
        )

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> schema.TerminalOutputResponse:
        return await self._require_client().terminal_output(session_id, terminal_id, **kwargs)

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> schema.ReleaseTerminalResponse | None:
        return await self._require_client().release_terminal(session_id, terminal_id, **kwargs)

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> schema.WaitForTerminalExitResponse:
        return await self._require_client().wait_for_terminal_exit(
            session_id, terminal_id, **kwargs
        )

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> schema.KillTerminalResponse | None:
        return await self._require_client().kill_terminal(session_id, terminal_id, **kwargs)

    async def create_elicitation(
        self, message: str, mode: Any, **kwargs: Any
    ) -> schema.CreateElicitationResponse:
        return await self._require_client().create_elicitation(message, mode, **kwargs)

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        await self._require_client().complete_elicitation(elicitation_id, **kwargs)

    def on_connect(self, conn: Agent) -> None:
        """The fan-out owns connection tracking; the host attaches real channels."""
        del conn

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a host-initiated extension request to the selected client."""
        client = self._require_client()
        return await client.ext_method(method, params)

    async def send_request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a raw JSON-RPC request to the selected client."""
        client = self._require_client()
        # The raw JSON-RPC helpers live on the SDK connection that backs a client and
        # are not part of the ``Client`` protocol itself.
        raw_request = getattr(client, "send_request")
        return await raw_request(method, params)

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a raw JSON-RPC notification to the selected client."""
        client = self._routed_client()
        if client is None:
            return
        raw_notification = getattr(client, "send_notification")
        await raw_notification(method, params)

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        """Route any other ACP client method to the selected connection.

        Capability-backed tools call filesystem/terminal methods that are not part of
        the small broadcast/routed surface above. They must act through the connection
        that initiated the turn, so every unknown public attribute becomes an async
        proxy for the routed client.
        """
        if name.startswith("_"):
            raise AttributeError(name)

        def routed(*args: Any, **kwargs: Any) -> Awaitable[Any]:
            client = self._require_client()
            method = getattr(client, name)
            return method(*args, **kwargs)

        return routed

    # -- internals ----------------------------------------------------------

    def _session_targets(self, session_id: str) -> tuple[Client, ...]:
        """Subscribers for a session, with a fallback only for local stdio.

        Hosted actors rely on the initiator subscribing before a turn starts. Falling
        back to the current connection after an unsubscribe would deliver updates to a
        viewer whose access was just revoked.
        """
        targets = self.subscribers(session_id)
        if targets or not self.local:
            return targets
        current = self.current()
        return (current,) if current is not None else ()

    def _routed_client(self) -> Client | None:
        """The connection selected for this request.

        A local stdio host has exactly one client and no connection context, so it may
        fall back to that client. A multi-viewer host never guesses: routing a
        permission or client-origin call to an arbitrary viewer would cross tenants.
        """
        selected = self.selected() or current_connection_id.get()
        if selected is not None:
            client = self._clients.get(selected)
            if client is not None:
                return client
        if self.local and len(self._clients) == 1:
            return next(iter(self._clients.values()))
        return None

    def _require_client(self) -> Client:
        client = self._routed_client()
        if client is None:
            raise RuntimeError("no attached client")
        return client

    async def _deliver(
        self,
        targets: tuple[Client, ...],
        label: str,
        deliver: Callable[[Client], Awaitable[None]],
    ) -> None:
        """Deliver concurrently to a snapshot, isolating individual failures."""
        results = await asyncio.gather(
            *(deliver(client) for client in targets), return_exceptions=True
        )
        for client, result in zip(targets, results, strict=True):
            if isinstance(result, Exception):
                _LOG.warning(
                    "%s failed for client %r; continuing with remaining viewers",
                    label,
                    client,
                    exc_info=(type(result), result, result.__traceback__),
                )
