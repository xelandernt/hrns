#!/usr/bin/env python3
"""Define a custom native capability in Python and run it through the real runtime.

The capability below is a normal Pydantic AI ``AbstractCapability``. It contributes native
instructions (not a tool-only wrapper) and is registered as a managed plugin with a real
config schema, so the same behavior is usable through the Python session API, ACP, and
editable manifests. The script is deterministic and offline; run it with:

    python examples/custom_capability.py

Expected output ends with::

    turn 1 (style on):  custom example: style=punchy
    turn 2 (style off): custom example: style=none
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.capabilities import AbstractCapability  # noqa: E402
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from harness.protocol import CapabilityPolicy, EnableChange  # noqa: E402
from harness.runtime.definitions import AgentDefinition, DefinitionBinding  # noqa: E402
from harness.runtime.registry import (  # noqa: E402
    BindingContext,
    PluginDescriptor,
    PluginRegistry,
)
from harness.runtime.session import SessionManager  # noqa: E402

PLUGIN_ID = "example.house_style"
BINDING_ID = "style"
MARKER = "[[custom-style:punchy]]"


# --- 1. The native behavior -------------------------------------------------


class HouseStyle(AbstractCapability[None]):
    """A custom capability that contributes instructions to every request.

    A custom capability may equally contribute toolsets, model settings, hooks, or
    lifecycle events; this example keeps the contribution to instructions so the effect is
    visible in the scripted model's input.
    """

    id = PLUGIN_ID
    description = "Adds a house writing style to the agent instructions."

    def __init__(self, *, style: str, marker: str = MARKER) -> None:
        self.style = style
        self.marker = marker

    def get_instructions(self) -> str:
        return f"{self.marker} Write in a {self.style} house style."


# --- 2. Its validated, serializable configuration ---------------------------


class HouseStyleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: str = Field(default="punchy", pattern="^(punchy|formal)$")


def build_house_style(config: HouseStyleConfig, _binding: BindingContext) -> HouseStyle:
    return HouseStyle(style=config.style)


# --- 3. Its management descriptor -------------------------------------------


def plugin_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        description="Example custom capability with editable style configuration.",
        config_model=HouseStyleConfig,
        factory=build_house_style,
        change_mode="next_turn",
        state_mode="run",
        presentation=["example.house_style"],
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields={"style"},
            change_mode="next_turn",
        ),
    )


# --- 4. The deterministic model and definition factory ----------------------


def scripted_model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    style = "punchy" if MARKER in (info.instructions or "") else "none"
    return ModelResponse(parts=[TextPart(f"custom example: style={style}")])


async def streamed_model(_messages: list[ModelMessage], info: AgentInfo):
    style = "punchy" if MARKER in (info.instructions or "") else "none"
    yield f"custom example: style={style}"


def build_agent(*, managed_capabilities: tuple[object, ...] = ()) -> Agent[None, str]:
    # Fresh agent per turn; managed bindings are bound at construction so the custom
    # capability's instructions reach the model request.
    return Agent(
        FunctionModel(scripted_model, stream_function=streamed_model, model_name="custom-example"),
        name="custom-capability-example",
        capabilities=[*managed_capabilities],
    )


# --- 5. Run the same policy path a client or the ACP host uses ---------------


async def main() -> None:
    registry = PluginRegistry()
    registry.register(plugin_descriptor())
    definition = AgentDefinition(
        agent_id="example.custom_capability",
        name="Custom capability example",
        definition_version="1.0.0",
        factory=build_agent,
        bindings=[
            DefinitionBinding(
                binding_id=BINDING_ID,
                plugin_id=PLUGIN_ID,
                config={"style": "punchy"},
                enabled=True,
            )
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        manager = SessionManager(
            registry,
            store=f"{tmp}/example.db",
            definitions=[definition],
        )
        async with manager.open_session(definition) as session:
            first = await session.run("Write a short announcement.")
            print(f"turn 1 (style on):  {first.output}")
            await session.update_configuration(
                [EnableChange(binding_id=BINDING_ID, enabled=False)],
                request_id="example-disable-style",
                expected_revision=session.snapshot.selected_revision,
            )
            second = await session.run("Write a short announcement.")
            print(f"turn 2 (style off): {second.output}")


if __name__ == "__main__":
    asyncio.run(main())
