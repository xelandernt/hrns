"""Pure helpers for the terminal UI: formatting, option state, form coercion.

Kept free of widgets so the behaviour can be unit tested without a running
application.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from ..constants import UI_MAX_TOOL_CONTENT_CHARS
from ..interactions import PERMISSION_MODES

__all__ = [
    "build_form_fields",
    "binding_summary",
    "coerce_value",
    "config_option_value",
    "format_usage",
    "is_subagent_tool",
    "merge_tool_event",
    "model_option",
    "option_values",
    "pending_application_text",
    "permission_mode_label",
    "presentation_payload",
    "tool_args_preview",
    "tool_detail_text",
    "tool_result_text",
    "tool_status_label",
]

TOOL_STATUS_LABELS = {
    "pending": "○ pending",
    "in_progress": "◐ running",
    "completed": "● completed",
    "failed": "✕ failed",
    "": "○ queued",
}


def local_command_names() -> frozenset[str]:
    """Compatibility hook; commands now live in :mod:`tui.commands`."""
    from .commands import command_names

    return command_names()


def permission_mode_label(mode: str) -> str:
    """Short status-bar label; the mode value stays the on-wire/persisted key."""
    return {"ask": "ask", "reads": "reads", "all": "allow all"}.get(mode, mode)


def tool_status_label(status: str) -> str:
    return TOOL_STATUS_LABELS.get(status or "", f"○ {status}")


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_args_preview(data: dict[str, Any], *, limit: int = 80) -> str:
    """Compact, one-line argument summary for a tool call header.

    ACP exposes the original call arguments as opaque JSON (``raw_input``).
    Recognising the common agent tools keeps the header readable; anything
    unknown falls back to the first two ``key=value`` pairs.
    """
    raw = data.get("raw_input")
    if isinstance(raw, str):
        return _one_line(raw, limit)
    if not isinstance(raw, dict):
        return ""

    def value(*keys: str) -> Any:
        for key in keys:
            if key in raw:
                return raw[key]
        return None

    kind = str(data.get("kind") or "")
    path = value("file_path", "path")
    if kind == "execute" or "command" in raw:
        return _one_line(value("command", "cmd") or "", limit)
    if kind == "read":
        parts = [str(path or "?")]
        if raw.get("limit") is not None:
            parts.append(f"limit={raw['limit']}")
        if raw.get("offset") is not None:
            parts.append(f"offset={raw['offset']}")
        return _one_line(", ".join(parts), limit)
    if kind in {"edit", "delete", "move"}:
        return _one_line(path or value("target") or "", limit)
    if kind == "search":
        pattern = value("pattern", "query") or "?"
        return _one_line(f'"{pattern}", {path or "."}', limit)
    if kind == "fetch":
        return _one_line(value("url", "query") or "", limit)
    if kind == "think":
        return _one_line(value("thought", "text") or "", limit)

    pairs = list(raw.items())[:2]
    return _one_line(", ".join(f"{key}={item!r}" for key, item in pairs), limit)


def _diff_counts(event: Any) -> tuple[int, int] | None:
    """Changed-line counts for ACP file-edit content, when present."""
    added = removed = 0
    found = False
    for block in getattr(event, "content", ()) or ():
        if not isinstance(block, dict) or block.get("type") != "diff":
            continue
        found = True
        old_lines = str(block.get("oldText") or "").splitlines()
        new_lines = str(block.get("newText") or "").splitlines()
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in {"replace", "delete"}:
                removed += i2 - i1
            if tag in {"replace", "insert"}:
                added += j2 - j1
    return (added, removed) if found else None


def _tool_content_parts(event: Any) -> list[str]:
    parts: list[str] = []
    for block in getattr(event, "content", ()) or ():
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "content":
            content = block.get("content") or {}
            if content.get("type") == "text":
                parts.append(str(content.get("text", "")))
            elif content.get("type") == "image":
                parts.append("[image output]")
            else:
                rendered = _render_json(content)
                if rendered:
                    parts.append(rendered)
        elif kind == "diff":
            path = block.get("path", "")
            old = block.get("oldText")
            new = block.get("newText", "")
            rendered = f"--- {path}\n+++ {path}\n"
            if old:
                rendered += "".join(f"- {line}\n" for line in str(old).splitlines())
            rendered += "".join(f"+ {line}\n" for line in str(new).splitlines())
            parts.append(rendered)
        elif kind == "terminal":
            terminal_id = block.get("terminalId", "")
            command = block.get("command") or block.get("title") or ""
            label = f"terminal {terminal_id}".strip()
            if command:
                label += f"\n$ {command}"
            output = block.get("output")
            if output not in (None, ""):
                label += "\n" + str(output)
            parts.append(label)
        else:
            # Unknown future ACP content stays visible as bounded JSON rather
            # than silently disappearing from the generic detail view.
            rendered = _render_json(block)
            if rendered:
                parts.append(f"{kind or 'activity'}\n{rendered}")
    return parts


def _tool_output_part(event: Any) -> str:
    raw = getattr(event, "raw_output", None)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    rendered = _render_json(raw)
    return f"output\n{rendered}" if rendered else ""


def _join_parts(parts: list[str], limit: int) -> str:
    text = "\n\n".join(part for part in parts if part).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n… truncated at {limit:,} characters"
    return text


def _output_already_shown(parts: list[str], output: str) -> bool:
    """Whether every non-empty output line is already in a content block."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return True
    existing = {line.strip() for part in parts for line in part.splitlines()}
    return all(line in existing for line in lines)


def tool_result_text(event: Any, *, limit: int = UI_MAX_TOOL_CONTENT_CHARS) -> str:
    """The result body only: content blocks and raw output, not arguments."""
    parts = _tool_content_parts(event)
    output = _tool_output_part(event)
    # Some peers repeat the raw output inside a content block; prefer the
    # richer content version instead of showing it twice.
    if output and not _output_already_shown(parts, output):
        parts.append(output)
    return _join_parts(parts, limit)


def tool_detail_text(event: Any, *, limit: int = UI_MAX_TOOL_CONTENT_CHARS) -> str:
    """Full detail for the expanded view, including call arguments."""
    parts = _tool_content_parts(event)
    output = _tool_output_part(event)
    if output and not _output_already_shown(parts, output):
        parts.append(output)
    if getattr(event, "raw_input", None) is not None:
        rendered = _render_json(event.raw_input)
        if rendered:
            parts.append("input\n" + rendered)
    for location in getattr(event, "locations", ()) or ():
        if isinstance(location, dict) and location.get("path"):
            line = f":{location['line']}" if location.get("line") else ""
            parts.append(f"location {location['path']}{line}")
    return _join_parts(parts, limit)


def _render_json(value: Any) -> str:
    import json

    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        rendered = str(value)
    return rendered[:4000]


def merge_tool_event(existing: dict[str, Any] | None, event: Any) -> dict[str, Any]:
    """Merge a progress update into the retained view of one tool call."""
    merged = dict(existing or {})
    merged["toolCallId"] = getattr(event, "tool_call_id", "") or merged.get("toolCallId", "")
    title = getattr(event, "title", "") or ""
    if title:
        merged["title"] = title
    kind = getattr(event, "kind", "") or ""
    if kind or event.is_new:
        merged["kind"] = kind
    raw_input = getattr(event, "raw_input", None)
    if raw_input is not None:
        merged["raw_input"] = raw_input
    status = getattr(event, "status", "") or ""
    if status:
        merged["status"] = status
    result = tool_result_text(event)
    if result:
        merged["result"] = result
    detail = tool_detail_text(event)
    if detail:
        merged["detail"] = detail
    counts = _diff_counts(event)
    if counts is not None:
        merged["diffAdded"], merged["diffRemoved"] = counts
    meta = getattr(event, "meta", None)
    if isinstance(meta, dict) and meta:
        existing_meta = merged.get("meta")
        merged["meta"] = {
            **(existing_meta if isinstance(existing_meta, dict) else {}),
            **meta,
        }
    return merged


def presentation_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """Find an optional versioned presentation payload in trusted tool metadata.

    Peers carry this as opaque JSON (``presentation`` in ACP ``_meta`` or in the
    ``harness`` metadata envelope). It is data only: only a locally registered
    renderer can turn it into executable presentation code.
    """
    candidates: list[Any] = []
    for key in ("presentation", "harnessPresentation", "presentationPayload"):
        candidates.append(data.get(key))
    for meta_key in ("meta", "_meta"):
        meta = data.get(meta_key)
        if not isinstance(meta, dict):
            continue
        for key in ("presentation", "harnessPresentation", "presentationPayload"):
            candidates.append(meta.get(key))
        harness = meta.get("harness")
        if isinstance(harness, dict):
            candidates.append(harness.get("presentation"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("rendererId") or candidate.get("renderer_id"):
            return candidate
    return None


_SUBAGENT_MARKERS = (
    "delegate_task",
    "delegate task",
    "delegation",
    "subagent",
    "sub-agent",
    "spawn agent",
    "spawn_agent",
)


def is_subagent_tool(data: dict[str, Any]) -> bool:
    """Whether an ordinary tool activity represents delegated child-agent work.

    ACP has no subagent update type. A peer that delegates exposes it as a tool
    call; classify only explicit delegation names so unrelated tools never
    become a fabricated "subagent" view.
    """
    parts = [
        str(data.get("title") or ""),
        str(data.get("kind") or ""),
    ]
    raw = data.get("raw_input")
    if isinstance(raw, dict):
        parts.extend(str(key) for key in raw)
    haystack = " ".join(parts).lower()
    return any(marker in haystack for marker in _SUBAGENT_MARKERS)


def config_option_value(option: dict[str, Any]) -> str:
    if option.get("type") == "boolean":
        return "on" if option.get("currentValue") else "off"
    current = option.get("currentValue")
    for item in option.get("options") or []:
        if isinstance(item, dict) and item.get("value") == current:
            return str(item.get("name", current))
    for group in option.get("groups") or []:
        for item in group.get("options") or []:
            if isinstance(item, dict) and item.get("value") == current:
                return str(item.get("name", current))
    return str(current if current is not None else "—")


def option_values(option: dict[str, Any]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for item in option.get("options") or []:
        if isinstance(item, dict):
            values.append(
                {
                    "value": str(item.get("value", "")),
                    "name": str(item.get("name", item.get("value", ""))),
                    "description": str(item.get("description") or ""),
                }
            )
    for group in option.get("groups") or []:
        for item in group.get("options") or []:
            if isinstance(item, dict):
                values.append(
                    {
                        "value": str(item.get("value", "")),
                        "name": f"{group.get('name', '')} / {item.get('name', item.get('value', ''))}",
                        "description": str(item.get("description") or ""),
                    }
                )
    return values


def effort_option(
    options: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the local runtime's unified reasoning-effort option, if advertised."""
    for option in options:
        if isinstance(option, dict) and option.get("id") == "effort":
            return option
    return None


def model_option(
    options: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the option that selects the model, if the agent exposes one."""
    candidates: list[dict[str, Any]] = []
    for option in options:
        if not isinstance(option, dict) or option.get("type") != "select":
            continue
        key = (
            f"{option.get('id', '')} {option.get('name', '')} {option.get('category', '')}".lower()
        )
        if "model" in key:
            candidates.append(option)
    for preferred in ("model", "models"):
        for option in candidates:
            if option.get("id") == preferred:
                return option
    return candidates[0] if candidates else None


def format_usage(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "Cost unavailable"
    cost = usage.get("cost")
    status = usage.get("cost_status", "estimated")
    currency = "USD"
    if isinstance(cost, dict):
        currency = cost.get("currency", "USD")
        cost = cost.get("amount")
    try:
        amount = Decimal(str(cost))
        if not amount.is_finite():
            raise ValueError("Non-finite cost")
        parts = [f"{currency} {amount:.6f} ({status})"]
    except (InvalidOperation, ValueError, TypeError):
        parts = ["Cost unavailable"]
    for camel, snake, label in (
        ("inputTokens", "input_tokens", "in"),
        ("outputTokens", "output_tokens", "out"),
        ("cachedReadTokens", "cache_read_tokens", "cache read"),
        ("cachedWriteTokens", "cache_write_tokens", "cache write"),
    ):
        value = usage.get(camel, usage.get(snake))
        if isinstance(value, int):
            parts.append(f"{label} {value:,}")
    return " · ".join(parts)


def binding_summary(binding: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Compute display state from a descriptor plus an optional snapshot."""
    binding_id = binding.get("bindingId") or binding.get("binding_id") or ""
    policy = binding.get("policy") or {}
    enabled = bool(binding.get("enabled", False))
    active_enabled = binding.get("activeEnabled", binding.get("active_enabled"))
    available = bool(policy.get("available", True))
    config = binding.get("config") or {}
    state: dict[str, Any] = {
        "bindingId": binding_id,
        "name": binding.get("name") or binding_id,
        "description": binding.get("description", ""),
        "pluginId": binding.get("pluginId") or binding.get("plugin_id") or "",
        "version": binding.get("version", ""),
        "enabled": enabled,
        "activeEnabled": active_enabled,
        "available": available,
        "locked": not available,
        "reason": binding.get("availabilityReason") or binding.get("availability_reason") or "",
        "canToggle": bool(policy.get("clientCanToggle", policy.get("client_can_toggle", False))),
        "changeMode": binding.get("changeMode")
        or binding.get("change_mode")
        or policy.get("changeMode")
        or "new_session",
        "requires": tuple(binding.get("requires") or ()),
        "conflicts": tuple(binding.get("conflicts") or ()),
        "editableFields": tuple(
            policy.get("editableFields") or policy.get("editable_fields") or ()
        ),
        "configSchema": binding.get("configSchema") or binding.get("config_schema") or {},
        "config": config,
        "defaults": dict(binding.get("defaults") or {}),
        "enabledByDefault": bool(
            policy.get("enabledByDefault", policy.get("enabled_by_default", False))
        ),
        "stateMode": binding.get("stateMode") or binding.get("state_mode") or "run",
    }
    if snapshot:
        states = snapshot.get("bindings") or {}
        selected_state = states.get(binding_id) if isinstance(states, dict) else None
        state["state"] = selected_state
        if isinstance(selected_state, dict):
            if "enabled" in selected_state:
                state["enabled"] = bool(selected_state["enabled"])
            if "config" in selected_state and isinstance(selected_state["config"], dict):
                state["config"] = selected_state["config"]
    state["pending"] = (
        bool(snapshot)
        and snapshot.get("selectedRevision") is not None
        and snapshot.get("activeRevision") is not None
        and snapshot.get("selectedRevision") != snapshot.get("activeRevision")
    )
    state["modeLabel"] = "next turn" if state["changeMode"] == "next_turn" else "new session"
    return state


def pending_application_text(binding: dict[str, Any], snapshot: dict[str, Any] | None) -> str:
    if not binding.get("pending"):
        return ""
    active = binding.get("activeEnabled")
    if active is None:
        return "Selected change applies at the next turn."
    if bool(active) != bool(binding["enabled"]):
        wanted = "on" if binding["enabled"] else "off"
        current = "on" if active else "off"
        return f"Selected {wanted} for the next turn; still {current} in the active revision."
    return ""


def build_form_fields(
    schema: dict[str, Any], current: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Describe form fields from a JSON Schema subset used by config/elicitation."""
    properties = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    current = current or {}
    fields: list[dict[str, Any]] = []
    if not isinstance(properties, dict):
        return fields
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        field_type = str(spec.get("type") or "string")
        kind = field_type
        choices: list[tuple[str, str]] = []
        item_type = "string"
        if field_type == "string" and isinstance(spec.get("enum"), list):
            kind = "select"
            choices = [(str(item), str(item)) for item in spec["enum"]]
        elif field_type == "array":
            items = spec.get("items") or {}
            if isinstance(items, dict):
                item_type = str(items.get("type") or "string")
            if isinstance(items, dict) and isinstance(items.get("enum"), list):
                kind = "multi"
                choices = [(str(item), str(item)) for item in items["enum"]]
            else:
                # Comma-separated input is the terminal-friendly representation
                # for an array without a closed set of choices.
                kind = "array"
        fields.append(
            {
                "name": name,
                "title": str(spec.get("title") or name.replace("_", " ")),
                "description": str(spec.get("description") or ""),
                "kind": kind,
                "choices": choices,
                "itemType": item_type,
                "required": name in required,
                "default": spec.get("default", current.get(name)),
            }
        )
    return fields


def coerce_value(field: dict[str, Any], raw: Any) -> Any:
    """Convert a widget value into the JSON-schema type; raise ValueError on bad input."""
    kind = field.get("kind")
    if kind == "boolean":
        return bool(raw)
    if kind == "integer":
        if raw in (None, ""):
            return None
        return int(str(raw))
    if kind == "number":
        if raw in (None, ""):
            return None
        return float(str(raw))
    if kind == "multi":
        if isinstance(raw, list):
            return raw
        if not raw:
            return []
        return [item.strip() for item in str(raw).split(",") if item.strip()]
    if kind == "array":
        if isinstance(raw, list):
            items = raw
        elif not raw:
            return []
        else:
            items = [item.strip() for item in str(raw).split(",") if item.strip()]
        item_type = field.get("itemType")
        try:
            if item_type == "integer":
                return [int(item) for item in items]
            if item_type == "number":
                return [float(item) for item in items]
        except (TypeError, ValueError) as exc:
            raise ValueError("array items need the declared numeric type") from exc
        return items
    if raw is None:
        return ""
    return str(raw)


@dataclass
class Settings:
    """Terminal preferences persisted beside profiles."""

    theme: str = "harness"
    auto_expand_tools: bool = False
    show_thoughts: bool = False
    side_panel: bool = True
    permission_mode: str = "ask"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> Settings:
        mapping = mapping or {}
        mode = str(mapping.get("permissionMode", mapping.get("permission_mode", "ask")))
        if mode not in PERMISSION_MODES:
            mode = "ask"
        return cls(
            theme=str(mapping.get("theme", "harness")),
            auto_expand_tools=bool(
                mapping.get("autoExpandTools", mapping.get("auto_expand_tools", False))
            ),
            show_thoughts=bool(mapping.get("showThoughts", mapping.get("show_thoughts", False))),
            side_panel=bool(mapping.get("sidePanel", mapping.get("side_panel", True))),
            permission_mode=mode,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "auto_expand_tools": self.auto_expand_tools,
            "show_thoughts": self.show_thoughts,
            "side_panel": self.side_panel,
            "permission_mode": self.permission_mode,
        }
