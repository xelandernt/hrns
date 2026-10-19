"""Pure helpers for the terminal UI: formatting, option state, form coercion.

Kept free of widgets so the behaviour can be unit tested without a running
application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..constants import UI_MAX_TOOL_CONTENT_CHARS

__all__ = [
    "build_form_fields",
    "binding_summary",
    "coerce_value",
    "config_option_value",
    "format_usage",
    "is_subagent_tool",
    "local_command_names",
    "merge_tool_event",
    "model_option",
    "option_values",
    "pending_application_text",
    "presentation_payload",
    "tool_detail_text",
    "tool_status_label",
]

LOCAL_COMMANDS: tuple[tuple[str, str], ...] = (
    ("agents", "Choose a different agent or connection"),
    ("sessions", "List, load, resume, close or delete sessions"),
    ("capabilities", "Inspect and change session capabilities"),
    ("model", "Change the active model or mode"),
    ("settings", "Terminal preferences"),
    ("attach", "Stage a local file for the next message"),
    ("help", "Keyboard map and commands"),
)

LOCAL_COMMAND_NAMES = frozenset(name for name, _ in LOCAL_COMMANDS)

TOOL_STATUS_LABELS = {
    "pending": "○ pending",
    "in_progress": "◐ running",
    "completed": "● completed",
    "failed": "✕ failed",
    "": "○ queued",
}


def local_command_names() -> frozenset[str]:
    return LOCAL_COMMAND_NAMES


def tool_status_label(status: str) -> str:
    return TOOL_STATUS_LABELS.get(status or "", f"○ {status}")


def tool_detail_text(event: Any, *, limit: int = UI_MAX_TOOL_CONTENT_CHARS) -> str:
    """Readable details for any tool payload; specialized renderers come first."""
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
    if getattr(event, "raw_input", None) is not None:
        rendered = _render_json(event.raw_input)
        if rendered:
            parts.append("input\n" + rendered)
    if getattr(event, "raw_output", None) is not None:
        rendered = _render_json(event.raw_output)
        if rendered:
            parts.append("output\n" + rendered)
    for location in getattr(event, "locations", ()) or ():
        if isinstance(location, dict) and location.get("path"):
            line = f":{location['line']}" if location.get("line") else ""
            parts.append(f"location {location['path']}{line}")
    text = "\n\n".join(part for part in parts if part).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n… truncated at {limit:,} characters"
    return text


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
    status = getattr(event, "status", "") or ""
    if status:
        merged["status"] = status
    detail = tool_detail_text(event)
    if detail:
        merged["detail"] = detail[:UI_MAX_TOOL_CONTENT_CHARS]
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


def model_option(options: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the option that selects the model, if the agent exposes one."""
    candidates: list[dict[str, Any]] = []
    for option in options:
        if not isinstance(option, dict) or option.get("type") != "select":
            continue
        key = f"{option.get('id', '')} {option.get('name', '')} {option.get('category', '')}".lower()
        if "model" in key:
            candidates.append(option)
    for preferred in ("model", "models"):
        for option in candidates:
            if option.get("id") == preferred:
                return option
    return candidates[0] if candidates else None


def format_usage(usage: dict[str, Any] | None) -> str:
    if not usage:
        return ""
    parts: list[str] = []
    total = usage.get("totalTokens") or usage.get("total_tokens")
    if total is not None:
        parts.append(f"{int(total):,} tokens")
    cost = usage.get("cost") or {}
    if isinstance(cost, dict) and cost.get("amount") is not None:
        currency = cost.get("currency", "")
        parts.append(f"{cost.get('amount')} {currency}".strip())
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
        "canEnable": bool(policy.get("clientCanEnable", policy.get("client_can_enable", False))),
        "canDisable": bool(policy.get("clientCanDisable", policy.get("client_can_disable", False))),
        "changeMode": binding.get("changeMode") or binding.get("change_mode") or policy.get("changeMode") or "new_session",
        "requires": tuple(binding.get("requires") or ()),
        "conflicts": tuple(binding.get("conflicts") or ()),
        "editableFields": tuple(policy.get("editableFields") or policy.get("editable_fields") or ()),
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

def build_form_fields(schema: dict[str, Any], current: dict[str, Any] | None = None) -> list[dict[str, Any]]:
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
    show_thoughts: bool = True
    side_panel: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> Settings:
        mapping = mapping or {}
        return cls(
            theme=str(mapping.get("theme", "harness")),
            auto_expand_tools=bool(mapping.get("autoExpandTools", mapping.get("auto_expand_tools", False))),
            show_thoughts=bool(mapping.get("showThoughts", mapping.get("show_thoughts", True))),
            side_panel=bool(mapping.get("sidePanel", mapping.get("side_panel", True))),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "auto_expand_tools": self.auto_expand_tools,
            "show_thoughts": self.show_thoughts,
            "side_panel": self.side_panel,
        }
