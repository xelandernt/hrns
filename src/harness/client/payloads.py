"""Normalization for ACP payloads and interaction records."""

from __future__ import annotations

import json
from typing import Any

from acp.schema import (
    ElicitationFormRequestMode,
    ElicitationFormSessionMode,
    ElicitationUrlRequestMode,
    ElicitationUrlSessionMode,
    PermissionOption,
    ToolCallUpdate,
)

from .errors import HarnessClientError
from .interactions import ElicitationRequest, PermissionRequest


def _clip(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _raw_arguments(value: Any) -> Any:
    """Decode ``raw_input``, which agents send as an object or as a JSON string.

    A string payload left undecoded renders as a JSON blob in approval prompts and hides the
    argument names every view keys off.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.startswith(("{", "[")):
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def absolute_path(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise HarnessClientError(f"The {label} must be an absolute path, got '{value}'.")
    return value


def checked_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if not 1 <= limit <= 100:
        raise HarnessClientError("The pagination limit must be between 1 and 100.")
    return limit


def text_of(content: Any) -> str | None:
    if getattr(content, "type", None) == "text":
        return getattr(content, "text", None)
    kind = getattr(content, "type", None) or "attachment"
    if kind == "image":
        return f"[image {getattr(content, 'mime_type', '') or ''}]".strip()
    if kind == "audio":
        return "[audio attachment]"
    if kind == "resource_link":
        return f"[resource link: {getattr(content, 'name', '')}]"
    if kind == "resource":
        return "[embedded resource]"
    return None


def normalize_permission(
    session_id: str,
    tool_call: ToolCallUpdate,
    options: list[PermissionOption],
) -> PermissionRequest:
    title = getattr(tool_call, "title", None) or getattr(tool_call, "kind", None) or "tool call"
    raw_input = _raw_arguments(getattr(tool_call, "raw_input", None))
    detail = ""
    if raw_input is not None:
        try:
            detail = json.dumps(raw_input, ensure_ascii=False, default=str)
        except Exception:
            detail = str(raw_input)
        detail = _clip(detail, 600)
    return PermissionRequest(
        session_id=session_id,
        tool_call_id=getattr(tool_call, "tool_call_id", "") or "",
        title=str(title),
        kind=str(getattr(tool_call, "kind", "") or ""),
        status=str(getattr(tool_call, "status", "") or ""),
        options=tuple(
            {
                "optionId": option.option_id,
                "name": option.name,
                "kind": option.kind,
            }
            for option in options
        ),
        raw_input=raw_input,
        detail=detail,
    )


def elicitation_record(
    message: str,
    mode: ElicitationFormSessionMode
    | ElicitationFormRequestMode
    | ElicitationUrlSessionMode
    | ElicitationUrlRequestMode,
) -> ElicitationRequest:
    if isinstance(mode, (ElicitationFormSessionMode, ElicitationFormRequestMode)):
        schema = mode.requested_schema.model_dump(mode="json", by_alias=True)
        return ElicitationRequest(
            kind="form",
            message=message,
            schema=schema,
            session_id=getattr(mode, "session_id", None),
            tool_call_id=getattr(mode, "tool_call_id", None),
            request_id=getattr(mode, "request_id", None),
        )
    return ElicitationRequest(
        kind="url",
        message=message,
        url=getattr(mode, "url", "") or "",
        session_id=getattr(mode, "session_id", None),
        tool_call_id=getattr(mode, "tool_call_id", None),
        request_id=getattr(mode, "request_id", None),
        elicitation_id=getattr(mode, "elicitation_id", None),
    )
