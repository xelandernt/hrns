"""Author-facing command contracts.

A plugin contributes commands that are *user actions*, not agent tools. Handlers receive a
validated configuration and a small context of authorized resources; they never receive a
fake ``RunContext``, mutable session history, or adapter internals. The runtime imports no UI
library here.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from harness.protocol import ConfigurationSnapshot, HarnessError
from harness.protocol.extensions import (
    SUPPORTED_CONVERSATION_OPERATIONS,
    CommandDescriptor,
    CommandResult,
    StartTurn,
)

from .resources import ResourceSet

__all__ = [
    "Command",
    "CommandArguments",
    "CommandBinding",
    "CommandContext",
    "TextArguments",
    "build_command_descriptor",
]

_COMMAND_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_CONVERSATION_OPERATIONS = frozenset(SUPPORTED_CONVERSATION_OPERATIONS)


class CommandArguments(BaseModel):
    """Base class for structured command arguments."""

    model_config = ConfigDict(extra="forbid")


class TextArguments(BaseModel):
    """Default argument model: the raw slash-tail text."""

    model_config = ConfigDict(extra="forbid")

    text: str = ""


@dataclass
class CommandContext:
    """Authorized, invocation-scoped context for one command handler."""

    binding_id: str
    plugin_id: str
    session_id: str
    principal: str
    deployment_id: str
    config: Any
    resources: ResourceSet | None = None
    configuration: ConfigurationSnapshot | None = None
    supported_operations: frozenset[str] = _CONVERSATION_OPERATIONS
    _history_reader: Callable[[], Any] | None = field(default=None, repr=False)

    def can(self, operation: str) -> bool:
        """Whether this host can perform one declared conversation operation."""
        return operation in self.supported_operations

    def history(self) -> list[Any]:
        """A deep, read-only snapshot of committed native messages, newest last."""
        if self._history_reader is None:
            return []
        try:
            messages = self._history_reader()
        except Exception:
            return []
        if not messages:
            return []
        try:
            return copy.deepcopy(list(messages))
        except Exception:
            return []

    def start_turn(self, prompt: str, *, display_text: str | None = None) -> CommandResult:
        """Build a result that explicitly starts one normal agent turn."""
        if not self.can("start_turn"):
            raise HarnessError(
                "UNSUPPORTED_OPERATION",
                "This host cannot start an agent turn from a command.",
                details={"operation": "start_turn"},
            )
        return CommandResult(conversation=StartTurn(prompt=prompt, display_text=display_text))


ArgsT = TypeVar("ArgsT", bound=BaseModel)


@dataclass(frozen=True)
class Command(Generic[ArgsT]):
    """One command contributed by a plugin binding.

    ``handler`` is asynchronous. ``parser`` converts raw slash-tail text into a mapping that
    ``args_model`` validates; without a parser the whole tail is ``TextArguments.text``.
    """

    name: str
    handler: Callable[[CommandContext, ArgsT], Awaitable[CommandResult]]
    description: str = ""
    args_model: type[ArgsT] = TextArguments  # type: ignore[assignment]
    parser: Callable[[str], Mapping[str, Any]] | None = None
    required_operations: tuple[str, ...] = ()
    arguments_hint: str = ""

    def __post_init__(self) -> None:
        if not _COMMAND_NAME.fullmatch(self.name):
            raise ValueError(
                f"Invalid command name {self.name!r}: use letters, digits, '_', '.' or '-'."
            )
        if not callable(self.handler):
            raise ValueError(f"Command {self.name!r} needs an async handler.")
        if self.args_model is not None and not (
            isinstance(self.args_model, type) and issubclass(self.args_model, BaseModel)
        ):
            raise ValueError(f"Command {self.name!r} args_model must be a Pydantic model.")
        unknown = set(self.required_operations) - _CONVERSATION_OPERATIONS
        if unknown:
            raise ValueError(
                f"Command {self.name!r} requires unsupported operations: {', '.join(sorted(unknown))}."
            )

    @property
    def takes_arguments(self) -> bool:
        return self.args_model is not TextArguments or bool(self.arguments_hint)

    def parse_text(self, text: str) -> dict[str, Any]:
        """Turn slash-tail text into a mapping for ``args_model``."""
        if self.parser is None:
            return {"text": text}
        parsed = self.parser(text)
        if not isinstance(parsed, Mapping):
            raise ValueError(f"Command {self.name!r} parser did not return a mapping.")
        return dict(parsed)

    def parse(self, text: str) -> ArgsT:
        values = self.parse_text(text)
        try:
            return self.args_model.model_validate(values)
        except ValidationError as exc:
            raise HarnessError(
                "INVALID_ARGUMENTS",
                f"Invalid arguments for command {self.name!r}.",
                details={"errors": exc.errors()[:5]},
            ) from exc

    def parse_arguments(self, arguments: Mapping[str, Any]) -> ArgsT:
        try:
            return self.args_model.model_validate(dict(arguments))
        except ValidationError as exc:
            raise HarnessError(
                "INVALID_ARGUMENTS",
                f"Invalid arguments for command {self.name!r}.",
                details={"errors": exc.errors()[:5]},
            ) from exc


@dataclass
class CommandBinding:
    """A command paired with its live binding configuration."""

    binding_id: str
    plugin_id: str
    command: Command[Any]
    config: Any
    resources: ResourceSet | None
    configuration: ConfigurationSnapshot | None
    supported_operations: frozenset[str] = _CONVERSATION_OPERATIONS
    principal: str = ""
    deployment_id: str = ""

    @property
    def command_name(self) -> str:
        return self.command.name

    def descriptor(self, *, revision: int = 0) -> CommandDescriptor:
        return build_command_descriptor(
            self.binding_id,
            self.command,
            revision=revision,
        )

    def context(
        self,
        *,
        session_id: str,
        principal: str | None = None,
        deployment_id: str | None = None,
        history_reader: Callable[[], Any] | None = None,
    ) -> CommandContext:
        return CommandContext(
            binding_id=self.binding_id,
            plugin_id=self.plugin_id,
            session_id=session_id,
            principal=principal if principal is not None else self.principal,
            deployment_id=deployment_id if deployment_id is not None else self.deployment_id,
            config=self.config,
            resources=self.resources,
            configuration=self.configuration,
            supported_operations=self.supported_operations,
            _history_reader=history_reader,
        )


def build_command_descriptor(
    binding_id: str, command: Command[Any], *, revision: int = 0
) -> CommandDescriptor:
    return CommandDescriptor(
        binding_id=binding_id,
        command_name=command.name,
        description=command.description,
        takes_arguments=command.takes_arguments,
        arguments_hint=command.arguments_hint,
        required_operations=list(command.required_operations),
        revision=revision,
    )
