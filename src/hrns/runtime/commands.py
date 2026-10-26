"""Application command bindings collected from the enabled native composition.

Commands belong to a configured binding, not to a package publisher or a run-local native
capability id: ``/<binding_id>:<command_name>``. A binding may be added twice with different
configuration, and each instance owns its own namespace.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from hrns.protocol import (
    SUPPORTED_CONVERSATION_OPERATIONS,
    CommandDescriptor,
    ConfigurationSnapshot,
    HrnsError,
)
from hrns.protocol.extensions import CommandInvokeRequest, CommandResult

from .extensions import Command, CommandBinding
from .registry import BindingContext, PluginRegistry
from .resources import ResourceSet

__all__ = ["CommandRegistry", "InvocationRecord", "build_command_registry"]


@dataclass
class InvocationRecord:
    """Bounded, per-session result memory for idempotent request ids."""

    request_hash: str
    response: dict[str, Any]


@dataclass
class CommandRegistry:
    """Immutable command table for one active composition revision."""

    bindings: dict[tuple[str, str], CommandBinding] = field(default_factory=dict)
    revision: int = 0
    supported_operations: frozenset[str] = frozenset(SUPPORTED_CONVERSATION_OPERATIONS)

    def descriptors(self) -> list[CommandDescriptor]:
        return [
            binding.descriptor(revision=self.revision)
            for binding in sorted(
                self.bindings.values(), key=lambda item: (item.binding_id, item.command_name)
            )
        ]

    def resolve(self, binding_id: str, command_name: str) -> CommandBinding:
        binding = self.bindings.get((binding_id, command_name))
        if binding is None:
            raise HrnsError(
                "COMMAND_UNAVAILABLE",
                f"Command '/{binding_id}:{command_name}' is not available in this session revision.",
                details={"binding_id": binding_id, "command_name": command_name},
            )
        return binding

    def parse_arguments(self, binding: CommandBinding, request: CommandInvokeRequest) -> Any:
        command = binding.command
        if request.arguments is not None:
            return command.parse_arguments(request.arguments)
        return command.parse(request.text or "")

    async def invoke(
        self,
        binding: CommandBinding,
        request: CommandInvokeRequest,
        *,
        history_reader: Callable[[], Any] | None = None,
    ) -> CommandResult:
        """Validate arguments and run one handler; side effects happen only after this."""
        unsupported = set(binding.command.required_operations) - self.supported_operations
        if unsupported:
            raise HrnsError(
                "UNSUPPORTED_OPERATION",
                f"Command '/{binding.binding_id}:{binding.command_name}' needs unsupported "
                f"operations: {', '.join(sorted(unsupported))}.",
                details={
                    "binding_id": binding.binding_id,
                    "command_name": binding.command_name,
                    "operations": sorted(unsupported),
                },
            )
        arguments = self.parse_arguments(binding, request)
        context = binding.context(
            session_id=request.session_id,
            history_reader=history_reader,
        )
        try:
            result = await binding.command.handler(context, arguments)
        except HrnsError:
            raise
        except Exception as exc:
            raise HrnsError(
                "COMMAND_FAILED",
                f"Command '/{binding.binding_id}:{binding.command_name}' failed: "
                f"{type(exc).__name__}: {exc}",
                details={"binding_id": binding.binding_id},
            ) from exc
        if not isinstance(result, CommandResult):
            raise HrnsError(
                "COMMAND_FAILED",
                f"Command '/{binding.binding_id}:{binding.command_name}' returned "
                f"{type(result).__name__}, not CommandResult.",
                details={"binding_id": binding.binding_id},
            )
        return result


def build_command_registry(
    *,
    registry: PluginRegistry,
    enabled: Sequence[Any],
    resources: ResourceSet,
    snapshot: ConfigurationSnapshot | None,
    session_id: str,
    principal: str,
    deployment_id: str,
    revision: int,
) -> CommandRegistry:
    """Collect commands from enabled offerings without re-running a native lifecycle."""
    supported = frozenset(SUPPORTED_CONVERSATION_OPERATIONS)
    bindings: dict[tuple[str, str], CommandBinding] = {}
    for offering in enabled:
        commands: Sequence[Command[Any]] = tuple(getattr(offering.descriptor, "commands", ()) or ())
        if not commands:
            continue
        context = BindingContext(
            binding_id=offering.binding_id,
            plugin_id=offering.plugin_id,
            session_id=session_id,
            principal=principal,
            deployment_id=deployment_id,
            binding_config=dict(offering.config),
            resources=resources,
            active_configuration=snapshot,
        )
        config = registry.validated_config(offering.descriptor, offering.config, context)
        for command in commands:
            if not isinstance(command, Command):
                raise HrnsError(
                    "INVALID_CONFIGURATION",
                    f"Plugin {offering.plugin_id!r} contributed an invalid command object.",
                    details={"binding_id": offering.binding_id},
                )
            key = (offering.binding_id, command.name)
            if key in bindings:
                raise HrnsError(
                    "INVALID_CONFIGURATION",
                    f"Binding {offering.binding_id!r} contributes duplicate command "
                    f"{command.name!r}.",
                    details={"binding_id": offering.binding_id, "command_name": command.name},
                )
            bindings[key] = CommandBinding(
                binding_id=offering.binding_id,
                plugin_id=offering.plugin_id,
                command=command,
                config=config,
                resources=resources,
                configuration=snapshot,
                supported_operations=supported,
                principal=principal,
                deployment_id=deployment_id,
            )
    return CommandRegistry(bindings=bindings, revision=revision, supported_operations=supported)
