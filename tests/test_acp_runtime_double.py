"""Demo deployment used by the ACP host tests (and small manual trials).

This module is a *deployment*: it defines native plugins, an ``AgentDefinition`` built
from a real Pydantic AI factory, and a ``build_host_bundle(setup)`` entry point that
composes the production runtime (``SessionManager`` + SQLite store) for ``harness-host
--runtime-module test_acp_runtime_double``.

Turns run real Pydantic AI agents driven by ``FunctionModel`` / ``TestModel`` — no
provider credentials — so the official-SDK subprocess roundtrip exercises the actual
runtime session controller, policy, persistence, and event mapping.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from harness_protocol import CapabilityPolicy
from harness_runtime.definitions import (
    AgentDefinition,
    DefinitionBinding,
)
from harness_runtime.host import HostBundle, HostMetadataStore, HostSetup
from harness_runtime.registry import PluginDescriptor, PluginRegistry
from harness_runtime.resources import ResourceRequirement
from harness_runtime.session import SessionManager
from harness_runtime.store import SessionStore
from pydantic import BaseModel
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

MODEL_IDS: tuple[str, ...] = ("test",)
DEMO_AGENT_ID = "demo-agent"


def _ensure_resource_resolution_compat() -> None:
    """Temporary bridge for an in-flight runtime change.

    The runtime's working-directory retention path calls ``ResourceSet.try_resolve``;
    if the installed revision predates that method, emulate its two-value contract so
    the host tests exercise the real session controller. Inert once the runtime
    provides it.
    """
    from harness_runtime.resources import ResourceSet

    if hasattr(ResourceSet, "try_resolve"):
        return

    def try_resolve(self: Any, requirement: Any) -> tuple[bool, Any]:
        if requirement.name in self:
            return True, self[requirement.name]
        return False, None

    ResourceSet.try_resolve = try_resolve  # type: ignore[attr-defined]


_ensure_resource_resolution_compat()


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------


class TicketConfig(BaseModel):
    """Editable, non-secret configuration for the ticket search plugin."""

    endpoint: str = "https://tickets.example.invalid"
    limit: int = 3


@dataclass
class TicketCapability(AbstractCapability[Any]):
    endpoint: str = "https://tickets.example.invalid"
    limit: int = 3

    def get_toolset(self) -> FunctionToolset[Any]:
        endpoint, limit = self.endpoint, self.limit

        def ticket_search(query: str) -> str:
            """Search the demo ticket backend."""
            return f"{limit} tickets matched {query!r} at {endpoint}"

        return FunctionToolset([ticket_search])


@dataclass
class LockedCapability(AbstractCapability[Any]):
    def get_toolset(self) -> FunctionToolset[Any]:
        def audit_log(entry: str) -> str:
            """Record an audit entry."""
            return f"audited: {entry}"

        return FunctionToolset([audit_log])


@dataclass
class SessionToolCapability(AbstractCapability[Any]):
    def get_toolset(self) -> FunctionToolset[Any]:
        def session_helper() -> str:
            """A helper fixed for the session's creation configuration."""
            return "session helper"

        return FunctionToolset([session_helper])


@dataclass
class ExtraCapability(AbstractCapability[Any]):
    def get_toolset(self) -> FunctionToolset[Any]:
        def extra_lookup(key: str) -> str:
            """Look something up in the locally added plugin."""
            return f"extra:{key}"

        return FunctionToolset([extra_lookup])


def ticket_factory(config: TicketConfig, binding: Any) -> AbstractCapability[Any]:
    return TicketCapability(endpoint=config.endpoint, limit=config.limit)


def locked_factory(config: Any, binding: Any) -> AbstractCapability[Any]:
    return LockedCapability()


def session_factory(config: Any, binding: Any) -> AbstractCapability[Any]:
    return SessionToolCapability()


def extra_factory(config: Any, binding: Any) -> AbstractCapability[Any]:
    return ExtraCapability()


def demo_registry(*, allow_local_binding_creation: bool = True) -> PluginRegistry:
    """The installed plugin inventory for the demo deployment."""
    registry = PluginRegistry(allow_local_binding_creation=allow_local_binding_creation)
    registry.register(
        PluginDescriptor(
            plugin_id="demo.tickets",
            version="1.2.0",
            description="Search the demo ticket backend.",
            config_model=TicketConfig,
            factory=ticket_factory,
            change_mode="next_turn",
            state_mode="external",
            presentation=["tickets"],
            default_policy=CapabilityPolicy(
                available=True,
                enabled_by_default=False,
                client_can_enable=True,
                client_can_disable=True,
                editable_fields={"endpoint"},
                change_mode="next_turn",
            ),
        )
    )
    registry.register(
        PluginDescriptor(
            plugin_id="demo.locked",
            version="1.0.0",
            description="Mandatory deployment audit.",
            factory=locked_factory,
            change_mode="next_turn",
            default_policy=CapabilityPolicy(
                available=True,
                enabled_by_default=True,
                client_can_enable=False,
                client_can_disable=False,
                change_mode="next_turn",
            ),
        )
    )
    registry.register(
        PluginDescriptor(
            plugin_id="demo.session_tool",
            version="0.9.0",
            description="A helper fixed at session creation.",
            factory=session_factory,
            change_mode="new_session",
            default_policy=CapabilityPolicy(
                available=True,
                enabled_by_default=False,
                client_can_enable=True,
                client_can_disable=True,
                change_mode="new_session",
            ),
        )
    )
    registry.register(
        PluginDescriptor(
            plugin_id="demo.extra",
            version="0.1.0",
            description="A locally addable extra lookup.",
            factory=extra_factory,
            change_mode="next_turn",
            default_policy=CapabilityPolicy(
                available=True,
                enabled_by_default=False,
                client_can_enable=True,
                client_can_disable=True,
                change_mode="next_turn",
            ),
        )
    )
    return registry


# ---------------------------------------------------------------------------
# Deterministic model behavior
# ---------------------------------------------------------------------------


def _last_user_index(messages: Sequence[Any]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, ModelRequest) and any(
            isinstance(part, UserPromptPart) for part in message.parts
        ):
            return index
    return -1


def _last_user_text(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    content = part.content
                    if isinstance(content, str):
                        return content
                    return " ".join(str(item) for item in content)
    return ""


def _tool_return_after_last_user(messages: Sequence[Any]) -> ToolReturnPart | None:
    start = _last_user_index(messages)
    for message in messages[start + 1 :]:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart):
                return part
    return None


def _demo_stream_function(messages: Sequence[Any], info: Any) -> AsyncIterator[Any]:
    """Deterministic model behavior keyed by the current user prompt."""

    async def generate() -> AsyncIterator[Any]:
        text = _last_user_text(messages)
        tool_return = _tool_return_after_last_user(messages)
        if tool_return is not None:
            output = tool_return.content
            reply = f"tool result: {output}"
            if type(output).__name__ == "ToolDenied":
                reply = "tool denied by the client"
            yield reply[:5]
            yield reply[5:]
            return
        lowered = text.lower()
        if "read files" in lowered:
            yield {
                0: DeltaToolCall(
                    name="read_file",
                    json_args=json.dumps({"path": "notes.md"}),
                    tool_call_id="call-read",
                )
            }
            return
        if "record" in lowered:
            yield {
                0: DeltaToolCall(
                    name="c_test_note",
                    json_args=json.dumps({"topic": text.strip()[:60] or "demo"}),
                    tool_call_id="call-note",
                )
            }
            return
        if "wait" in lowered:
            yield {
                0: DeltaToolCall(
                    name="c_test_wait",
                    json_args=json.dumps({"seconds": 30}),
                    tool_call_id="call-wait",
                )
            }
            return
        reply = f"echo: {text}"
        yield reply[:4]
        yield reply[4:]

    return generate()


def model_for(model_id: str | None) -> Any:
    if model_id == "echo":
        return TestModel(custom_output_text="fixed echo model reply")
    return FunctionModel(stream_function=_demo_stream_function)


def _demo_factory(
    *,
    resources: Any = None,
    model: Any = None,
    active_config: Any = None,
    context: Any = None,
    managed_capabilities: Sequence[Any] | None = None,
) -> Agent[Any, str]:
    """Definition factory: fixed behavior plus this turn's managed capabilities."""
    resolved_model = model if not isinstance(model, str) else model_for(model)
    agent: Agent[Any, str] = Agent(
        resolved_model if resolved_model is not None else model_for("function"),
        instructions="You are the Harness demo test agent.",
        capabilities=list(managed_capabilities or []),
        # Deferred approvals are part of this agent's supported output surface; the
        # runtime resumes the turn with DeferredToolResults once the ACP client decides.
        output_type=[str, DeferredToolRequests],
    )

    @agent.tool_plain
    def read_file(path: str) -> str:
        """Read a file from the workspace."""
        return f"contents of {path}"

    @agent.tool_plain(requires_approval=True)
    def c_test_note(topic: str) -> str:
        """Record a note (requires client approval)."""
        return f"note({topic})"

    @agent.tool_plain
    async def c_test_wait(seconds: float = 30) -> str:
        """Sleep so tests can cancel a turn in flight."""
        await asyncio.sleep(seconds)
        return "waited"

    return agent


def build_demo_definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id=DEMO_AGENT_ID,
        name="Demo Agent",
        description="Deterministic agent used by the ACP host tests.",
        definition_version="1.0.0",
        factory=_demo_factory,
        model="function",
        resources=[
            ResourceRequirement(name="workspace", kind="workspace", required=False),
            ResourceRequirement(name="model", kind="model", required=False),
        ],
        bindings=[
            DefinitionBinding(
                binding_id="tickets",
                plugin_id="demo.tickets",
                config={"endpoint": "https://tickets.example.invalid", "limit": 2},
                enabled=False,
            ),
            DefinitionBinding(binding_id="locked", plugin_id="demo.locked", enabled=True),
            DefinitionBinding(binding_id="session_tool", plugin_id="demo.session_tool", enabled=False),
        ],
        interaction_features=["streaming", "permissions", "configuration"],
    )


DEMO_DEFINITION = build_demo_definition()
demo_definition = DEMO_DEFINITION


# ---------------------------------------------------------------------------
# Bundle builder (the ``--runtime-module`` entry point)
# ---------------------------------------------------------------------------


def build_host_bundle(setup: HostSetup) -> HostBundle:
    """Compose the real runtime for the demo deployment."""
    definition = DEMO_DEFINITION
    allow_local = setup.advertise_local_binding_creation
    registry = demo_registry(allow_local_binding_creation=allow_local)
    store = SessionStore(setup.store_path if setup.store_path else ":memory:")
    manager = SessionManager(
        registry,
        store=store,
        definitions=[definition],
        allow_local_binding_creation=allow_local,
    )
    store_path = None if str(store.path) == ":memory:" else str(store.path)
    return HostBundle(
        definition=definition,
        registry=registry,
        manager=manager,
        store_path=store_path,
        meta=HostMetadataStore(store_path),
    )
