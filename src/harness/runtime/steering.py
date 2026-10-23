"""Steer a running turn instead of waiting for it to finish.

Pydantic AI keeps a per-run queue of messages that drain into the next model request
(``RunContext.enqueue``). This module bridges that queue to the host's extension
surface: one channel per session, attached by a capability on every model request, so
an extension call handled in another task can still reach the live run.

Delivery is "as soon as possible": the message lands in the request after the current
tool call or model response, and is committed to native history like any other user
message. When the turn is about to end, Pydantic AI redirects through one more request
rather than dropping it.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext

__all__ = ["SteeringCapability", "SteeringChannel"]


class SteeringChannel:
    """Handle to the running turn's pending-message queue."""

    def __init__(self) -> None:
        self._context: RunContext[Any] | None = None

    def attach(self, context: RunContext[Any]) -> None:
        self._context = context

    def detach(self) -> None:
        self._context = None

    def enqueue(self, text: str) -> bool:
        """Queue ``text`` for the next model request; false when no turn is live."""
        context = self._context
        if context is None or not text.strip():
            return False
        try:
            return context.enqueue(text, priority="asap") is not None
        except Exception:
            # A finished or closed run cannot accept messages; the client queues locally.
            return False


class SteeringCapability(AbstractCapability[Any]):
    """Expose the live ``RunContext`` to the session's steering channel."""

    def __init__(self, channel: SteeringChannel) -> None:
        self.channel = channel

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        self.channel.attach(ctx)
        return request_context
