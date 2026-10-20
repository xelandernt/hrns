"""Translation of native Pydantic AI stream events into runtime events.

The runtime stream is deliberately separate from the public transcript: it carries the
native execution facts (text deltas, tool lifecycle, usage, deferred approvals) while
capabilities keep their own event types. Nothing here re-implements capability behavior;
each event is projected from the public ``AgentStreamEvent`` union.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import (
    CapabilityEvent,
    CustomEvent,
    DeferredToolRequestsEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolReturnPart,
)
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.usage import RunUsage
from pydantic_core import to_jsonable_python

from harness.protocol import RuntimeEvent

__all__ = [
    "TranslationState",
    "deferred_requests_payload",
    "output_payload",
    "translate_event",
    "usage_payload",
]


@dataclass
class TranslationState:
    """Per-turn state needed to decode streamed thinking parts.

    Some providers (notably OpenAI's Responses API) carry reasoning text in
    ``provider_details['raw_content']`` and stream it as updater callbacks
    instead of ``content_delta``. Tracking the last emitted text lets us turn
    those cumulative updates into the deltas the runtime vocabulary expects.
    """

    thinking: dict[int, tuple[dict[str, Any], str]] = field(default_factory=dict)


def _part_index(event: Any) -> int | None:
    index = getattr(event, "index", None)
    return index if isinstance(index, int) else None


def _raw_thinking_text(details: Any) -> str:
    """Join a thinking part's provider-specific raw reasoning chunks."""
    if not isinstance(details, dict):
        return ""
    raw = details.get("raw_content")
    if isinstance(raw, list):
        return "".join(str(item) for item in raw)
    return raw if isinstance(raw, str) else ""


def _thinking_event(
    text: str,
    index: int | None,
    *,
    thought: bool = True,
    part_start: bool = False,
) -> RuntimeEvent:
    return RuntimeEvent(
        type="text_delta",
        data={
            "text": text,
            "part_index": index,
            "kind": "thinking" if thought else "text",
            "thought": thought,
            "part_start": part_start,
        },
    )


def translate_event(
    event: Any, state: TranslationState | None = None
) -> list[RuntimeEvent]:
    """Project one native stream event into zero or more runtime events.

    ``state`` is optional for stateless callers; pass one per turn to decode
    providers that stream reasoning through cumulative ``provider_details``.
    """
    if isinstance(event, AgentRunResultEvent):
        return []  # the trailing result event is represented by turn_complete
    if isinstance(event, PartStartEvent):
        part = event.part
        index = _part_index(event)
        if isinstance(part, TextPart) and part.content:
            return [
                RuntimeEvent(
                    type="text_delta",
                    data={
                        "text": part.content,
                        "part_index": index,
                        "kind": "text",
                        "thought": False,
                        "part_start": True,
                    },
                )
            ]
        if isinstance(part, ThinkingPart):
            details = (
                dict(part.provider_details)
                if isinstance(part.provider_details, dict)
                else {}
            )
            text = part.content or _raw_thinking_text(details)
            if state is not None and index is not None:
                state.thinking[index] = (details, text)
            return [_thinking_event(text, index, part_start=True)] if text else []
        return []
    if isinstance(event, PartDeltaEvent):
        delta = event.delta
        index = _part_index(event)
        if isinstance(delta, TextPartDelta) and delta.content_delta:
            return [
                RuntimeEvent(
                    type="text_delta",
                    data={
                        "text": delta.content_delta,
                        "part_index": index,
                        "kind": "text",
                        "thought": False,
                    },
                )
            ]
        if isinstance(delta, ThinkingPartDelta):
            text = delta.content_delta or ""
            if state is not None and index is not None:
                details, emitted = state.thinking.get(index, ({}, ""))
                change = delta.provider_details
                if callable(change):
                    try:
                        details = change(dict(details))
                    except Exception:
                        details = dict(details)
                elif isinstance(change, dict):
                    details = {**details, **change}
                current = _raw_thinking_text(details)
                if text:
                    emitted += text
                elif current.startswith(emitted):
                    text = current[len(emitted):]
                    emitted = current
                else:
                    text = current
                    emitted = current
                state.thinking[index] = (details, emitted)
            return [_thinking_event(text, index)] if text else []
        return []
    if isinstance(event, PartEndEvent) and isinstance(event.part, ThinkingPart):
        index = _part_index(event)
        text = ""
        if state is not None and index is not None:
            details, emitted = state.thinking.pop(index, ({}, ""))
            final = _raw_thinking_text(event.part.provider_details)
            if final.startswith(emitted):
                text = final[len(emitted):]
        return [_thinking_event(text, index)] if text else []
    if isinstance(event, FunctionToolCallEvent | OutputToolCallEvent):
        part = event.part
        return [
            RuntimeEvent(
                type="tool_call",
                data={
                    "tool_call_id": event.tool_call_id,
                    "tool_name": part.tool_name,
                    "args": _tool_args(part),
                    "kind": "output" if isinstance(event, OutputToolCallEvent) else "function",
                },
            )
        ]
    if isinstance(event, FunctionToolResultEvent | OutputToolResultEvent):
        part = event.part
        if isinstance(part, ToolReturnPart):
            outcome = part.outcome
            content: Any = part.content
            content_type = "tool_return"
        elif isinstance(part, RetryPromptPart):
            outcome = "retry"
            content = part.content
            content_type = "retry_prompt"
        else:  # pragma: no cover - defensive for future part kinds
            return []
        payload: Any = _jsonable_content(content)
        return [
            RuntimeEvent(
                type="tool_update",
                data={
                    "tool_call_id": event.tool_call_id,
                    "tool_name": getattr(part, "tool_name", None),
                    "status": _acp_status(outcome),
                    "outcome": outcome,
                    "content": payload,
                    "output": payload,
                    "content_type": content_type,
                    "kind": "output" if isinstance(event, OutputToolResultEvent) else "function",
                },
            )
        ]
    if isinstance(event, DeferredToolRequestsEvent):
        return [
            RuntimeEvent(type="deferred_tool_requests", data=deferred_requests_payload(event.requests))
        ]
    if isinstance(event, CustomEvent):
        name = getattr(event, "name", None) or getattr(event, "event_kind", "custom")
        payload = getattr(event, "payload", None)
        if payload is None:
            payload = to_jsonable_python(event, serialize_unknown=True)
        return [RuntimeEvent(type="custom", data={"name": name, "payload": to_jsonable_python(payload)})]
    if isinstance(event, CapabilityEvent):
        return [
            RuntimeEvent(
                type="capability",
                data={
                    "event_kind": getattr(event, "event_kind", "capability"),
                    "event": to_jsonable_python(event, serialize_unknown=True),
                },
            )
        ]
    return []


def _tool_args(part: Any) -> Any:
    raw = getattr(part, "args", None)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return to_jsonable_python(raw) if raw is not None else {}


def _jsonable_content(content: Any) -> Any:
    """JSON-safe tool content without dropping non-JSON values."""
    if isinstance(content, str):
        return content
    try:
        return to_jsonable_python(content)
    except Exception:  # pragma: no cover - defensive
        return str(content)


def usage_payload(usage: RunUsage | None) -> dict[str, Any]:
    """Stable usage summary for events and the turns table."""
    if usage is None:
        return {
            "requests": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
    payload: dict[str, Any] = {
        "requests": usage.requests,
        "tool_calls": usage.tool_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }
    if usage.input_audio_tokens:
        payload["input_audio_tokens"] = usage.input_audio_tokens
    if usage.output_audio_tokens:
        payload["output_audio_tokens"] = usage.output_audio_tokens
    if usage.details:
        payload["details"] = dict(usage.details)
    if usage.cost is not None:
        try:
            payload["cost"] = float(usage.cost)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass
    if usage.total_tokens:
        payload["total_tokens"] = usage.total_tokens
    return payload


def output_payload(output: Any) -> tuple[Any, str]:
    """JSON-safe result payload plus a qualified type name (native objects are never persisted)."""
    type_name = _qualified_type_name(output)
    if output is None:
        return None, type_name
    if hasattr(output, "model_dump") and callable(output.model_dump):
        try:
            return output.model_dump(mode="json"), type_name
        except Exception:  # pragma: no cover - defensive
            pass
    try:
        return to_jsonable_python(output), type_name
    except Exception:  # pragma: no cover - e.g. custom dependency objects
        return str(output), type_name


def _qualified_type_name(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def deferred_requests_payload(requests: Any) -> dict[str, Any]:
    """Approval/external-call view of a ``DeferredToolRequests`` output or event."""
    approvals = [
        {
            "tool_call_id": part.tool_call_id,
            "tool_name": part.tool_name,
            "args": _tool_args(part),
        }
        for part in getattr(requests, "approvals", [])
    ]
    calls = [
        {
            "tool_call_id": part.tool_call_id,
            "tool_name": part.tool_name,
            "args": _tool_args(part),
        }
        for part in getattr(requests, "calls", [])
    ]
    metadata = getattr(requests, "metadata", None) or {}
    return {
        "approvals": approvals,
        "calls": calls,
        "metadata": to_jsonable_python(metadata) if metadata else {},
    }


def _acp_status(outcome: str) -> str:
    """Map a native tool outcome onto ACP's tool-call status vocabulary."""
    if outcome == "success":
        return "completed"
    if outcome == "retry":
        return "failed"
    if outcome == "interrupted":
        return "failed"
    if outcome == "denied":
        return "failed"
    return "failed"
