"""Passive native observer for tool presentation metadata.

The observer listens to public Pydantic AI capability events and tool hooks. It never wraps
the ACP adapter, changes model-facing results, or owns a second event store. Final
presentation state is written into ``ToolReturn.metadata`` (native message metadata) so a
replayed conversation can reconstruct completed views through public history.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai import CapabilityEvent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import FunctionToolCallEvent, ToolReturn
from pydantic_ai.tools import ToolDefinition

from harness.protocol import PresentationPayload, ToolPresentationSnapshot

__all__ = [
    "PresentationObserver",
    "ToolPresentationProgressEvent",
    "presentation_from_metadata",
    "snapshot_from_metadata",
]

_MAX_PRESENTATION_BYTES = 1_000_000


@dataclass(kw_only=True)
class ToolPresentationProgressEvent(CapabilityEvent, namespace="harness"):
    """Optional typed progress a capability tool emits through the public event stream."""

    presentation: dict[str, Any] = field(default_factory=dict)


def _bounded(payload: PresentationPayload | None) -> PresentationPayload | None:
    if payload is None:
        return None
    try:
        encoded = json.dumps(
            payload.to_wire(), ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return payload if len(encoded) <= _MAX_PRESENTATION_BYTES else None


def _payload(value: Any) -> PresentationPayload | None:
    if value is None:
        return None
    if isinstance(value, PresentationPayload):
        return _bounded(value)
    if not isinstance(value, dict):
        return None
    try:
        return _bounded(PresentationPayload.model_validate(value))
    except Exception:
        return None


def _metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _declared(tool_def: ToolDefinition | None) -> tuple[dict[str, Any], PresentationPayload | None]:
    if tool_def is None:
        return {}, None
    declared = _metadata(_metadata(tool_def.metadata).get("harness")).get("presentation")
    declared = _metadata(declared)
    return declared, _payload(declared.get("input"))


def presentation_from_metadata(metadata: Any) -> dict[str, Any]:
    """The normalized ``harness.presentation`` block of tool metadata, if present."""
    return _metadata(_metadata(metadata).get("harness")).get("presentation") or {}


def snapshot_from_metadata(
    session_id: str, tool_call_id: str, metadata: Any
) -> ToolPresentationSnapshot | None:
    """Rebuild a completed presentation snapshot from committed native return metadata."""
    block = _metadata(presentation_from_metadata(metadata))
    if not block:
        return None
    input_payload = _payload(block.get("input"))
    output_payload = _payload(block.get("output"))
    if input_payload is None and output_payload is None:
        return None
    fallback = str(block.get("fallback") or "")
    if not fallback:
        fallback = (
            output_payload.fallback
            if output_payload is not None
            else input_payload.fallback
            if input_payload is not None
            else ""
        )
    return ToolPresentationSnapshot(
        session_id=session_id,
        tool_call_id=tool_call_id,
        revision=max(1, int(block.get("revision") or 1)),
        binding_id=block.get("bindingId") or block.get("binding_id"),
        run_id=block.get("runId") or block.get("run_id"),
        input=input_payload,
        output=output_payload,
        fallback=fallback,
    )


@dataclass(kw_only=True)
class PresentationObserver(AbstractCapability[Any]):
    """Always-on, session-scoped observer forwarding validated presentation snapshots."""

    session_id: str
    publish: Callable[[ToolPresentationSnapshot], Awaitable[None]]
    id: str | None = "harness.presentation"
    _revisions: dict[str, int] = field(default_factory=dict)
    _inputs: dict[str, PresentationPayload | None] = field(default_factory=dict)
    _bindings: dict[str, str | None] = field(default_factory=dict)

    def _revision(self, tool_call_id: str) -> int:
        revision = self._revisions.get(tool_call_id, 0) + 1
        self._revisions[tool_call_id] = revision
        if len(self._revisions) > 1000:
            for key in list(self._revisions)[:500]:
                self._revisions.pop(key, None)
                self._inputs.pop(key, None)
                self._bindings.pop(key, None)
        return revision

    async def _publish(self, snapshot: ToolPresentationSnapshot) -> None:
        try:
            await self.publish(snapshot)
        except Exception:
            # A failing presentation sink must not turn a successful tool call into a
            # failed model run. The standard ACP view still carries status and content.
            return

    async def on_event(self, ctx: RunContext[Any], *, event: Any) -> None:
        if isinstance(event, FunctionToolCallEvent):
            tool_def = ctx.tools.get(event.part.tool_name)
            declared, input_payload = _declared(tool_def)
            binding = declared.get("bindingId") or declared.get("binding_id")
            if input_payload is None and not binding:
                return
            call_id = event.tool_call_id
            self._inputs[call_id] = input_payload
            self._bindings[call_id] = str(binding) if binding else None
            await self._publish(
                ToolPresentationSnapshot(
                    session_id=self.session_id,
                    tool_call_id=call_id,
                    revision=self._revision(call_id),
                    binding_id=self._bindings[call_id],
                    run_id=ctx.run_id if ctx.run_id else None,
                    input=input_payload,
                    fallback=input_payload.fallback if input_payload else "",
                )
            )
            return
        if isinstance(event, ToolPresentationProgressEvent):
            call_id = event.tool_call_id or ctx.tool_call_id
            if not call_id:
                return
            payload = _payload(event.presentation)
            if payload is None:
                return
            await self._publish(
                ToolPresentationSnapshot(
                    session_id=self.session_id,
                    tool_call_id=call_id,
                    revision=self._revision(call_id),
                    binding_id=self._bindings.get(call_id),
                    run_id=ctx.run_id if ctx.run_id else None,
                    progress=payload,
                    fallback=payload.fallback,
                )
            )

    async def after_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: Any,
        tool_def: ToolDefinition,
        args: Any,
        result: Any,
    ) -> Any:
        del args
        call_id = getattr(call, "tool_call_id", "") or ctx.tool_call_id
        if not call_id:
            return result
        declared, declared_input = _declared(tool_def)
        input_payload = self._inputs.get(call_id) or declared_input
        binding = self._bindings.get(call_id) or declared.get("bindingId")
        output_payload: PresentationPayload | None = None
        if isinstance(result, ToolReturn):
            returned = _metadata(_metadata(result.metadata).get("harness")).get("presentation")
            returned = _metadata(returned)
            output_payload = _payload(returned.get("output")) or _payload(returned)
            if input_payload is None:
                input_payload = _payload(returned.get("input"))
            if not binding:
                binding = returned.get("bindingId") or returned.get("binding_id")
            if output_payload is None and input_payload is None and not binding:
                return result
            revision = self._revision(call_id)
            normalized = {
                "version": 1,
                "revision": revision,
                "bindingId": str(binding) if binding else None,
                "runId": ctx.run_id if ctx.run_id else None,
                "input": input_payload.to_wire() if input_payload else None,
                "output": output_payload.to_wire() if output_payload else None,
                "fallback": (
                    output_payload.fallback
                    if output_payload
                    else input_payload.fallback
                    if input_payload
                    else ""
                ),
            }
            metadata = dict(result.metadata) if isinstance(result.metadata, dict) else {}
            harness = dict(_metadata(metadata.get("harness")))
            harness["presentation"] = normalized
            metadata["harness"] = harness
            result = replace(result, metadata=metadata)
        else:
            if input_payload is None:
                return result
            revision = self._revision(call_id)
        await self._publish(
            ToolPresentationSnapshot(
                session_id=self.session_id,
                tool_call_id=call_id,
                revision=revision,
                binding_id=str(binding) if binding else None,
                run_id=ctx.run_id if ctx.run_id else None,
                input=input_payload,
                output=output_payload,
                fallback=(
                    output_payload.fallback
                    if output_payload
                    else input_payload.fallback
                    if input_payload
                    else ""
                ),
            )
        )
        self._inputs.pop(call_id, None)
        self._bindings.pop(call_id, None)
        return result
