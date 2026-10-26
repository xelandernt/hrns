"""Map ACP session updates onto client events."""

from __future__ import annotations

from typing import Any

from acp.schema import (
    AgentMessageChunk,
    AgentPlanContentUpdate,
    AgentPlanRemovedUpdate,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    SessionInfoUpdate,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
)

from .bus import EventBus
from .events import (
    AgentTextChunk,
    CommandsUpdated,
    ConfigOptionsUpdated,
    ModesUpdated,
    PlanChanged,
    SessionInfoChanged,
    ToolEvent,
    UsageUpdated,
    UserMessageReplayed,
)
from .payloads import _raw_arguments, text_of


def tool_event(session_id: str, update: Any, *, is_new: bool) -> ToolEvent:
    content: list[dict[str, Any]] = []
    for block in getattr(update, "content", None) or []:
        try:
            content.append(block.model_dump(mode="json", by_alias=True, exclude_none=True))
        except Exception:
            content.append({"type": "content", "content": {"type": "text", "text": str(block)}})
    locations = tuple(
        {
            "path": getattr(location, "path", "") or "",
            "line": str(getattr(location, "line", "") or ""),
        }
        for location in (getattr(update, "locations", None) or [])
    )
    title = getattr(update, "title", "") or ""
    if is_new and not title:
        title = getattr(update, "tool_call_id", "") or "tool"
    return ToolEvent(
        session_id=session_id,
        tool_call_id=getattr(update, "tool_call_id", "") or "",
        title=title,
        kind=str(getattr(update, "kind", "") or ""),
        status=str(getattr(update, "status", "") or ""),
        is_new=is_new,
        content=tuple(content),
        locations=locations,
        raw_input=_raw_arguments(getattr(update, "raw_input", None)),
        raw_output=getattr(update, "raw_output", None),
        meta=dict(getattr(update, "field_meta", None) or {}),
    )


def dispatch_update(bus: EventBus, session_id: str, update: Any) -> None:
    raw_meta = dict(getattr(update, "field_meta", None) or {})
    if isinstance(update, UserMessageChunk):
        # Live prompts are rendered by the composer action; user updates only
        # arrive during session/load replay and belong in the transcript.
        text = text_of(update.content) or ""
        if text:
            bus.publish(
                UserMessageReplayed(
                    session_id=session_id,
                    text=text,
                    message_id=getattr(update, "message_id", None),
                )
            )
        return
    if isinstance(update, AgentMessageChunk):
        text = text_of(update.content) or ""
        if text:
            bus.publish(
                AgentTextChunk(
                    session_id=session_id,
                    text=text,
                    message_id=getattr(update, "message_id", None),
                    meta=raw_meta,
                )
            )
        return
    if isinstance(update, AgentThoughtChunk):
        text = text_of(update.content) or ""
        if text:
            bus.publish(
                AgentTextChunk(
                    session_id=session_id,
                    text=text,
                    message_id=getattr(update, "message_id", None),
                    thought=True,
                    meta=raw_meta,
                )
            )
        return
    if isinstance(update, ToolCallStart):
        bus.publish(tool_event(session_id, update, is_new=True))
        return
    if isinstance(update, ToolCallProgress):
        bus.publish(tool_event(session_id, update, is_new=False))
        return
    if isinstance(update, AgentPlanUpdate):
        entries = tuple((entry.content, entry.priority, entry.status) for entry in update.entries)
        bus.publish(PlanChanged(session_id=session_id, entries=entries))
        return
    if isinstance(update, AgentPlanContentUpdate):
        plan = update.plan
        if getattr(plan, "type", "") == "items":
            entries = tuple((entry.content, entry.priority, entry.status) for entry in plan.entries)
            bus.publish(PlanChanged(session_id=session_id, entries=entries))
        elif getattr(plan, "type", "") == "markdown":
            content = str(getattr(plan, "content", ""))
            entries = tuple(
                (line.strip(), "medium", "pending") for line in content.splitlines() if line.strip()
            )
            bus.publish(PlanChanged(session_id=session_id, entries=entries))
        return
    if isinstance(update, AgentPlanRemovedUpdate):
        bus.publish(PlanChanged(session_id=session_id, cleared=True))
        return
    if isinstance(update, AvailableCommandsUpdate):
        commands = tuple(
            command.model_dump(mode="json", by_alias=True, exclude_none=True)
            for command in update.available_commands
        )
        bus.publish(CommandsUpdated(session_id=session_id, commands=commands))
        return
    if isinstance(update, CurrentModeUpdate):
        bus.publish(
            ModesUpdated(
                session_id=session_id,
                current_mode_id=update.current_mode_id,
                available_modes=(),
            )
        )
        return
    if isinstance(update, ConfigOptionUpdate):
        options = tuple(
            option.model_dump(mode="json", by_alias=True, exclude_none=True)
            for option in update.config_options
        )
        bus.publish(ConfigOptionsUpdated(session_id=session_id, options=options))
        return
    if isinstance(update, SessionInfoUpdate):
        bus.publish(SessionInfoChanged(session_id=session_id, title=update.title))
        hrns_meta = raw_meta.get("hrns", {})
        if isinstance(hrns_meta, dict):
            usage = hrns_meta.get("sessionUsage")
            if isinstance(usage, dict):
                bus.publish(UsageUpdated(session_id=session_id, summary=usage))
        return
    if isinstance(update, UsageUpdate):
        hrns_meta = raw_meta.get("hrns", {})
        detail = hrns_meta.get("contextUsage") if isinstance(hrns_meta, dict) else None
        summary: dict[str, Any] = {
            "scope": "context",
            "used": update.used,
            "size": update.size,
            "detail": detail if isinstance(detail, dict) else {},
        }
        if update.cost is not None:
            summary["cost"] = {
                "amount": update.cost.amount,
                "currency": update.cost.currency,
            }
        bus.publish(UsageUpdated(session_id=session_id, summary=summary))
        return
    # Unknown future update kinds degrade to a readable activity event.
    bus.publish(
        AgentTextChunk(
            session_id=session_id,
            text=f"[activity: {getattr(update, 'session_update', 'update')}]",
            thought=True,
            meta=raw_meta,
        )
    )
