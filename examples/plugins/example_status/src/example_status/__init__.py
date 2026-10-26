"""Example plugin: a native tool plus two independent user commands.

This module is the ``hrns.capabilities`` descriptor provider. It must not import
Textual; local widgets live in :mod:`example_status.tui`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolReturn
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from hrns.protocol import PresentationPayload
from hrns.protocol.extensions import CommandResult, StartTurn
from hrns.runtime.extensions import Command, CommandContext
from hrns.runtime.registry import BindingContext, PluginDescriptor

__all__ = ["descriptor"]


class ExampleConfig(BaseModel):
    """Configurable label shown in command output."""

    label: str = "example"


@dataclass(kw_only=True)
class EchoCapability(AbstractCapability[Any]):
    """Native agent tool with independent model-facing and UI-only data."""

    binding_id: str
    id: str | None = "example.echo"

    def get_toolset(self) -> Any:
        binding_id = self.binding_id

        async def echo(ctx: RunContext[Any], text: str) -> Any:
            del ctx
            return ToolReturn(
                f"echo: {text}",
                metadata={
                    "hrns": {
                        "presentation": {
                            "output": PresentationPayload(
                                renderer_id="example.echo",
                                schema_version=1,
                                data={"text": text, "uiOnly": "not sent to the model"},
                                fallback=f"echo: {text}",
                            ).to_wire()
                        }
                    }
                },
            )

        return FunctionToolset(
            tools=[Tool(echo, name="example_echo", takes_ctx=True)],
            metadata={
                "hrns": {
                    "presentation": {
                        "bindingId": binding_id,
                        "input": PresentationPayload(
                            renderer_id="example.echo",
                            schema_version=1,
                            data={"region": "input"},
                            fallback="echo input",
                        ).to_wire(),
                    }
                }
            },
        )


def factory(config: ExampleConfig, context: BindingContext) -> EchoCapability:
    del config
    return EchoCapability(binding_id=context.binding_id)


async def status(context: CommandContext, arguments: Any) -> CommandResult:
    text = getattr(arguments, "text", "")
    config = context.config
    label = getattr(config, "label", "example")
    return CommandResult(
        presentation=PresentationPayload(
            renderer_id="example.git.status",
            schema_version=1,
            data={"label": label, "text": text},
            fallback=f"{label} status: {text}".strip(),
        )
    )


async def ask(context: CommandContext, arguments: Any) -> CommandResult:
    del context
    text = getattr(arguments, "text", "")
    return CommandResult(
        conversation=StartTurn(prompt=f"Review the current changes: {text}".strip())
    )


def descriptor() -> PluginDescriptor:
    """Zero-argument provider loaded lazily by ``hrns.capabilities`` discovery."""
    return PluginDescriptor(
        plugin_id="example_status",
        version="0.1.0",
        description="Example status and ask commands with an independent echo tool view.",
        config_model=ExampleConfig,
        factory=factory,
        commands=[
            Command(name="status", handler=status, description="Show the current status"),
            Command(name="ask", handler=ask, description="Start a normal agent turn"),
        ],
        presentation=["example.git.status", "example.echo"],
    )
