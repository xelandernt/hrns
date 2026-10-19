"""Deterministic, credential-free example definitions for the harness.

``demo_definition`` is the canonical end-to-end demonstration used by documentation, the
``harness-host`` command, and the test suite:

    harness-host harness_runtime.examples:demo_definition

It is a normal :class:`~harness_runtime.definitions.AgentDefinition` whose Python factory
returns a *fresh* native Pydantic AI agent backed by a :class:`~pydantic_ai.models.function.FunctionModel`.
The managed ``tone`` binding contributes native instructions (``Capability(instructions=...)``);
enabling or disabling that binding between turns changes what the model actually receives.
The scripted model echoes the active tone marker, so toggling is visible in the output without
any provider request:

    tone on  -> "offline demo: tone=concise"
    tone off -> "offline demo: tone=none"
    tone on  -> "offline demo: tone=concise"

Nothing here imports ``harness_runtime.session`` at module import; the session controller is
loaded only by :func:`run_demo`, which keeps the definition importable by the host worker
without side effects.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from harness_protocol import CapabilityPolicy, EnableChange, HarnessError
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from .definitions import AgentDefinition, DefinitionBinding
from .registry import BindingContext, PluginDescriptor, PluginRegistry

__all__ = [
    "DEMO_AGENT_ID",
    "DEMO_BINDING_ID",
    "DEMO_PLUGIN_ID",
    "ToneConfig",
    "build_demo_agent",
    "demo_definition",
    "demo_definition_factory",
    "demo_plugin_descriptor",
    "demo_registry",
    "run_demo",
    "run_demo_sync",
]

DEMO_AGENT_ID = "harness.demo"
DEMO_PLUGIN_ID = "example.tone"
DEMO_BINDING_ID = "tone"
DEMO_VERSION = "1.0.0"

_BASE_INSTRUCTIONS = (
    "You are the offline harness demo agent. Answer briefly. "
    "The active tone capability, when enabled, adds a [[tone=...]] instruction marker."
)

_TONE_INSTRUCTIONS = {
    "concise": "[[tone=concise]] Prefer one short paragraph. Do not repeat the question.",
    "enthusiastic": "[[tone=enthusiastic]] Be upbeat and celebratory, but stay accurate.",
}

_TONE_MARKERS = {
    "concise": "[[tone=concise]]",
    "enthusiastic": "[[tone=enthusiastic]]",
}


class ToneConfig(BaseModel):
    """Validated, serializable configuration for the demo tone capability."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    style: str = Field(default="concise", pattern="^(concise|enthusiastic)$")


def _tone_factory(config: ToneConfig, _binding: BindingContext) -> Capability:
    style = config.style if isinstance(config, ToneConfig) else str(config["style"])
    try:
        instructions = _TONE_INSTRUCTIONS[style]
    except KeyError:  # pragma: no cover - the config model rejects unknown styles
        raise HarnessError(
            "INVALID_CONFIGURATION",
            f"Unknown demo tone style {style!r}; expected concise or enthusiastic.",
            details={"style": style},
        ) from None
    return Capability(instructions=instructions, id=DEMO_PLUGIN_ID)


def demo_plugin_descriptor() -> PluginDescriptor:
    """The example plugin used by the demo definition (native instructions capability)."""
    return PluginDescriptor(
        plugin_id=DEMO_PLUGIN_ID,
        version=DEMO_VERSION,
        description="Demo-only native instructions capability with a switchable tone.",
        config_model=ToneConfig,
        factory=_tone_factory,
        change_mode="next_turn",
        state_mode="run",
        presentation=["demo.tone"],
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_enable=True,
            client_can_disable=True,
            editable_fields={"style"},
            change_mode="next_turn",
        ),
        compatibility={
            "runtime_compatible": True,
            "constraint": "Bundled example; not a general-purpose production plugin.",
        },
    )


def _demo_model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Deterministic scripted model: report which tone marker reached the request."""
    return ModelResponse(parts=[TextPart(_demo_text(info))])


def _demo_text(info: AgentInfo) -> str:
    instructions = info.instructions or ""
    active = next(
        (style for style, marker in _TONE_MARKERS.items() if marker in instructions),
        "none",
    )
    return f"offline demo: tone={active}"


async def _demo_stream(_messages: list[ModelMessage], info: AgentInfo):
    """Streaming variant used by the session controller (which streams every turn)."""
    yield _demo_text(info)


def build_demo_agent(
    *,
    resources: Any = None,
    active_config: Any = None,
    managed_capabilities: tuple[Any, ...] = (),
) -> Agent[None, str]:
    """Definition factory: return a fresh native agent for one turn.

    Declares ``managed_capabilities`` so the runtime binds enabled bindings at agent
    construction (native instructions are visible to the scripted model).
    """
    del resources, active_config  # the offline demo needs no external host resources
    return Agent(
        FunctionModel(
            _demo_model,
            stream_function=_demo_stream,
            model_name="harness-demo-function",
        ),
        name="harness-demo",
        instructions=_BASE_INSTRUCTIONS,
        capabilities=[*managed_capabilities],
    )


def demo_definition() -> AgentDefinition:
    """The canonical deterministic demo definition (no credentials, no network, no workspace)."""
    return AgentDefinition(
        agent_id=DEMO_AGENT_ID,
        name="Offline harness demo",
        description=(
            "Deterministic native Pydantic AI FunctionModel with a switchable tone capability; "
            "demonstrates that disabling a binding omits its instructions on the next turn."
        ),
        definition_version=DEMO_VERSION,
        factory=build_demo_agent,
        bindings=[
            DefinitionBinding(
                binding_id=DEMO_BINDING_ID,
                plugin_id=DEMO_PLUGIN_ID,
                config={"style": "concise"},
                enabled=True,
                policy=CapabilityPolicy(
                    available=True,
                    enabled_by_default=True,
                    client_can_enable=True,
                    client_can_disable=True,
                    editable_fields={"style"},
                    change_mode="next_turn",
                ),
            )
        ],
        resources=[],
        interaction_features=[],
    )


def demo_definition_factory() -> AgentDefinition:
    """Callable form for launchers that expect an import reference to a factory."""
    return demo_definition()


def demo_registry(*, include_catalog: bool = True) -> PluginRegistry:
    """Registry with the demo plugin (and optionally the whole bundled catalog)."""
    if include_catalog:
        from .catalog import build_catalog_registry  # local import: catalog builds examples too

        return build_catalog_registry()
    registry = PluginRegistry()
    registry.register(demo_plugin_descriptor())
    return registry


def _tone_of(output: str) -> str:
    marker = "tone="
    if marker not in output:
        return "unknown"
    return output.split(marker, 1)[1].strip().split()[0].rstrip(";.,")


async def run_demo(
    *,
    store: str | Path | None = None,
    include_catalog: bool = True,
    principal: str = "demo",
) -> dict[str, Any]:
    """Run the on -> off -> on toggle demonstration through the real session controller.

    The definition, policy, revision snapshots and per-turn composition are the same ones a
    client or the ACP host uses; only the model is scripted.
    """
    from .session import SessionManager  # local import: keep demo_definition host-safe

    definition = demo_definition()
    manager = SessionManager(
        demo_registry(include_catalog=include_catalog),
        store=store,
        definitions=[definition],
        allow_local_binding_creation=False,
    )
    turns: list[dict[str, Any]] = []
    async with manager.open_session(definition, principal=principal, deployment_id="local") as session:
        first = await session.run("Say hello.")
        turns.append(
            {
                "prompt": "Say hello.",
                "output": str(first.output),
                "tone": _tone_of(str(first.output)),
                "selected_revision": session.snapshot.selected_revision,
            }
        )

        disabled = await session.update_configuration(
            [EnableChange(binding_id=DEMO_BINDING_ID, enabled=False)],
            request_id="demo-disable-tone",
            expected_revision=session.snapshot.selected_revision,
        )
        second = await session.run("Say hello again.")
        turns.append(
            {
                "prompt": "Say hello again.",
                "output": str(second.output),
                "tone": _tone_of(str(second.output)),
                "selected_revision": disabled.selected_revision,
            }
        )

        enabled = await session.update_configuration(
            [EnableChange(binding_id=DEMO_BINDING_ID, enabled=True)],
            request_id="demo-enable-tone",
            expected_revision=disabled.selected_revision,
        )
        third = await session.run("Say hello one more time.")
        turns.append(
            {
                "prompt": "Say hello one more time.",
                "output": str(third.output),
                "tone": _tone_of(str(third.output)),
                "selected_revision": enabled.selected_revision,
            }
        )
        final_snapshot = session.snapshot

    return {
        "agent_id": definition.agent_id,
        "definition_version": definition.definition_version,
        "turns": turns,
        "selected_revision": final_snapshot.selected_revision,
        "active_revision": final_snapshot.active_revision,
        "toggled": [turn["tone"] for turn in turns] == ["concise", "none", "concise"],
        "transport": "python-session",
    }


def run_demo_sync(**kwargs: Any) -> dict[str, Any]:
    """Synchronous wrapper for scripts and the ``python -m`` entry point."""
    return asyncio.run(run_demo(**kwargs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic offline harness demo (no credentials or network)."
    )
    parser.add_argument(
        "--store",
        default=None,
        help="SQLite store path (defaults to the runtime host store).",
    )
    parser.add_argument(
        "--no-catalog",
        action="store_true",
        help="Register only the demo plugin instead of the full bundled catalog.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)
    try:
        result = run_demo_sync(store=args.store, include_catalog=not args.no_catalog)
    except HarnessError as exc:
        print(f"harness demo: {exc.message}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for index, turn in enumerate(result["turns"], start=1):
            print(f"turn {index}: {turn['output']}")
        print(f"toggled correctly: {result['toggled']}")
    return 0 if result["toggled"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised manually
    raise SystemExit(main())
