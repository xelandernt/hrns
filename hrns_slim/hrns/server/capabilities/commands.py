"""Capability-advertised slash commands for ACP sessions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from inspect import isawaitable
from typing import Generic, Protocol, TypeAlias, runtime_checkable

from acp import schema
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.capabilities import AbstractCapability, CombinedCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.output import OutputDataT
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.usage import RunUsage

__all__ = [
    "CommandContext",
    "CommandHandled",
    "CommandHandler",
    "CommandPrompt",
    "CommandResult",
    "SlashCommand",
    "SlashCommands",
    "SupportsSlashCommands",
    "find_slash_commands",
    "parse_slash_command",
]


@dataclass(frozen=True, kw_only=True)
class CommandHandled:
    """Returned by a slash command that completed the turn without calling the agent.

    ``message`` is optional plain text emitted as an agent message if the command did not
    already emit its own structured ACP updates. ``usage`` and ``cost`` let commands that
    invoke a model (such as ``/compact``) account for their token usage and spend.
    """

    message: str | None = None
    usage: RunUsage | None = None
    cost: Decimal | None = None
    stop_reason: schema.StopReason = "end_turn"


@dataclass(frozen=True, kw_only=True)
class CommandPrompt:
    """Returned by a slash command that rewrites the turn into a prompt for the agent."""

    text: str


CommandResult: TypeAlias = CommandHandled | CommandPrompt | None


@dataclass(kw_only=True)
class CommandContext(Generic[AgentDepsT]):
    """Session state and helpers exposed to a capability slash-command handler."""

    session_id: str
    cwd: str
    command: str
    arguments: str
    history: list[ModelMessage]
    deps: AgentDepsT
    model: Model | str | None
    model_settings: ModelSettings | None
    emit_update: Callable[
        [
            schema.UserMessageChunk
            | schema.AgentMessageChunk
            | schema.AgentThoughtChunk
            | schema.ToolCallStart
            | schema.ToolCallProgress
            | schema.AgentPlanUpdate
            | schema.AvailableCommandsUpdate
            | schema.CurrentModeUpdate
            | schema.ConfigOptionUpdate
            | schema.SessionInfoUpdate
            | schema.UsageUpdate
            | schema.SessionUpdateCompactionUpdate
            | schema.SessionUpdateCompactionSummaryChunk
        ],
        Awaitable[None],
    ]
    emit_text: Callable[[str], Awaitable[None]]
    emit_context_usage: Callable[[int, Decimal | None], Awaitable[None]]
    replace_history: Callable[[list[ModelMessage]], None]


CommandHandler: TypeAlias = Callable[
    [CommandContext[AgentDepsT]],
    CommandResult | Awaitable[CommandResult],
]


@dataclass(frozen=True, kw_only=True)
class SlashCommand(Generic[AgentDepsT]):
    """One slash command advertised over ACP ``available_commands_update``."""

    name: str
    description: str
    input_hint: str | None = None
    handler: CommandHandler[AgentDepsT] | None = None

    def __post_init__(self) -> None:
        normalized = self.name.strip().lstrip("/")
        if not normalized or any(ch.isspace() for ch in normalized):
            raise ValueError(f"Invalid slash command name: {self.name!r}")
        object.__setattr__(self, "name", normalized)

    def to_acp(self) -> schema.AvailableCommand:
        """Convert to ACP's ``AvailableCommand`` schema."""
        command_input = (
            schema.AvailableCommandInput(root=schema.UnstructuredCommandInput(hint=self.input_hint))
            if self.input_hint
            else None
        )
        return schema.AvailableCommand(
            name=self.name,
            description=self.description,
            input=command_input,
        )


@runtime_checkable
class SupportsSlashCommands(Protocol[AgentDepsT]):
    """Protocol implemented by capabilities that advertise ACP slash commands."""

    def get_slash_commands(self) -> Sequence[SlashCommand[AgentDepsT]]: ...


@dataclass
class SlashCommands(AbstractCapability[AgentDepsT]):
    """Capability that advertises one or more slash commands over ACP.

    Attach this directly to an agent to expose custom commands without defining a custom
    capability subclass::

        agent = Agent(
            "openai:gpt-5.4",
            capabilities=[
                SlashCommands(
                    [
                        SlashCommand(
                            name="status",
                            description="Show session history size",
                            handler=handle_status,
                        ),
                    ]
                )
            ],
        )
    """

    commands: Sequence[SlashCommand[AgentDepsT]] = ()
    id: str | None = "hrns.slash_commands"

    def get_slash_commands(self) -> Sequence[SlashCommand[AgentDepsT]]:
        return tuple(self.commands)


def find_slash_commands(
    agent: AbstractAgent[AgentDepsT, OutputDataT],
    extra_capabilities: Sequence[AbstractCapability[AgentDepsT]] = (),
) -> tuple[SlashCommand[AgentDepsT], ...]:
    """Collect advertised slash commands from an agent's capabilities in tree order.

    First registration wins when two capabilities advertise the same command name.
    """
    ordered: list[SlashCommand[AgentDepsT]] = []
    seen: set[str] = set()

    def walk(capability: AbstractCapability[AgentDepsT]) -> None:
        if isinstance(capability, CombinedCapability):
            for child in capability.capabilities:
                walk(child)
        if isinstance(capability, SupportsSlashCommands):
            for command in capability.get_slash_commands():
                key = command.name.lower()
                if key not in seen:
                    seen.add(key)
                    ordered.append(command)

    walk(agent.root_capability)
    for capability in extra_capabilities:
        walk(capability)
    return tuple(ordered)


def parse_slash_command(text: str) -> tuple[str, str] | None:
    """Parse ``/name [arguments]`` from a plain text prompt, or return ``None``."""
    stripped = text.strip()
    if not stripped.startswith("/") or len(stripped) < 2:
        return None
    body = stripped[1:]
    head, _, rest = body.partition(" ")
    if not head or any(ch.isspace() for ch in head):
        return None
    return head, rest.strip()


async def run_command_handler(
    command: SlashCommand[AgentDepsT],
    ctx: CommandContext[AgentDepsT],
) -> CommandResult:
    """Invoke a command handler, awaiting it if asynchronous."""
    if command.handler is None:
        return None
    result = command.handler(ctx)
    return await result if isawaitable(result) else result
