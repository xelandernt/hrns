"""Read-only AG-UI conversion and atomic replay for existing native execution."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar

from ag_ui.core import (
    BaseEvent,
    Message,
    MessagesSnapshotEvent,
    RunErrorEvent,
    StateSnapshotEvent,
    TextMessageStartEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartStartEvent,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.run import AgentRunResult, AgentRunResultEvent
from pydantic_ai.ui.ag_ui import AGUIAdapter, AGUIEventStream

OutputT = TypeVar("OutputT")


@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[str | None]


class Observation:
    """One converter per native run, shared encoded output for every observer.

    All state changes are synchronous on the runtime's event loop. Snapshotting and
    installing a subscriber therefore form one atomic operation without network I/O.
    """

    def __init__(
        self, session_id: str, history: list[ModelMessage], *, queue_size: int = 256
    ) -> None:
        self.session_id = session_id
        self._encoder = AGUIEventStream(thread_id=session_id)
        self._messages = AGUIAdapter.dump_messages(history)
        self._baseline = list(self._messages)
        self._prefix: list[str] = []
        self._active = False
        self._state: dict[str, object] = {}
        self._subscribers: set[_Subscriber] = set()
        self._queue_size = queue_size
        self._closed = False
        self._finish_pending: Callable[[list[ModelMessage]], None] | None = None

    def _encode(self, event: BaseEvent) -> str:
        return self._encoder.encode_event(event)

    def _publish(self, event: BaseEvent) -> None:
        encoded = self._encode(event)
        if self._active:
            self._prefix.append(encoded)
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.queue.put_nowait(encoded)
            except asyncio.QueueFull:
                self._disconnect(subscriber)

    def _disconnect(self, subscriber: _Subscriber) -> None:
        self._subscribers.discard(subscriber)
        while not subscriber.queue.empty():
            subscriber.queue.get_nowait()
        subscriber.queue.put_nowait(None)

    def close(self) -> None:
        self._closed = True
        for subscriber in tuple(self._subscribers):
            self._disconnect(subscriber)

    def state_changed(self, state: dict[str, object]) -> None:
        self._state = dict(state)
        self._publish(StateSnapshotEvent(snapshot={"hrns": self._state, "readOnly": True}))

    def snapshot(self) -> dict[str, object]:
        return {
            "readOnly": True,
            "session": self._state,
            "messages": [
                m.model_dump(mode="json", by_alias=True, exclude_none=True)
                for m in (self._baseline if self._active else self._messages)
            ],
            "events": list(self._prefix) if self._active else [],
        }

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[str]]:
        subscriber = _Subscriber(asyncio.Queue(maxsize=self._queue_size))
        baseline = list(self._baseline if self._active else self._messages)
        replay = tuple(self._prefix) if self._active else ()
        state = self._encode(StateSnapshotEvent(snapshot={"hrns": self._state, "readOnly": True}))
        if not self._closed:
            self._subscribers.add(subscriber)
        else:
            subscriber.queue.put_nowait(None)

        async def events() -> AsyncIterator[str]:
            yield self._encode(MessagesSnapshotEvent(messages=baseline))
            yield state
            for encoded in replay:
                yield encoded
            while True:
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                yield event

        try:
            yield events()
        finally:
            self._subscribers.discard(subscriber)

    async def consume(
        self,
        stream: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[OutputT]],
        history: list[ModelMessage],
        user_content: list[UserContent] | None,
        emit: Callable[[AgentStreamEvent], Awaitable[None]],
        run_id: str,
    ) -> AgentRunResult[OutputT]:
        # Dump input once; its eventual native request reuses these message IDs.
        input_messages = (
            AGUIAdapter.dump_messages([ModelRequest(parts=[UserPromptPart(content=user_content)])])
            if user_content
            else []
        )
        self._messages.extend(input_messages)
        self._baseline = list(self._messages)
        self._prefix = []
        self._active = False
        self._publish(MessagesSnapshotEvent(messages=list(self._baseline)))
        self._active = True
        converter = AGUIEventStream(thread_id=self.session_id, run_id=run_id)
        result: AgentRunResult[OutputT] | None = None
        # Native response order supplies identity association, without reducing deltas.
        failure: Exception | None = None
        response_index = -1
        assistant_ids: dict[int, deque[str]] = defaultdict(deque)
        reasoning_ids: dict[int, deque[str]] = defaultdict(deque)
        tool_ids: dict[str, str] = {}

        async def native() -> AsyncIterator[AgentStreamEvent | AgentRunResultEvent[OutputT]]:
            nonlocal result, response_index, failure
            try:
                async for event in stream:
                    if isinstance(event, AgentRunResultEvent):
                        result = event.result
                    else:
                        if isinstance(event, PartStartEvent) and event.index == 0:
                            response_index += 1
                        await emit(event)
                    yield event
            except Exception as exc:
                failure = exc
                raise

        def finish(messages: list[ModelMessage]) -> None:
            appended: list[Message] = []
            response_index = -1
            user_ids = deque(message.id for message in input_messages)
            for message in messages[len(history) :]:
                if isinstance(message, ModelResponse):
                    response_index += 1
                for dumped in AGUIAdapter.dump_messages([message]):
                    if dumped.role == "user" and user_ids:
                        dumped.id = user_ids.popleft()
                        continue  # Input is already in the baseline.
                    if dumped.role == "assistant" and assistant_ids[response_index]:
                        dumped.id = assistant_ids[response_index].popleft()
                    elif dumped.role == "reasoning" and reasoning_ids[response_index]:
                        dumped.id = reasoning_ids[response_index].popleft()
                    elif dumped.role == "tool" and dumped.tool_call_id in tool_ids:
                        dumped.id = tool_ids[dumped.tool_call_id]
                    appended.append(dumped)
            self._messages.extend(appended)
            self._active = False
            self._prefix = []
            self._baseline = list(self._messages)
            self._publish(MessagesSnapshotEvent(messages=list(self._messages)))

        self._finish_pending = finish
        try:
            async for event in converter.transform_stream(native()):
                if isinstance(event, TextMessageStartEvent):
                    if event.message_id not in assistant_ids[response_index]:
                        assistant_ids[response_index].append(event.message_id)
                elif isinstance(event, ToolCallStartEvent) and event.parent_message_id:
                    if event.parent_message_id not in assistant_ids[response_index]:
                        assistant_ids[response_index].append(event.parent_message_id)
                elif isinstance(event, ToolCallResultEvent):
                    tool_ids[event.tool_call_id] = event.message_id
                elif event.type == "REASONING_START":
                    message_id = getattr(event, "message_id", None)
                    if isinstance(message_id, str):
                        reasoning_ids[response_index].append(message_id)
                self._publish(event)
            if failure is not None:
                raise failure
            assert result is not None, "native stream must produce a result"
            return result
        except asyncio.CancelledError:
            self._publish(RunErrorEvent(message="Run cancelled", code="CANCELLED"))
            raise
        finally:
            if result is not None:
                finish(result.all_messages())
                self._finish_pending = None

    def interrupted(self, messages: list[ModelMessage]) -> None:
        if self._finish_pending is not None:
            self._finish_pending(messages)
            self._finish_pending = None
