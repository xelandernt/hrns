"""Typed events published by :class:`~hrns.client.client.HrnsAcpClient`.

Events are plain frozen dataclasses so both the Textual UI and the headless
runner can consume them without depending on UI types or on live ACP schema
objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .constants import CLIENT_NAME

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hrns.protocol import ConfigurationSnapshot

#: Rendered on the header, like ``{"kind": "local", "label": "/work/dir"}``.
LocationInfo = dict[str, str]


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """Connection lifecycle: ``connecting`` -> ``ready`` -> ``lost``."""

    state: str
    detail: str = ""
    location: LocationInfo = field(default_factory=dict)
    agent_name: str = CLIENT_NAME
    exception: BaseException | None = None


@dataclass(frozen=True, slots=True)
class UserMessageReplayed:
    """A prior user prompt replayed by ``session/load`` (not a new submission)."""

    session_id: str
    text: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTextChunk:
    """Incremental assistant text for the current turn."""

    session_id: str
    text: str
    message_id: str | None = None
    thought: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolEvent:
    """A tool call starts or progresses; ``status`` is pending/in_progress/completed."""

    session_id: str
    tool_call_id: str
    title: str
    kind: str
    status: str
    is_new: bool
    content: tuple[dict[str, Any], ...] = ()
    locations: tuple[dict[str, str], ...] = ()
    raw_input: Any = None
    raw_output: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanChanged:
    """The agent's plan was updated; ``cleared`` marks a removal update."""

    session_id: str
    entries: tuple[tuple[str, str, str], ...] = ()  # (content, priority, status)
    cleared: bool = False


@dataclass(frozen=True, slots=True)
class CommandsUpdated:
    """Agent-advertised commands (``available_commands_update``)."""

    session_id: str
    commands: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PluginCommandsListed:
    """User-only commands contributed by enabled hrns plugin bindings."""

    session_id: str
    commands: tuple[Any, ...] = ()
    selected_revision: int = 0
    supported_operations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolPresentationUpdated:
    """Supplemental presentation snapshots keyed by session and tool-call id."""

    snapshots: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class ModesUpdated:
    """Session mode state changed."""

    session_id: str
    current_mode_id: str
    available_modes: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigOptionsUpdated:
    """The standard ACP configuration option list was replaced by the agent."""

    session_id: str
    options: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigurationChanged:
    """The project extension published a complete configuration snapshot."""

    session_id: str
    snapshot: ConfigurationSnapshot


@dataclass(frozen=True, slots=True)
class UsageUpdated:
    """Usage numbers for the running or completed turn."""

    session_id: str
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """The user submitted a prompt and the agent began working."""

    session_id: str
    prompt_text: str


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """The prompt request returned with a stop reason."""

    session_id: str
    stop_reason: str
    usage: dict[str, Any] | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CapabilitiesListed:
    """The paginated extension capability list result."""

    session_id: str | None
    bindings: tuple[dict[str, Any], ...] = ()
    selected_revision: int | None = None
    policy_revision: int | None = None
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class PluginListed:
    """Paginated plugins offered for local binding creation."""

    plugins: tuple[Any, ...] = ()
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class SessionsListed:
    """``session/list`` result used by the sessions panel."""

    sessions: tuple[dict[str, Any], ...] = ()
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class SessionInfoChanged:
    session_id: str
    title: str | None = None
