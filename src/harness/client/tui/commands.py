"""Terminal command registry.

Local commands are part of the client, not the agent. The registry keeps the
name, aliases and help text in one place so the inline picker, palette, help
screen and slash dispatcher cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "LOCAL_COMMANDS",
    "LocalCommand",
    "command_names",
    "command_suggestion_text",
    "matches_command",
    "resolve_local_command",
]


@dataclass(frozen=True, slots=True)
class LocalCommand:
    """One terminal command and the aliases kept for compatibility."""

    name: str
    description: str
    aliases: tuple[str, ...] = ()
    takes_argument: bool = False
    hidden: bool = False

    def matches(self, needle: str) -> bool:
        needle = needle.strip().lower()
        if not needle:
            return True
        names = (self.name, *self.aliases)
        return any(needle in name.lower() for name in names) or needle in self.description.lower()


LOCAL_COMMANDS: tuple[LocalCommand, ...] = (
    LocalCommand("connect", "Choose a connection or add a remote/ACP target"),
    LocalCommand(
        "models",
        "Select or configure the local provider and model",
        aliases=("model",),
    ),
    LocalCommand("effort", "Set the local model's reasoning effort"),
    LocalCommand(
        "plugins",
        "Enable, disable, add or edit plugins",
        aliases=("capabilities",),
    ),
    LocalCommand("mcp", "Show MCP servers, tools and connection state"),
    LocalCommand(
        "todo",
        "Show the session's plan and progress",
        aliases=("todos",),
    ),
    LocalCommand("rename", "Rename the current session", takes_argument=True),
    LocalCommand("sessions", "List, load, resume, close or delete sessions"),
    LocalCommand("find", "Find text in the retained conversation", takes_argument=True),
    LocalCommand("copy", "Copy the latest completed assistant response"),
    LocalCommand(
        "skills",
        "Browse discovered skills and insert a $reference",
        takes_argument=True,
    ),
    LocalCommand("permissions", "Choose how tool approvals are answered"),
    LocalCommand("settings", "Terminal preferences"),
    LocalCommand("attach", "Stage a local file for the next message", takes_argument=True),
    LocalCommand("help", "Keyboard map and commands"),
    # Routing prefix for agent-advertised commands with a local name collision.
    LocalCommand(
        "agent",
        "Run an agent-advertised command by name",
        takes_argument=True,
        hidden=True,
    ),
)

_BY_NAME: dict[str, LocalCommand] = {}
for _command in LOCAL_COMMANDS:
    _BY_NAME[_command.name] = _command
    for _alias in _command.aliases:
        _BY_NAME[_alias] = _command


def resolve_local_command(name: str) -> LocalCommand | None:
    """Resolve a canonical name or compatibility alias."""
    return _BY_NAME.get(name.strip().lower())


def command_names(*, include_aliases: bool = True) -> frozenset[str]:
    if include_aliases:
        return frozenset(_BY_NAME)
    return frozenset(command.name for command in LOCAL_COMMANDS)


def matches_command(command: LocalCommand, needle: str) -> bool:
    return command.matches(needle)


def command_suggestion_text(command: LocalCommand) -> str:
    """Compact one-line description used by the inline picker."""
    suffix = ""
    if command.aliases:
        suffix = "  (alias: " + ", ".join(f"/{alias}" for alias in command.aliases) + ")"
    return f"/{command.name}   {command.description}{suffix}"
