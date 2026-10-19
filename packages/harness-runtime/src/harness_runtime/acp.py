"""ACP v1 mapping layer for the Harness host.

This module owns the *translation* between the runtime's abstract session surface and
the official ACP SDK schema. Nothing here holds live session state: the host
(:mod:`harness_runtime.host`) drives sessions and calls these pure helpers.

Design rules:

* The standard ACP surface comes first. Framework extensions are explicit
  ``_harness/*`` methods negotiated through ``initialize``'s ``_meta.harness``
  (see :func:`extension_advertisement` and :func:`parse_client_extensions`).
* Disabled capabilities are omitted from the runtime composition; this layer only
  *describes* state that the runtime policy controller already decided.
* Boolean configuration toggles are emitted only when the client advertises
  ``clientCapabilities.session.configOptions.boolean``; otherwise toggles are
  ``select`` options with ``on``/``off`` values (ACP v1 requirement).
* Tool-call presentation reuses the upstream harness's public presenter helpers
  (``pydantic_ai_harness.experimental.acp``), never its private adapter state.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any, TypeAlias

import acp
from acp import schema
from acp.exceptions import RequestError
from harness_config import EFFORT_DEFAULT, EFFORT_OFF, THINKING_LEVELS
from harness_protocol import (
    EXTENSION_METHODS,
    BindingChange,
    BindingDescriptor,
    ConfigurationSnapshot,
    ExtensionAdvertisement,
    HarnessError,
    changes_adapter,
)
from pydantic import ValidationError
from pydantic_ai.messages import ToolCallPart
from pydantic_ai_harness.experimental import HarnessExperimentalWarning

# The adapter is version-pinned and contract-tested; the warning is useful once
# at development time, not on every host process's protocol stderr.
warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

from pydantic_ai_harness.experimental.acp import (  # noqa: E402
    ToolCallPresentation,
    chain_presenters,
    default_coding_presenter,
)

#: The public upstream presenter signature (the alias itself is not exported by the
#: upstream package, but ``ToolCallPresentation``/``default_coding_presenter`` are).
ToolCallPresenter: TypeAlias = Callable[[ToolCallPart], ToolCallPresentation | None]

__all__ = [
    "BINDING_OPTION_PREFIX",
    "EFFORT_OPTION_ID",
    "EXTENSION_METHODS_V1",
    "MAX_TEXT_UPDATE_BYTES",
    "MODEL_OPTION_ID",
    "PLUGINS_LIST_METHOD",
    "absolutize_presentation",
    "changed_method_supported",
    "chunk_text",
    "config_options",
    "default_presenter",
    "effort_option",
    "event_to_update",
    "extension_advertisement",
    "harness_rpc_error",
    "humanize_tool_name",
    "initial_configuration_changes",
    "jsonable",
    "mcp_forbidden_error",
    "model_option",
    "model_view",
    "parse_client_extensions",
    "permission_options",
    "prompt_blocks_to_runtime",
    "replayable_event_type",
    "session_info_update",
    "stop_reason_for",
    "text_updates",
    "tool_call_part",
    "tool_call_progress_update",
    "tool_call_start_update",
    "turn_usage",
    "usage_update",
    "user_message_update",
    "validate_requested_cwd",
    "canonical_root",
]

#: ACP stdio is newline-delimited JSON read with a bounded buffer. Each text update is
#: kept well below 64 KiB *after JSON escaping*; the upstream adapter chunks the same way.
MAX_TEXT_UPDATE_BYTES = 48 * 1024

#: Stable id of the standard model selection option.
MODEL_OPTION_ID = "model"

#: Stable id of the local runtime's unified reasoning-effort option.
EFFORT_OPTION_ID = "effort"

#: Capability toggles are exposed as ``binding:<bindingId>`` standard options.
BINDING_OPTION_PREFIX = "binding:"

#: The project extension method for the local plugin inventory. The protocol package's
#: ``EXTENSION_METHODS`` tuple predates this method, so the host advertises the union.
PLUGINS_LIST_METHOD = "_harness/plugins/list"

#: The extension methods this host can serve (requests) plus the notification it may send.
EXTENSION_METHODS_V1: tuple[str, ...] = tuple(dict.fromkeys((*EXTENSION_METHODS, PLUGINS_LIST_METHOD)))

#: The notification the agent sends when configuration changed outside an update request.
CHANGED_METHOD = "_harness/configuration/changed"

_REPLAY_EXCLUDED = frozenset(
    {"permission_request", "turn_complete", "error", "config_changed"}
)


def jsonable(value: Any) -> Any:
    """Coerce a runtime value into something an ACP notification can serialize."""
    from pydantic_core import to_jsonable_python

    return to_jsonable_python(value, fallback=str, bytes_mode="base64")


def default_presenter() -> ToolCallPresenter:
    """The presenter chain installed by default: upstream coding-tool presentation."""
    return chain_presenters(default_coding_presenter)


def _escaped_len(char: str) -> int:
    if char in '"\\\b\f\n\r\t':
        return 2
    code = ord(char)
    if code < 0x20:
        return 6
    if code < 0x80:
        return 1
    if code < 0x10000:
        return 6
    return 12


def chunk_text(text: str, budget: int = MAX_TEXT_UPDATE_BYTES) -> Iterator[str]:
    """Split text so each chunk's JSON-escaped size stays inside ``budget`` bytes.

    Mirrors the upstream adapter's chunking semantics (which is why a non-ASCII
    character is accounted for as its escaped length rather than one byte).
    """
    chunk: list[str] = []
    size = 0
    for char in text:
        char_size = _escaped_len(char)
        if chunk and size + char_size > budget:
            yield "".join(chunk)
            chunk = []
            size = 0
        chunk.append(char)
        size += char_size
    if chunk:
        yield "".join(chunk)


def text_updates(text: str, *, thought: bool = False) -> Iterator[schema.SessionUpdate]:
    """Agent text (or thought) chunks sized for the stdio buffer."""
    for chunk in chunk_text(text):
        yield acp.update_agent_thought_text(chunk) if thought else acp.update_agent_message_text(chunk)


def user_message_update(text: str) -> schema.UserMessageChunk:
    """A replayable user-message chunk (used by ``session/load`` transcript replay)."""
    return acp.update_user_message_text(text)


def humanize_tool_name(name: str) -> str:
    """Readable fallback title for a tool call whose presenter offered none."""
    cleaned = (name or "tool").rsplit(".", 1)[-1].replace("_", " ").strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else "Tool"


def tool_call_part(tool_name: str, args: Any, tool_call_id: str) -> ToolCallPart:
    """Build the upstream ``ToolCallPart`` the public presenter API expects."""
    if not isinstance(args, Mapping | str):
        args = jsonable(args)
    return ToolCallPart(tool_name=tool_name or "tool", args=args, tool_call_id=tool_call_id)


def _presentation_for(
    tool_name: str,
    args: Any,
    tool_call_id: str,
    *,
    cwd: str,
    presenter: ToolCallPresenter,
) -> ToolCallPresentation:
    try:
        presentation = presenter(tool_call_part(tool_name, args, tool_call_id))
    except Exception:  # presentation must never break a turn
        presentation = None
    if presentation is None:
        return ToolCallPresentation()
    return absolutize_presentation(presentation, cwd)


def _resolve_within(path: str, cwd: str) -> str | None:
    """Resolve a workspace-relative path against ``cwd`` (``None`` when it escapes)."""
    if os.path.isabs(path):
        return path
    resolved = os.path.normpath(os.path.join(cwd, path))
    relative = os.path.relpath(resolved, os.path.normpath(cwd))
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    return resolved


def absolutize_presentation(
    presentation: ToolCallPresentation, cwd: str
) -> ToolCallPresentation:
    """Make a presentation's locations/diffs absolute for the ACP client.

    ACP requires tool-call locations and file-edit diffs to carry absolute paths.
    Absolute paths pass through; a relative path escaping ``cwd`` via ``..`` is
    dropped rather than pointed outside the workspace.
    """
    locations = tuple(
        location.model_copy(update={"path": resolved})
        for location in presentation.locations
        if (resolved := _resolve_within(location.path, cwd)) is not None
    )
    content: list[Any] = []
    for item in presentation.content:
        if isinstance(item, schema.FileEditToolCallContent):
            resolved = _resolve_within(item.path, cwd)
            if resolved is None:
                continue
            content.append(item.model_copy(update={"path": resolved}))
        else:
            content.append(item)
    return ToolCallPresentation(
        kind=presentation.kind,
        title=presentation.title,
        locations=locations,
        content=tuple(content),
    )


def _tool_data(data: Mapping[str, Any]) -> tuple[str, str, Any, str | None]:
    tool_call_id = str(data.get("tool_call_id") or data.get("toolCallId") or "")
    tool_name = str(data.get("tool_name") or data.get("toolName") or "tool")
    args = data.get("args") if "args" in data else data.get("raw_input")
    title = data.get("title")
    return tool_call_id, tool_name, args, str(title) if title is not None else None


def tool_call_start_update(
    data: Mapping[str, Any],
    *,
    cwd: str,
    presenter: ToolCallPresenter,
    default_status: schema.ToolCallStatus = "in_progress",
) -> schema.ToolCallStart:
    """Map a runtime ``tool_call`` event onto a rich ACP ``tool_call`` update."""
    tool_call_id, tool_name, args, explicit_title = _tool_data(data)
    if not tool_call_id:
        tool_call_id = f"tool-{abs(hash((tool_name, str(args)))):x}"
    presentation = _presentation_for(tool_name, args, tool_call_id, cwd=cwd, presenter=presenter)
    status = data.get("status") or default_status
    return acp.start_tool_call(
        tool_call_id=tool_call_id,
        title=explicit_title or presentation.title or humanize_tool_name(tool_name),
        kind=presentation.kind or (data.get("kind") if data.get("kind") in _TOOL_KINDS else None),
        status=status if status in _TOOL_STATUSES else default_status,
        content=list(presentation.content) or None,
        locations=list(presentation.locations) or None,
        raw_input=jsonable(args) if args is not None else None,
    )


_TOOL_KINDS = frozenset({"read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other"})
_TOOL_STATUSES = frozenset({"pending", "in_progress", "completed", "failed"})


def tool_call_progress_update(data: Mapping[str, Any]) -> schema.ToolCallProgress | None:
    """Map a runtime ``tool_update`` event onto an ACP ``tool_call_update``."""
    tool_call_id = str(data.get("tool_call_id") or data.get("toolCallId") or "")
    if not tool_call_id:
        return None
    status = data.get("status")
    title = data.get("title")
    output = data.get("output", data.get("raw_output"))
    raw_input = data.get("args", data.get("raw_input"))
    return acp.update_tool_call(
        tool_call_id=tool_call_id,
        title=str(title) if title is not None else None,
        status=status if status in _TOOL_STATUSES else None,
        raw_input=jsonable(raw_input) if raw_input is not None else None,
        raw_output=jsonable(output) if output is not None else None,
    )


def usage_update(data: Mapping[str, Any]) -> schema.UsageUpdate | None:
    """Context-window usage update, when the runtime knows the window size."""
    size = data.get("context_size", data.get("size"))
    used = data.get("context_used", data.get("used"))
    if not isinstance(size, int) or not isinstance(used, int) or size <= 0:
        return None
    return schema.UsageUpdate(session_update="usage_update", used=max(0, used), size=size)


def session_info_update(data: Mapping[str, Any]) -> schema.SessionInfoUpdate | None:
    title = data.get("title")
    updated_at = data.get("updated_at", data.get("updatedAt"))
    if title is None and updated_at is None:
        return None
    return schema.SessionInfoUpdate(
        session_update="session_info_update",
        title=str(title) if title is not None else None,
        updated_at=str(updated_at) if updated_at is not None else None,
    )


def turn_usage(data: Any) -> schema.Usage | None:
    """Map the runtime's turn usage summary onto ACP's UNSTABLE per-turn usage."""
    if not isinstance(data, Mapping):
        return None
    total = data.get("total_tokens", data.get("totalTokens"))
    input_tokens = data.get("input_tokens", data.get("inputTokens", 0))
    output_tokens = data.get("output_tokens", data.get("outputTokens", 0))
    if total is None:
        total = int(input_tokens or 0) + int(output_tokens or 0)
    try:
        usage = schema.Usage(
            total_tokens=max(0, int(total)),
            input_tokens=max(0, int(input_tokens or 0)),
            output_tokens=max(0, int(output_tokens or 0)),
        )
    except (TypeError, ValueError):
        return None
    for source, target in (
        ("thought_tokens", "thought_tokens"),
        ("cached_read_tokens", "cached_read_tokens"),
        ("cached_write_tokens", "cached_write_tokens"),
        ("cache_read_tokens", "cached_read_tokens"),
        ("cache_write_tokens", "cached_write_tokens"),
    ):
        value = data.get(source, data.get(_camel(source)))
        if isinstance(value, int) and value >= 0:
            setattr(usage, target, value)
    return usage


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


_STOP_REASONS = {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}


def stop_reason_for(value: Any, *, default: schema.StopReason = "end_turn") -> schema.StopReason:
    """Normalize the runtime's stop reason onto ACP's closed set."""
    if value == "length":
        return "max_tokens"
    if value == "content_filter":
        return "refusal"
    return value if value in _STOP_REASONS else default


def event_to_update(
    event_type: str,
    data: Mapping[str, Any],
    *,
    cwd: str,
    presenter: ToolCallPresenter,
) -> schema.SessionUpdate | None:
    """Translate one non-permission runtime event into an ACP session update.

    Returns ``None`` for events that are not renderable (turn completion, errors,
    configuration notifications) or that are live-only.
    """
    if event_type == "text_delta":
        text = data.get("text", data.get("delta", ""))
        return None if not isinstance(text, str) or not text else (
            acp.update_agent_thought_text(text) if data.get("thought") else acp.update_agent_message_text(text)
        )
    if event_type == "tool_call":
        return tool_call_start_update(data, cwd=cwd, presenter=presenter)
    if event_type == "tool_update":
        return tool_call_progress_update(data)
    if event_type == "usage":
        return usage_update(data)
    if event_type == "session_info":
        return session_info_update(data)
    if event_type == "user_message":
        text = data.get("text", "")
        return user_message_update(str(text)) if text else None
    return None


def replayable_event_type(event_type: str) -> bool:
    """Whether a stored event should be re-mapped during ``session/load`` replay."""
    return event_type not in _REPLAY_EXCLUDED and event_type != "config_changed"


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def permission_options(data: Mapping[str, Any]) -> list[schema.PermissionOption]:
    """Build ACP permission options from a runtime permission request.

    The runtime may supply explicit options; otherwise the conventional four
    (allow once/always, reject once/always) are advertised.
    """
    raw = data.get("options")
    options: list[schema.PermissionOption] = []
    if isinstance(raw, Sequence):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            option_id = item.get("option_id", item.get("optionId", item.get("id")))
            name = item.get("name", item.get("label"))
            kind = item.get("kind")
            if not option_id or not name or kind not in {"allow_once", "allow_always", "reject_once", "reject_always"}:
                continue
            options.append(schema.PermissionOption(option_id=str(option_id), name=str(name), kind=kind))
    if options:
        return options
    return [
        schema.PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
        schema.PermissionOption(option_id="allow-always", name="Always allow", kind="allow_always"),
        schema.PermissionOption(option_id="reject-once", name="Reject", kind="reject_once"),
        schema.PermissionOption(option_id="reject-always", name="Always reject", kind="reject_always"),
    ]


# ---------------------------------------------------------------------------
# Standard configuration options
# ---------------------------------------------------------------------------


def model_option(models: Sequence[str], current: str | None) -> schema.SessionConfigOptionSelect | None:
    """The standard ``model`` select option, or ``None`` when no models are offered."""
    if not models:
        return None
    current_value = current if current in models else models[0]
    return schema.SessionConfigOptionSelect(
        id=MODEL_OPTION_ID,
        name="Model",
        type="select",
        current_value=current_value,
        options=[schema.SessionConfigSelectOption(value=model, name=model) for model in models],
        description="Model used by the next turn.",
        category="model",
    )


_EFFORT_LABELS = {
    EFFORT_DEFAULT: ("Default", "Let the provider choose its default reasoning effort."),
    EFFORT_OFF: ("Off", "Disable reasoning when the model allows it."),
    "minimal": ("Minimal", "Fastest responses with the least reasoning."),
    "low": ("Low", "Light reasoning for straightforward work."),
    "medium": ("Medium", "Balanced reasoning for everyday work."),
    "high": ("High", "Deeper reasoning for harder work."),
    "xhigh": ("XHigh", "Maximum reasoning; highest cost and latency."),
}


def effort_option(
    current: str | None,
    *,
    support: str | None,
) -> schema.SessionConfigOptionSelect | None:
    """The unified ``effort`` select option, or ``None`` when the model cannot reason.

    ``support`` is ``"supported"``, ``"always"``, or ``"custom"`` (an id the
    registry cannot profile). Always-on models omit ``off`` because pydantic-ai
    silently ignores it there.
    """
    if support is None:
        return None
    values = [EFFORT_DEFAULT]
    if support != "always":
        values.append(EFFORT_OFF)
    values.extend(THINKING_LEVELS)
    current_value = current if current in values else EFFORT_DEFAULT
    return schema.SessionConfigOptionSelect(
        id=EFFORT_OPTION_ID,
        name="Reasoning effort",
        type="select",
        current_value=current_value,
        options=[
            schema.SessionConfigSelectOption(
                value=value, name=_EFFORT_LABELS[value][0], description=_EFFORT_LABELS[value][1]
            )
            for value in values
        ],
        description="Reasoning effort used by the next turn. Only models that support thinking show this.",
        category="model",
    )


def _binding_toggle_option(
    descriptor: BindingDescriptor,
    *,
    boolean: bool,
) -> schema.SessionConfigOptionBoolean | schema.SessionConfigOptionSelect | None:
    policy = descriptor.policy
    if (
        descriptor.change_mode != "next_turn"
        or not policy.available
        or descriptor.availability_reason is not None
    ):
        return None
    # Directional permission applies to a real transition: an enabled binding is editable
    # only when the client may disable it, and vice versa.
    editable = (descriptor.enabled and policy.client_can_disable) or (
        not descriptor.enabled and policy.client_can_enable
    )
    if not editable:
        return None
    option_id = f"{BINDING_OPTION_PREFIX}{descriptor.binding_id}"
    description = (
        f"{descriptor.description}\n\nChanges apply to the next turn; the running turn keeps "
        "its active revision."
        if descriptor.description
        else "Changes apply to the next turn; the running turn keeps its active revision."
    )
    if boolean:
        return schema.SessionConfigOptionBoolean(
            id=option_id,
            name=descriptor.name or descriptor.binding_id,
            type="boolean",
            current_value=descriptor.enabled,
            description=description,
            category="capability",
        )
    return schema.SessionConfigOptionSelect(
        id=option_id,
        name=descriptor.name or descriptor.binding_id,
        type="select",
        current_value="on" if descriptor.enabled else "off",
        options=[
            schema.SessionConfigSelectOption(value="on", name="On"),
            schema.SessionConfigSelectOption(value="off", name="Off"),
        ],
        description=description,
        category="capability",
    )


def config_options(
    snapshot: ConfigurationSnapshot,
    descriptors: Iterable[BindingDescriptor],
    *,
    models: Sequence[str] = (),
    current_model: str | None = None,
    current_effort: str | None = None,
    effort_support: str | None = None,
    boolean_toggles: bool = False,
) -> list[schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean]:
    """The complete standard ACP configuration option list for a session.

    Locked, unavailable, and ``new_session`` bindings are inspectable through the
    extension only; they are never offered as editable standard options.
    """
    options: list[schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean] = []
    model = model_option(models, current_model)
    if model is not None:
        options.append(model)
    effort = effort_option(current_effort, support=effort_support)
    if effort is not None:
        options.append(effort)
    for descriptor in descriptors:
        option = _binding_toggle_option(descriptor, boolean=boolean_toggles)
        if option is not None:
            options.append(option)
    return options


def model_view(models: Sequence[str], current: str | None) -> dict[str, Any] | None:
    """JSON view of model selection included in extension responses."""
    if not models:
        return None
    current_value = current if current in models else models[0]
    return {
        "currentValue": current_value,
        "options": [{"value": model, "name": model} for model in models],
    }


# ---------------------------------------------------------------------------
# Extension negotiation and payloads
# ---------------------------------------------------------------------------


def extension_advertisement(*, local_binding_creation: bool) -> ExtensionAdvertisement:
    methods = list(EXTENSION_METHODS_V1)
    if not local_binding_creation:
        methods = [method for method in methods if method != PLUGINS_LIST_METHOD]
    return ExtensionAdvertisement(
        version=1,
        methods=methods,
        local_binding_creation=bool(local_binding_creation),
    )


def parse_client_extensions(meta: Any) -> tuple[set[str], int | None]:
    """Read the client's ``_meta.harness`` advertisement.

    Accepts either an ``ExtensionAdvertisement``-shaped mapping or an object with the
    same fields; unknown major versions disable extension dispatch.
    """
    if not isinstance(meta, Mapping):
        return set(), None
    version = meta.get("version")
    methods = meta.get("methods")
    if not isinstance(version, int) or not isinstance(methods, Sequence) or isinstance(methods, str):
        return set(), None
    cleaned = {str(method) for method in methods if isinstance(method, str)}
    return cleaned, version


def changed_method_supported(methods: set[str]) -> bool:
    """Whether the client accepts the configuration-changed extension notification."""
    return CHANGED_METHOD in methods or "harness/configuration/changed" in methods


def initial_configuration_changes(meta: Any) -> list[BindingChange]:
    """Parse ``session/new`` ``_meta.harness.initialConfiguration`` into changes.

    The request is validated with the protocol package's discriminated union so a
    malformed transaction is rejected with an invalid-params error before a session
    is created. The runtime policy controller performs the semantic validation
    (new-session lifecycle, policy, dependencies).
    """
    if meta is None:
        return []
    if not isinstance(meta, Mapping):
        raise HarnessError("INVALID_CONFIGURATION", "_meta.harness must be an object.")
    raw = meta.get("initialConfiguration", meta.get("initial_configuration"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HarnessError(
            "INVALID_CONFIGURATION",
            "initialConfiguration must be a list of binding changes.",
            {"field": "initialConfiguration"},
        )
    try:
        return changes_adapter.validate_python(raw)
    except ValidationError as exc:
        raise HarnessError(
            "INVALID_CONFIGURATION",
            "initialConfiguration is not a valid binding-change transaction.",
            {"errors": exc.errors()[:10]},
        ) from exc


def prompt_blocks_to_runtime(prompt: Sequence[Any]) -> list[dict[str, Any]]:
    """Convert SDK prompt content blocks into runtime prompt blocks (JSON mappings)."""
    blocks: list[dict[str, Any]] = []
    for block in prompt:
        dumped = block.model_dump(mode="json", by_alias=True, exclude_none=True)
        blocks.append({key: value for key, value in dumped.items() if key != "_meta"})
    return blocks


# ---------------------------------------------------------------------------
# Working directories and errors
# ---------------------------------------------------------------------------


def canonical_root(path: str | os.PathLike[str]) -> str:
    """Canonical absolute form of an allowed workspace root."""
    return os.path.realpath(os.path.expanduser(os.fspath(path)))


def validate_requested_cwd(cwd: Any, *, allowed: Sequence[str] | None, hosted: bool) -> str:
    """Canonicalize a client-requested working directory and enforce host policy.

    The client never gets to expand the server's filesystem scope: relative and
    non-absolute paths are rejected, symlinks are resolved before comparison, and a
    hosted worker requires the canonical path to be equal to or below one of its
    configured roots. ``allowed is None`` means the local host does not restrict roots.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        raise HarnessError("INVALID_CONFIGURATION", "cwd must be a non-empty absolute path.")
    if not os.path.isabs(cwd):
        raise HarnessError(
            "INVALID_CONFIGURATION",
            "cwd must be an absolute path.",
            {"cwd": cwd},
        )
    candidate = os.path.realpath(cwd)
    if allowed is not None:
        roots = [canonical_root(root) for root in allowed]
        for root in roots:
            if candidate == root or root == os.sep or candidate.startswith(root + os.sep):
                return candidate
        raise HarnessError(
            "WORKSPACE_UNAVAILABLE",
            "The requested working directory is outside the workspace roots allowed for this deployment.",
            {"cwd": candidate, "allowedRoots": roots, "hosted": hosted},
        )
    return candidate


def harness_rpc_error(error: BaseException) -> RequestError:
    """Translate a :class:`HarnessError` (or generic failure) into an ACP RPC error.

    Project application errors use JSON-RPC server-error code ``-32000`` with
    ``error.data.code`` carrying the stable machine-readable value, exactly as
    ``HarnessError.to_rpc()`` defines.
    """
    if isinstance(error, HarnessError):
        return RequestError(-32000, error.message, {"code": error.code, "details": error.details})
    if isinstance(error, ValidationError):
        return RequestError.invalid_params({"errors": error.errors()[:10]})
    if isinstance(error, RequestError):
        return error
    return RequestError.internal_error({"details": str(error) or type(error).__name__})


def mcp_forbidden_error(*, hosted: bool, servers: Sequence[Any]) -> HarnessError:
    """Stable rejection for client-supplied MCP servers the host cannot authorize."""
    if hosted:
        return HarnessError(
            "POLICY_DENIED",
            "Client-supplied MCP servers are prohibited in hosted mode; MCP endpoints are "
            "authorized by the deployment.",
            {"mcpServerCount": len(servers)},
        )
    return HarnessError(
        "INVALID_CONFIGURATION",
        "This agent does not accept client-supplied MCP servers; MCP is integrated through "
        "a declared definition resource.",
        {"mcpServerCount": len(servers)},
    )


