"""Shared fixtures for catalog, starter and example tests.

The catalog is deliberately lazy: importing it must not import optional integrations.  These
helpers build a :class:`~harness_runtime.registry.BindingContext` and a resource set with
offline stand-ins (test model, in-memory stores, a temporary workspace) so every *verified*
factory can actually be exercised without provider credentials.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from harness_runtime import definitions
from harness_runtime.registry import BindingContext
from harness_runtime.resources import ResourceSet

# ``BindingContext`` uses a forward reference to ``AgentFactoryContext`` that the runtime
# module keeps under TYPE_CHECKING; rebuild it here so tests can construct real contexts.
BindingContext.model_rebuild(
    _types_namespace={"AgentFactoryContext": definitions.AgentFactoryContext}
)


#: Valid, minimal, serializable configuration for entries whose schema has required fields.
#: Every value is real: these validate ``extra="forbid"`` models rather than bypassing them.
CATALOG_CONFIGS: dict[str, dict[str, Any]] = {
    "pydantic.mcp": {"url": "https://example.org/mcp"},
    "pydantic.ssh_workspace": {"destination": "user@example.org"},
    "harness.google_workspace": {"services": ["gmail"]},
    "harness.stackone": {"account_id": "account-1"},
    "harness.tool_call_judge": {"question": "Would this call destroy data?"},
    "harness.approval_reviewer": {"question": "Would this call destroy data?"},
    "harness.clear_tool_results": {"max_tokens": 5_000},
    "harness.sliding_window": {"max_messages": 100},
    "harness.summarizing_compaction": {"max_messages": 100},
    "harness.tiered_compaction": {"target_fraction": 0.8},
    "harness.clamp_oversized": {"max_part_chars": 5_000},
    "harness.warn_near_limits": {"max_iterations": 40},
    "harness.input_guardrail": {"detectors": [{"kind": "redact_secrets"}]},
    "harness.output_guardrail": {"detectors": [{"kind": "redact_personal_data"}]},
    "harness.system_reminders": {"goal_reanchor": True},
    "harness.skills": {"missing_directories": "skip"},
    "pydantic.prefix_tools": {"prefix": "vendor"},
    "pydantic.mcp_native": {"url": "https://example.org/mcp"},
    "harness.managed_prompt": {"name": "support", "default": "You are support."},
}


def example_config(plugin_id: str) -> dict[str, Any]:
    """Valid configuration for one entry (empty when the schema has no required fields)."""
    return dict(CATALOG_CONFIGS.get(plugin_id, {}))


async def answerer(_request: Any) -> Any:  # pragma: no cover - never invoked during build
    raise RuntimeError("tests do not answer user questions")


async def deferred_handler(_ctx: Any, _requests: Any) -> None:  # pragma: no cover
    return None


def _file_key(_part: Any) -> None:  # pragma: no cover - part shape unused
    return None


def offline_resource_set(workspace_dir: str | Path) -> ResourceSet:
    """Resources every verified entry can resolve offline (in-memory stores, test model)."""
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import Capability, Hooks, LocalWorkspace
    from pydantic_ai.workspaces import LocalWorkspaceBackend
    from pydantic_ai_harness.compaction import ClampOversizedMessages, SlidingWindowCompaction
    from pydantic_ai_harness.conversation_search import SnapshotHistorySource
    from pydantic_ai_harness.memory import InMemoryStore
    from pydantic_ai_harness.planning import InMemoryPlanStore
    from pydantic_ai_harness.spend import InMemorySpendStore
    from pydantic_ai_harness.step_persistence import InMemoryStepStore
    from pydantic_ai_harness.subagents import SubAgent
    from pydantic_ai_harness.tool_output_limits import LocalFileStore

    step_store = InMemoryStepStore()
    return ResourceSet(
        {
            "workspace": LocalWorkspaceBackend(str(workspace_dir)),
            "memory.store": InMemoryStore(),
            "plan.store": InMemoryPlanStore(),
            "spend.store": InMemorySpendStore(),
            "step.store": step_store,
            "conversations": SnapshotHistorySource(step_store),
            "tool_overflow.store": LocalFileStore(base_dir=str(workspace_dir)),
            "model.judge": "test",
            "model.advisor": "test",
            "user_answers": answerer,
            "deferred.handler": deferred_handler,
            "subagents.agents": [
                SubAgent(Agent("test", name="helper", description="Offline helper agent."))
            ],
            "dynamic.agents": [Agent("test", name="helper")],
            "render.app": object(),
            "wrapped.capability": Capability(instructions="Wrapped offline capability."),
            "wrapped.workspace": LocalWorkspace(str(workspace_dir)),
            "compaction.tiers": [ClampOversizedMessages(max_part_chars=1_000)],
            "compaction.fallback_chain": [
                SlidingWindowCompaction(max_messages=10, keep_messages=5)
            ],
            "callbacks.file_key": _file_key,
            "callbacks.hooks": Hooks(),
            "hooks": Hooks(),
            "callbacks.model_selector": lambda _ctx: "test",
            "callbacks.model_id_resolver": lambda _ctx, model_id: model_id,
            "callbacks.prepare_tools": lambda _ctx, tools: tools,
            "callbacks.history": lambda messages: messages,
            "callbacks.event_stream": lambda _ctx, _events: None,
            "callbacks.dynamic_reminders": [],
            "callbacks.tool_guardrail": lambda _call: True,
            "thread.executor": ThreadPoolExecutor(max_workers=1),
            "media.s3_credentials": {"access_key_id": "test", "secret_access_key": "test"},
        }
    )


def make_binding(
    plugin_id: str,
    resources: ResourceSet | None = None,
    *,
    binding_id: str = "test-binding",
) -> BindingContext:
    return BindingContext(
        binding_id=binding_id,
        plugin_id=plugin_id,
        session_id="test-session",
        principal="test",
        deployment_id="test-deployment",
        binding_config={},
        resources=resources,
    )


@pytest.fixture()
def catalog_resources() -> Iterator[ResourceSet]:
    with tempfile.TemporaryDirectory() as tmp:
        yield offline_resource_set(tmp)


@pytest.fixture()
def binding_for(catalog_resources: ResourceSet) -> Callable[..., BindingContext]:
    def factory(plugin_id: str, *, resources: ResourceSet | None = None) -> BindingContext:
        return make_binding(plugin_id, resources if resources is not None else catalog_resources)

    return factory
