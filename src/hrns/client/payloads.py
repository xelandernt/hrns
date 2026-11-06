"""Normalization for ACP payloads and interaction records."""

from __future__ import annotations

import json
import ntpath
import posixpath
from typing import Any

from acp.schema import (
    ElicitationFormRequestMode,
    ElicitationFormSessionMode,
    ElicitationUrlRequestMode,
    ElicitationUrlSessionMode,
    PermissionOption,
    ToolCallUpdate,
)

from hrns.shared import ASK_USER_META

from .errors import HrnsClientError
from .interactions import (
    AskUserOption,
    AskUserQuestion,
    AskUserRequest,
    AskUserResponse,
    ElicitationRequest,
    PermissionRequest,
)


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
    """Check a path that belongs to the *agent's* filesystem, not this client's.

    A remote agent may be POSIX while the terminal runs on Windows, so both rootings
    are accepted; the host validates the path against its own workspace policy.
    """
    if not isinstance(value, str) or not (posixpath.isabs(value) or ntpath.isabs(value)):
        raise HrnsClientError(f"The {label} must be an absolute path, got '{value}'.")
    return value


def checked_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if not 1 <= limit <= 100:
        raise HrnsClientError("The pagination limit must be between 1 and 100.")
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


def _options(entries: object) -> list[AskUserOption]:
    """Read a titled (``const``) or untitled (bare string) option list."""
    if not isinstance(entries, list):
        return []
    options: list[AskUserOption] = []
    for entry in entries:
        if isinstance(entry, str):
            label, description = entry, ""
        elif isinstance(entry, dict):
            raw = entry.get("const", entry.get("title"))
            label = raw if isinstance(raw, str) else ""
            raw_description = entry.get("description")
            description = raw_description if isinstance(raw_description, str) else ""
        else:
            continue
        if label:
            options.append(AskUserOption(label=label, description=description))
    return options


def _question_from_property(
    name: str, spec: dict[str, Any], properties: dict[str, Any]
) -> AskUserQuestion | None:
    multi_select = spec.get("type") == "array"
    items = spec.get("items")
    if multi_select:
        options = _options(items.get("anyOf") if isinstance(items, dict) else None)
        options = options or _options(items.get("enum") if isinstance(items, dict) else None)
    else:
        options = _options(spec.get("oneOf")) or _options(spec.get("enum"))
    if not options:
        return None

    title = spec.get("title")
    header = title if isinstance(title, str) and title.strip() else name
    description = spec.get("description")
    text = description if isinstance(description, str) and description.strip() else header
    companion = f"{name}_text"
    return AskUserQuestion(
        header=header,
        question=text,
        options=tuple(options),
        multi_select=multi_select,
        choice_field=name,
        text_field=companion if companion in properties else "",
    )


def _ask_user_questions(
    schema: dict[str, Any],
) -> list[AskUserQuestion]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    questions: list[AskUserQuestion] = []
    for name, spec in properties.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        question = _question_from_property(name, spec, properties)
        if question is not None:
            questions.append(question)
    return questions


def ask_user_request(
    mode: ElicitationFormSessionMode | ElicitationFormRequestMode,
) -> AskUserRequest | None:
    """Recognize an ``ask_user_question`` elicitation and recover its questions.

    The marker is the only thing the agent adds: titles, descriptions, options and
    multi-select come from the standard form schema. ``None`` means the request is a
    generic form and should go to the dialog.
    """
    if ASK_USER_META not in (mode.requested_schema.field_meta or {}):
        return None
    questions = _ask_user_questions(mode.requested_schema.model_dump(mode="json", by_alias=True))
    if not questions:
        return None
    return AskUserRequest(
        request_id="",
        questions=tuple(questions),
        session_id=getattr(mode, "session_id", None),
    )


def ask_user_content(request: AskUserRequest, response: AskUserResponse) -> dict[str, Any]:
    """Serialize answers back into the form property names the agent expects."""
    by_header = {question.header: question for question in request.questions}
    content: dict[str, Any] = {}
    for answer in response.answers:
        question = by_header.get(answer.header)
        if question is None:
            continue
        if answer.custom_answer:
            if question.text_field:
                content[question.text_field] = answer.custom_answer
            continue
        if not question.choice_field:
            continue
        if question.multi_select:
            content[question.choice_field] = list(answer.selected)
        elif answer.selected:
            content[question.choice_field] = answer.selected[0]
    return content
