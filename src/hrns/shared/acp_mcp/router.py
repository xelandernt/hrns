"""Install the stateless ``mcp/message`` routes on an ACP SDK connection.

The Python SDK still generates the stateful MCP-over-ACP prototype, so its
router validates ``connectionId`` before an application handler runs. This
module replaces the two ``mcp/message`` routes on the SDK's
:class:`acp.router.MessageRouter` with stateless handlers and sends raw
``mcp/message`` frames through the SDK connection.

Everything here is standard ACP wire data; no ``_hrns/*`` extension is used.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from acp.router import MessageRouter, Route
from pydantic import JsonValue, TypeAdapter

from .core import (
    MCP_MESSAGE_METHOD,
    AcpChannel,
    JsonObject,
    McpOverAcpCaller,
    McpOverAcpProvider,
    outcome_to_wire,
)

__all__ = ["SdkAcpChannel", "bind_caller", "bind_provider"]

_JSON_VALUE = TypeAdapter(JsonValue)


class SdkAcpChannel(AcpChannel):
    """Adapt an ``acp`` SDK connection to :class:`AcpChannel`."""

    def __init__(self, connection: object) -> None:
        raw = getattr(connection, "_conn", None)
        if raw is None:
            raise TypeError("expected an acp ClientSideConnection or AgentSideConnection")
        self._connection = connection
        self._raw: object = raw

    @property
    def key(self) -> object:
        return self._connection

    async def send_request(self, method: str, params: JsonObject) -> JsonValue:
        send = getattr(self._raw, "send_request", None)
        if not callable(send):
            raise RuntimeError("the ACP connection cannot send requests")
        result = send(method, params)
        if not inspect.isawaitable(result):
            raise RuntimeError("the ACP connection returned a non-awaitable request")
        return _JSON_VALUE.validate_python(await result)

    async def send_notification(self, method: str, params: JsonObject) -> None:
        send = getattr(self._raw, "send_notification", None)
        if not callable(send):
            raise RuntimeError("the ACP connection cannot send notifications")
        result = send(method, params)
        if inspect.isawaitable(result):
            await result

    def install(
        self,
        *,
        request_handler: Callable[[object], Awaitable[object | None]] | None = None,
        notification_handler: Callable[[object], Awaitable[object | None]] | None = None,
    ) -> None:
        handler = getattr(self._raw, "_handler", None)
        add_route = getattr(handler, "add_route", None)
        if not isinstance(handler, MessageRouter) or not callable(add_route):
            raise TypeError("the ACP connection was not built with the SDK's MessageRouter")
        if request_handler is not None:
            add_route(
                Route(
                    method=MCP_MESSAGE_METHOD,
                    func=request_handler,
                    kind="request",
                )
            )
        if notification_handler is not None:
            add_route(
                Route(
                    method=MCP_MESSAGE_METHOD,
                    func=notification_handler,
                    kind="notification",
                )
            )


def bind_provider(connection: object, provider: McpOverAcpProvider) -> SdkAcpChannel:
    """Install ``provider`` as the ``mcp/message`` request handler."""
    channel = SdkAcpChannel(connection)

    async def request_route(params: object) -> JsonObject:
        return outcome_to_wire(await provider.handle_request(channel, params))

    async def notification_route(params: object) -> None:
        provider.handle_notification(channel, params)

    channel.install(
        request_handler=request_route,
        notification_handler=notification_route,
    )
    return channel


def bind_caller(connection: object, caller: McpOverAcpCaller) -> SdkAcpChannel:
    """Install ``caller`` as the ``mcp/message`` notification handler."""
    channel = SdkAcpChannel(connection)

    async def notification_route(params: object) -> None:
        caller.handle_notification(channel, params)

    channel.install(notification_handler=notification_route)
    return channel
