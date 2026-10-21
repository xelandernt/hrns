"""Bundled lazy catalog of the reviewed Pydantic AI / Harness capability inventory.

This module is the runtime's *catalog data*: one :class:`CatalogEntry` per reviewed upstream
feature, each with a stable ``plugin_id``, the real upstream import target, an honest coverage
tier, a validated (serializable) configuration model, and a lazy factory that constructs the
native capability only when a binding composes.

The three coverage levels from the concept are independent and are recorded independently:

* **Runtime compatibility** -- the upstream class exists and is usable as a native capability
  in Python.  ``CatalogTier.VERIFIED`` entries were imported *and constructed* in the pinned
  development environment (no optional extras); ``INSTALL_REQUIRED`` entries need a documented
  optional extra; ``UNAVAILABLE`` entries are not shipped by the pinned upstream releases at
  all (they are documented here so the gap is explicit rather than silently absent).
* **Managed configuration** -- this module supplies a Pydantic ``config_model`` (JSON Schema,
  ``extra="forbid"``) and a descriptor factory.  A managed entry still does not promise that
  every provider/platform combination works; those constraints are recorded per entry.
* **Presentation** -- generic activity rendering always applies; an optional specialised
  renderer id may be recorded.

Rules this catalog follows:

* Importing the module never imports ``pydantic_ai_harness`` or any optional dependency.
  Factories import their upstream module on first composition only.
* A missing optional dependency raises ``CAPABILITY_UNAVAILABLE`` with the exact install
  command; it never falls back to a different implementation.
* Non-serializable dependencies (models, clients, stores, callables, workspaces) are named
  *resources* resolved from the deployment's :class:`~harness.runtime.resources.ResourceSet`.
  A factory without its required resource fails explicitly; it never invents host state.
* ``plugin_id`` values are stable; changing one is a deployment-visible compatibility event.

Use :func:`register_catalog` to install the whole catalog into a
:class:`~harness.runtime.registry.PluginRegistry`, or :func:`entry` to inspect one entry.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.protocol import CapabilityPolicy, HarnessError, PluginInfo

from .registry import (
    BindingContext,
    CompatibilityConstraints,
    PluginDescriptor,
    PluginRegistry,
)

if TYPE_CHECKING:
    from .resources import ResourceKind

__all__ = [
    "CATALOG",
    "CatalogEntry",
    "CatalogGroup",
    "CatalogKind",
    "CatalogTier",
    "build_catalog_registry",
    "entry",
    "iter_catalog",
    "iter_plugins",
    "media_store_entries",
    "plugin_infos",
    "register_catalog",
    "resource_requirements",
    "unavailable_entries",
]


# ---------------------------------------------------------------------------
# Coverage tiers and groups
# ---------------------------------------------------------------------------


class CatalogTier(StrEnum):
    """Honest coverage level of one catalog entry in the pinned environment."""

    VERIFIED = "verified"
    """Imported and constructed offline in the pinned development environment."""

    INSTALL_REQUIRED = "install_required"
    """The upstream import target exists but needs a documented optional extra."""

    UNAVAILABLE = "unavailable"
    """Absent from the pinned upstream releases; recorded so the gap is explicit."""


class CatalogGroup(StrEnum):
    """The concept's capability inventory groups (CONCEPT.md section 9)."""

    COMPOSITIONS = "compositions"
    FILES = "files_and_execution"
    TOOLS_MEDIA = "tools_and_media"
    HOSTED = "business_integrations"
    WEB = "web_and_research"
    REASONING = "reasoning_and_delegation"
    CONTEXT = "context_management"
    KNOWLEDGE = "knowledge_and_memory"
    INTERACTION = "interaction_and_controls"
    SELF_EXTENSION = "self_extension"
    SERVICES = "execution_services"
    DURABILITY = "durability"
    LOOP = "loop_customization"


class CatalogKind(StrEnum):
    """What a catalog factory produces.

    ``CAPABILITY`` plugins are offered as managed agent bindings. ``RESOURCE`` entries create
    deployment resources (media stores) consumed by capabilities such as ``StepPersistence``;
    they are registered with deployment tooling, not as toggleable agent behavior.
    """

    CAPABILITY = "capability"
    RESOURCE = "resource"


#: Stable resource names used by catalog factories.  Definitions declare these in their
#: ``resources`` list; deployments bind host values.  Kinds map to
#: :data:`harness.runtime.resources.ResourceKind`.
RESOURCE_KINDS: dict[str, ResourceKind] = {
    "workspace": "workspace",
    "model.default": "model",
    "model.judge": "model",
    "model.advisor": "model",
    "memory.store": "memory",
    "plan.store": "store",
    "spend.store": "store",
    "step.store": "store",
    "conversations": "store",
    "tool_overflow.store": "store",
    "media.store": "store",
    "mcp.toolset": "service",
    "thread.executor": "service",
    "user_answers": "service",
    "deferred.handler": "service",
    "image.generator": "service",
    "subagents.agents": "service",
    "dynamic.agents": "service",
    "render.app": "service",
    "wrapped.capability": "service",
    "wrapped.workspace": "service",
    "compaction.tiers": "custom",
    "compaction.fallback_chain": "custom",
    "callbacks.file_key": "custom",
    "callbacks.hooks": "custom",
    "callbacks.model_selector": "custom",
    "callbacks.model_id_resolver": "custom",
    "callbacks.prepare_tools": "custom",
    "callbacks.prepare_output_tools": "custom",
    "callbacks.history": "custom",
    "callbacks.event_stream": "custom",
    "callbacks.tool_guardrail": "custom",
    "callbacks.dynamic_reminders": "custom",
    "hooks": "custom",
    "defense": "service",
    "media.s3_credentials": "service",
    "media.postgres_pool": "service",
}


def _resource_kind(name: str) -> ResourceKind:
    return RESOURCE_KINDS.get(name, "custom")


# ---------------------------------------------------------------------------
# Shared config primitives
# ---------------------------------------------------------------------------


class _Config(BaseModel):
    """Base for catalog configuration models: strict, serializable, no secrets."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class _NoConfig(_Config):
    """Configuration model for entries that accept no settings."""


class BandAction(_Config):
    """One tool-output band action, mirroring the upstream ``Truncate``/``Spill`` family."""

    kind: Literal["truncate", "spill", "passthrough"] = "truncate"
    strategy: Literal["head_tail", "head", "tail"] = "head_tail"
    max_chars: int = Field(default=4_000, ge=1)
    preview_chars: int = Field(default=1_000, ge=1)
    then: BandAction | None = None


class BandConfig(_Config):
    """A size band (characters by default) and the action applied above it."""

    over: int = Field(ge=1)
    action: BandAction = Field(default_factory=BandAction)


class BudgetConfig(_Config):
    """A validated budget window; ``scope`` callables are Python-only and not exposed."""

    usd: Decimal | None = Field(default=None, ge=Decimal(0))
    tokens: int | None = Field(default=None, ge=1)
    window: Literal["run", "conversation", "day", "month", "total"] = "day"
    warn_at: float | None = Field(default=None, gt=0, lt=1)
    name: str = "default"


class DetectorConfig(_Config):
    """A serializable built-in guardrail detector."""

    kind: Literal["redact_secrets", "redact_personal_data", "blocked_keywords"]
    only: tuple[str, ...] | None = None
    extra: dict[str, str] | None = None
    keywords: tuple[str, ...] | None = None
    case_sensitive: bool = False
    whole_words: bool = False
    message: str | None = None

    @model_validator(mode="after")
    def _check(self) -> DetectorConfig:
        if self.kind == "blocked_keywords" and not self.keywords:
            raise ValueError("blocked_keywords requires a non-empty 'keywords' list")
        if self.kind != "blocked_keywords" and self.keywords:
            raise ValueError("'keywords' only applies to blocked_keywords")
        return self


class ReminderConfig(_Config):
    """A static system reminder schedule."""

    content: str = Field(min_length=1)
    interval: int = Field(default=1, ge=1)
    first_after: int | None = Field(default=None, ge=1)
    max_fires: int | None = Field(default=None, ge=1)
    tag: str | None = "system-reminder"


# --- core capability configs ------------------------------------------------


class LocalWorkspaceConfig(_Config):
    read_only: bool = False


class WebSearchConfig(_Config):
    native: bool = True
    local: bool = False
    search_context_size: Literal["low", "medium", "high"] | None = None
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    max_uses: int | None = Field(default=None, ge=1)


class WebFetchConfig(_Config):
    native: bool = True
    local: bool = False
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    max_uses: int | None = Field(default=None, ge=1)
    enable_citations: bool = False
    max_content_tokens: int | None = Field(default=None, ge=1)


class XSearchConfig(_Config):
    local: bool = False
    allowed_x_handles: tuple[str, ...] = ()
    excluded_x_handles: tuple[str, ...] = ()
    from_date: str | None = None
    to_date: str | None = None


class BrowserUseConfig(_Config):
    """Documented Browser Use settings; the module requires the [browser-use] extra."""

    llm: str | None = None
    allowed_domains: tuple[str, ...] = ()
    max_steps: int = Field(default=50, ge=1)
    headless: bool | None = None
    use_vision: bool | Literal["auto"] = True
    session_scope: Literal["call", "agent"] = "call"
    block_ip_addresses: bool = True
    guidance: str | None = None


class ThinkingConfig(_Config):
    effort: bool | Literal["minimal", "low", "medium", "high", "xhigh"] = True


class MCPConfig(_Config):
    url: str = Field(min_length=1)
    allowed_tools: tuple[str, ...] | None = None
    description: str | None = None


class MCPNativeConfig(_Config):
    url: str = Field(min_length=1)
    allowed_tools: tuple[str, ...] | None = None
    description: str | None = None


class ToolSearchConfig(_Config):
    max_results: int = Field(default=10, ge=1)
    tool_description: str | None = None
    parameter_description: str | None = None


class CachingConfig(_Config):
    retention: bool | Literal["5m", "1h"] = True
    messages: bool = True


class InstrumentationConfig(_Config):
    include_content: bool = True
    include_binary_content: bool = True
    include_model_request_parameters: bool = True
    version: Literal[2, 3, 4, 5, 6] = 5
    use_aggregated_usage_attribute_names: bool = True


class ImageGenerationConfig(_Config):
    native: bool = True
    local: bool = False
    action: Literal["generate", "edit", "auto"] | None = None
    background: Literal["transparent", "opaque", "auto"] | None = None
    moderation: Literal["auto", "low"] | None = None
    quality: Literal["low", "medium", "high", "auto"] | None = None
    size: str | None = None
    output_format: Literal["png", "webp", "jpeg"] | None = None
    image_model: str | None = None

    @model_validator(mode="after")
    def _check(self) -> ImageGenerationConfig:
        if self.local and not self.native:
            return self
        return self


class PrefixToolsConfig(_Config):
    prefix: str = Field(min_length=1)


class IncludeReturnSchemasConfig(_Config):
    tools: tuple[str, ...] | None = None


class SetToolMetadataConfig(_Config):
    tools: tuple[str, ...] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReinjectSystemPromptConfig(_Config):
    replace_existing: bool = False


class DeferredToolCallsConfig(_Config):
    """No serializable settings; the handler is a deployment resource."""


# --- harness: files, shell, workspaces --------------------------------------


class CoderConfig(_Config):
    instructions: str | None = None
    unrestricted_filesystem: bool = False
    repo_context: bool = True
    sub_agents: bool = False
    """Default ``False`` because the runtime composes managed bindings per run: upstream
    refuses a self-delegating ``Coder`` passed at run scope. Set ``True`` only when the
    definition is a Python factory that binds ``managed_capabilities`` at agent construction."""
    agent_folders: tuple[str, ...] | None = None
    caching: bool = True


class ResearcherConfig(_Config):
    instructions: str | None = None
    replace_default_instructions: bool = False
    include_default_subagent: bool = True


class FileSystemConfig(_Config):
    root_dir: str | None = None
    read_only: bool = False
    allowed_patterns: tuple[str, ...] = ()
    denied_patterns: tuple[str, ...] = ()
    read_only_patterns: tuple[str, ...] | None = None
    max_read_lines: int = Field(default=2_000, ge=1)
    max_read_chars: int | None = Field(default=50_000, ge=1)
    content_hashes: bool = True
    tools: tuple[str, ...] | None = None


class ShellConfig(_Config):
    allowed_commands: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] | None = None
    default_timeout: float = Field(default=30.0, gt=0)
    max_output_chars: int = Field(default=50_000, ge=1)
    persist_cwd: bool = False
    allow_interactive: bool = False
    max_file_bytes: int | None = Field(default=None, ge=1)


class SSHWorkspaceConfig(_Config):
    destination: str = Field(min_length=1)
    working_dir: str | None = None
    read_only: bool = False
    ssh_args: tuple[str, ...] = ()


class SandboxConfig(_Config):
    """Common sandbox settings; unsupported keys are rejected per concrete entry."""

    working_dir: str | None = None
    idle_timeout: float | None = Field(default=None, gt=0)
    warn_if_no_tools: bool = True


class SmolSandboxConfig(_Config):
    image: str | None = None
    cpus: int | None = Field(default=None, ge=1)
    memory_mb: int | None = Field(default=None, ge=64)
    allow_internet_access: bool = False
    working_dir: str = "/workspace"


class BubblewrapConfig(_Config):
    network: bool = False
    bwrap_args: tuple[str, ...] = ()


class MacroscopeConfig(_Config):
    base: str | None = None
    command: str = "macroscope"
    timeout: float = Field(default=600.0, gt=0)
    guidance: str | None = None


class LocalStackConfig(_Config):
    endpoint_url: str = "http://localhost.localstack.cloud:4566"
    region: str = "us-east-1"
    allowed_services: tuple[str, ...] = ()
    denied_services: tuple[str, ...] = ()
    manage_container: bool = False

    @model_validator(mode="after")
    def _check(self) -> LocalStackConfig:
        if self.allowed_services and self.denied_services:
            raise ValueError("allowed_services and denied_services are mutually exclusive")
        return self


# --- harness: reasoning and delegation -------------------------------------


class PlanningConfig(_Config):
    guidance: str | None = None
    cache_ttl: Literal["5m", "1h"] = "5m"
    store: Literal["run", "resource"] = "run"
    enable_subtasks: bool = False
    inject: bool = True
    tools: tuple[str, ...] | None = None


class SubAgentsConfig(_Config):
    forward_usage: bool = True
    include_self: bool = False
    max_depth: int = Field(default=3, ge=1)
    tool_name: str = "delegate_task"


class DynamicWorkflowConfig(_Config):
    tool_name: str = "run_workflow"
    max_agent_calls: int = Field(default=50, ge=1)
    max_retries: int = Field(default=3, ge=0)
    forward_usage: bool = True
    inherit_model: bool = False


class AdvisorConfig(_Config):
    mode: Literal["auto", "native", "local"] = "auto"
    max_uses: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1024)
    caching: bool | None = None
    forward_history: bool = False


class BackgroundToolsConfig(_Config):
    tools: Literal["all"] | tuple[str, ...] = "all"
    metadata: dict[str, str] | None = None


# --- harness: context management -------------------------------------------


class _AtLeastOneTrigger(_Config):
    max_messages: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_fraction: float | None = Field(default=None, gt=0, lt=1)

    @model_validator(mode="after")
    def _one_trigger(self) -> _AtLeastOneTrigger:
        triggers = [self.max_messages, self.max_tokens, self.max_fraction]
        if not any(value is not None for value in triggers):
            raise ValueError("set at least one of max_messages, max_tokens, or max_fraction")
        if self.max_tokens is not None and self.max_fraction is not None:
            raise ValueError("max_tokens and max_fraction are mutually exclusive")
        return self


class ClearToolResultsConfig(_AtLeastOneTrigger):
    keep_pairs: int = Field(default=3, ge=0)
    placeholder: str = "[tool result cleared]"
    clear_tool_inputs: bool = False
    min_clear_tokens: int | None = Field(default=None, ge=0)


class SlidingWindowConfig(_AtLeastOneTrigger):
    keep_messages: int = Field(default=40, ge=0)
    keep_tokens: int | None = Field(default=None, ge=0)
    preserve_first_user_message: bool = True
    receipts: bool = False


class SummarizingConfig(_AtLeastOneTrigger):
    model: str | None = None
    keep_messages: int = Field(default=20, ge=0)
    keep_tokens: int | None = Field(default=None, ge=0)
    incremental: bool = False
    keep_user_messages: bool = False
    instructions: str | None = None


class TieredConfig(_Config):
    target_tokens: int | None = Field(default=None, ge=1)
    target_fraction: float | None = Field(default=None, gt=0, lt=1)
    context_window: int | None = Field(default=None, ge=1)
    fallback_context_window: int = Field(default=200_000, ge=1)

    @model_validator(mode="after")
    def _one_target(self) -> TieredConfig:
        if (self.target_tokens is None) == (self.target_fraction is None):
            raise ValueError("set exactly one of target_tokens or target_fraction")
        return self


class FallbackCompactionConfig(_Config):
    max_tokens: int | None = Field(default=None, ge=1)
    max_fraction: float | None = Field(default=None, gt=0, lt=1)

    @model_validator(mode="after")
    def _exclusive(self) -> FallbackCompactionConfig:
        if self.max_tokens is not None and self.max_fraction is not None:
            raise ValueError("max_tokens and max_fraction are mutually exclusive")
        return self


class ClampOversizedConfig(_Config):
    max_part_tokens: int | None = Field(default=None, ge=1)
    max_part_chars: int | None = Field(default=None, ge=1)
    keep_head_chars: int = Field(default=2_000, ge=0)
    keep_tail_chars: int = Field(default=2_000, ge=0)
    clamp_tool_call_args: bool = True

    @model_validator(mode="after")
    def _one_limit(self) -> ClampOversizedConfig:
        if self.max_part_tokens is None and self.max_part_chars is None:
            raise ValueError("set max_part_tokens or max_part_chars")
        return self


class DeduplicateReadsConfig(_Config):
    placeholder: str = "[superseded file read]"
    max_messages: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)


class WarnNearLimitsConfig(_Config):
    max_iterations: int | None = Field(default=None, ge=1)
    max_context_tokens: int | None = Field(default=None, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_context_fraction: float | None = Field(default=None, gt=0, lt=1)
    warning_threshold: float = Field(default=0.7, gt=0, lt=1)
    critical_remaining_iterations: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _one_limit(self) -> WarnNearLimitsConfig:
        if not any(
            value is not None
            for value in (
                self.max_iterations,
                self.max_context_tokens,
                self.max_total_tokens,
                self.max_context_fraction,
            )
        ):
            raise ValueError("set at least one limit to warn about")
        return self


class ReportContextUsageConfig(_Config):
    context_window: int | None = Field(default=None, ge=1)
    fallback_context_window: int = Field(default=200_000, ge=1)


class ToolOutputLimitsConfig(_Config):
    bands: tuple[BandConfig, ...] = (BandConfig(over=10_000),)
    per_tool: dict[str, tuple[BandConfig, ...]] = Field(default_factory=dict)
    tool_filter: tuple[str, ...] | None = None
    over_tokens: bool = False
    strip_ansi: bool = False
    store: Literal["workspace", "local_file", "resource"] = "workspace"
    summary_prompt: str | None = None


class WarnOnCacheBustsConfig(_Config):
    collapse_ratio: float | None = Field(default=None, gt=0)
    min_prefix_tokens: int | None = Field(default=None, ge=0)
    min_missed_ratio: float = Field(default=0.05, gt=0, lt=1)
    min_missed_tokens: int = Field(default=2_000, ge=1)
    cache_ttl_seconds: float | None = Field(default=None, gt=0)


# --- harness: knowledge and memory -----------------------------------------


class MemoryConfig(_Config):
    agent_name: str = "main"
    namespace: str = ""
    inject_memory: bool = True
    max_tokens: int = Field(default=2_000, ge=1)
    max_lines: int = Field(default=200, ge=1)
    max_memory_size: int = Field(default=65_536, ge=1)
    max_search_results: int = Field(default=10, ge=1)
    max_search_result_chars: int = Field(default=4_000, ge=1)
    max_search_files: int = Field(default=1_000, ge=1)
    guidance: str | None = None
    injection_errors: Literal["ignore", "raise"] = "ignore"
    heading: str = ""


class ConversationSearchConfig(_Config):
    scope: Literal["conversation", "all"] = "conversation"
    max_matches: int = Field(default=10, ge=1)
    context_lines: int = Field(default=5, ge=0)
    bm25_k1: float = Field(default=1.5, gt=0)
    bm25_b: float = Field(default=0.75, ge=0, le=1)
    add_instructions: bool = True
    tool_id: str = "conversation-search"


class SkillsConfig(_Config):
    directories: tuple[str, ...] = (".agents/skills",)
    include: tuple[str, ...] | None = None
    exclude: tuple[str, ...] | None = None
    missing_directories: Literal["error", "skip"] = "error"
    duplicate_names: Literal["error", "keep_first"] = "error"

    @model_validator(mode="after")
    def _exclusive(self) -> SkillsConfig:
        if self.include is not None and self.exclude is not None:
            raise ValueError("include and exclude are mutually exclusive")
        return self


class RepoContextConfig(_Config):
    home_dir: str | None = None
    filenames: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
    autoload_instructions: bool = True
    expose_inventory_tool: bool = False
    inventory_tool_name: str = "inventory_agent_context"
    nested_traversal: bool = False
    nested_inject: Literal["pointer", "contents"] = "pointer"


class PydanticAIDocsConfig(_Config):
    local_docs_path: str | None = None
    cache: bool = True


# --- harness: controls ------------------------------------------------------


class InputGuardrailConfig(_Config):
    detectors: tuple[DetectorConfig, ...] = Field(min_length=1)
    parallel: bool = False

    @model_validator(mode="after")
    def _no_replace_in_parallel(self) -> InputGuardrailConfig:
        if self.parallel and any(
            detector.kind in {"redact_secrets", "redact_personal_data"}
            for detector in self.detectors
        ):
            raise ValueError(
                "redacting detectors cannot run with parallel=True (replace is refused)"
            )
        return self


class OutputGuardrailConfig(_Config):
    detectors: tuple[DetectorConfig, ...] = Field(min_length=1)


class ToolGuardrailConfig(_Config):
    tools: tuple[str, ...] | None = None
    hidden: tuple[str, ...] = ()


class PromptInjectionDefenderConfig(_Config):
    block_high_risk: bool | None = None
    tool_filter: Literal["all"] | tuple[str, ...] = "all"
    semantic_detection: bool = False
    blocked_message: str | None = None


class ToolCallJudgeConfig(_Config):
    question: str = Field(min_length=1)
    tools: tuple[str, ...] | None = None
    include_conversation: bool = False
    conversation_window: int = Field(default=4_000, ge=1)
    on_uncertain: Literal["block", "allow", "ask"] = "block"
    denial_message: str | None = None


class ApprovalReviewerConfig(_Config):
    question: str = Field(min_length=1)
    tools: tuple[str, ...] | None = None
    include_conversation: bool = False
    conversation_window: int = Field(default=4_000, ge=1)


class SpendLimitsConfig(_Config):
    budgets: tuple[BudgetConfig, ...] = ()
    store: Literal["in_memory", "resource"] = "in_memory"
    on_unpriced: Literal["zero", "raise"] = "zero"
    expose_tools: bool = False


class AskUserConfig(_Config):
    timeout: float | None = Field(default=None, gt=0)


class SystemRemindersConfig(_Config):
    reminders: tuple[ReminderConfig, ...] = ()
    goal_reanchor: bool = False
    cache_ttl: Literal["5m", "1h"] = "5m"

    @model_validator(mode="after")
    def _at_least_one(self) -> SystemRemindersConfig:
        if not self.reminders and not self.goal_reanchor:
            raise ValueError("provide at least one reminder or set goal_reanchor=True")
        return self


class TrajectoryJudgeConfig(_Config):
    every: int = Field(default=10, ge=1)
    window: int = Field(default=20_000, ge=1)
    name: str | None = None
    instructions: str | None = None


# --- harness: services and durability --------------------------------------


class CapabilityCreationConfig(_Config):
    directory: str = ".authored"
    guidance: str | None = None


class StepPersistenceConfig(_Config):
    agent_name: str | None = None
    capture_frontier: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)


class ManagedPromptConfig(_Config):
    name: str = Field(min_length=1)
    default: str = Field(min_length=1)
    label: str = "production"
    render_template: bool = False


class DurabilityConfig(_Config):
    name: str | None = None


class ParallelDurabilityConfig(DurabilityConfig):
    parallel_execution_mode: Literal["sequential", "parallel_ordered_events"] = "sequential"


class MediaStoreConfig(_Config):
    """Base for media-store resource factories (not agent capabilities)."""


class DiskMediaStoreConfig(MediaStoreConfig):
    directory: str = Field(min_length=1)


class SqliteMediaStoreConfig(MediaStoreConfig):
    database: str | None = None


class S3MediaStoreConfig(MediaStoreConfig):
    bucket: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    region: str = Field(min_length=1)
    key_prefix: str = ""


class PostgresMediaStoreConfig(MediaStoreConfig):
    pass


# --- hosted integrations ----------------------------------------------------


class HostedIntegrationConfig(_Config):
    """Shared non-secret settings for hosted MCP integrations.

    Credentials are deliberately absent: deployments provide the vendor environment variable
    (for example ``GITHUB_TOKEN``) or an ``auth`` callable through host configuration.
    """

    read_only: bool = False
    include_instructions: bool = True
    id: str | None = None


class GitHubConfig(HostedIntegrationConfig):
    toolsets: tuple[str, ...] | None = None
    url: str | None = None


class GoogleWorkspaceConfig(HostedIntegrationConfig):
    services: tuple[str, ...] = Field(min_length=1)


class StackOneConfig(_Config):
    account_id: str = Field(min_length=1)
    actions: tuple[str, ...] | None = None


class PostHogConfig(HostedIntegrationConfig):
    features: tuple[str, ...] | None = None


class LogfireMCPConfig(HostedIntegrationConfig):
    url: str | None = None


# ---------------------------------------------------------------------------
# Lazy import and resource helpers
# ---------------------------------------------------------------------------


class _MissingExtra(HarnessError):
    pass


def _load(plugin_id: str, import_ref: str, install_hint: str | None) -> Any:
    """Import an upstream target lazily, translating failures into actionable errors."""
    module_name, _, attribute = import_ref.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        hint = f" Install it with `{install_hint}`." if install_hint else ""
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Plugin {plugin_id!r} needs an optional dependency that is not installed: {exc}.{hint}",
            details={
                "plugin_id": plugin_id,
                "import_ref": import_ref,
                "install_hint": install_hint,
            },
        ) from exc
    try:
        return getattr(module, attribute)
    except AttributeError as exc:  # pragma: no cover - protects the catalog inventory
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Plugin {plugin_id!r} import target {import_ref!r} has no attribute {attribute!r}; "
            "the installed upstream release changed.",
            details={"plugin_id": plugin_id, "import_ref": import_ref},
        ) from exc


def _cfg(config: Any, field: str, default: Any = None) -> Any:
    """Read one validated config field from a model instance or a mapping shim."""
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(field, default)
    return getattr(config, field, default)


def _present(config: Any, field: str) -> bool:
    if config is None:
        return False
    if isinstance(config, Mapping):
        return field in config
    return hasattr(config, field)


def _resource(
    binding: BindingContext,
    name: str,
    *,
    plugin_id: str,
    description: str = "",
    required: bool = True,
    default: Any = None,
) -> Any:
    """Resolve one deployment resource, raising the runtime's availability error when absent."""
    resources = getattr(binding, "resources", None)
    bound = resources is not None and name in resources
    if not bound:
        if not required:
            return default
        code = (
            "WORKSPACE_UNAVAILABLE"
            if _resource_kind(name) == "workspace"
            else "RESOURCE_UNAVAILABLE"
        )
        suffix = f" ({description})" if description else ""
        raise HarnessError(
            code,
            f"Plugin {plugin_id!r} requires resource {name!r}{suffix}, which this deployment "
            "does not bind. Bind it explicitly; there is no host fallback.",
            details={
                "plugin_id": plugin_id,
                "resource": name,
                "kind": _resource_kind(name),
                **(
                    {"binding_id": binding.binding_id}
                    if getattr(binding, "binding_id", None)
                    else {}
                ),
            },
        )
    try:
        return resources[name]  # type: ignore[index]
    except (KeyError, TypeError) as exc:  # pragma: no cover - ResourceSet is well-formed
        raise HarnessError(
            "RESOURCE_UNAVAILABLE",
            f"Resource {name!r} could not be resolved from the deployment's resource set.",
            details={"plugin_id": plugin_id, "resource": name},
        ) from exc


def _require_module(plugin_id: str, module_name: str, install_hint: str) -> None:
    """Fail early with the install command when an optional runtime dependency is absent."""
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Plugin {plugin_id!r} requires optional dependency {module_name!r}. "
            f"Install it with `{install_hint}`.",
            details={"plugin_id": plugin_id, "module": module_name, "install_hint": install_hint},
        ) from exc


def _constructor(
    plugin_id: str,
    import_ref: str,
    install_hint: str | None,
    *,
    fields: Mapping[str, str],
    skip_none: bool = True,
) -> Callable[[Any, BindingContext], Any]:
    """Build a lazy factory that forwards validated config fields to ``cls(**kwargs)``."""

    def factory(config: Any, binding: BindingContext) -> Any:
        target = _load(plugin_id, import_ref, install_hint)
        kwargs: dict[str, Any] = {}
        for config_field, parameter in fields.items():
            value = _cfg(config, config_field)
            if value is None and skip_none:
                continue
            kwargs[parameter] = value
        return target(**kwargs)

    factory.__name__ = f"build_{plugin_id.replace('.', '_').replace('-', '_')}"
    return factory


def _call_with(
    plugin_id: str,
    import_ref: str,
    install_hint: str | None,
    build: Callable[[Any, Any, BindingContext], Any],
) -> Callable[[Any, BindingContext], Any]:
    """Build a factory with a per-entry builder callback ``(cls, config, binding)``."""

    def factory(config: Any, binding: BindingContext) -> Any:
        target = _load(plugin_id, import_ref, install_hint)
        return build(target, config, binding)

    factory.__name__ = f"build_{plugin_id.replace('.', '_').replace('-', '_')}"
    return factory


def _as_list(value: Sequence[str] | None) -> list[str] | None:
    if value is None:
        return None
    return list(value)


# ---------------------------------------------------------------------------
# Catalog entry model
# ---------------------------------------------------------------------------


class CatalogEntry(BaseModel):
    """One reviewed upstream capability with honest coverage and a lazy factory."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    plugin_id: str = Field(min_length=1, max_length=200)
    """Stable publisher-qualified plugin id; never derived from import order."""

    name: str
    description: str = ""
    integration: Literal["pydantic-ai-slim", "pydantic-ai-harness"]
    upstream_import: str | None = None
    """Real ``module:attr`` import target; ``None`` only for genuinely absent upstream APIs."""

    version: str
    """Installed upstream distribution version this entry was reviewed against."""

    group: CatalogGroup
    tier: CatalogTier
    kind: CatalogKind = CatalogKind.CAPABILITY

    config_model: type[BaseModel] | None = None
    factory: Callable[[Any, BindingContext], Any] | None = None

    extras: tuple[str, ...] = ()
    """Exact install commands for missing optional dependencies."""

    requires_modules: tuple[str, ...] = ()
    """Importable module ids gated by the registry before composition."""

    requirements: tuple[str, ...] = ()
    """Binding-id dependencies enforced by the runtime's dependency graph.

    Catalog plugins currently express *resource* needs through
    :attr:`deployment_resources` instead: resource names are definition-level facts, and
    treating them as binding ids would create false edges when a binding is named after its
    resource (for example a binding called ``workspace``).
    """

    deployment_resources: tuple[str, ...] = ()
    """Named deployment resources this plugin's factory requires when composed.

    Declared on the agent definition as ``ResourceRequirement`` entries; a missing value
    fails the turn explicitly. Never a binding dependency.
    """

    conflicts: tuple[str, ...] = ()
    """Documented incompatible binding ids (empty when only tool-name overlap applies)."""

    change_mode: Literal["next_turn", "new_session"] = "new_session"
    state_mode: Literal["run", "session", "external"] = "run"
    presentation: tuple[str, ...] = ("generic",)

    default_policy: CapabilityPolicy | None = None
    notes: tuple[str, ...] = ()
    """Honest constraints, provider requirements, and verification limits."""

    signature_verified: bool = True
    """Whether constructor parameters were inspected in the pinned environment (not merely
    taken from upstream documentation)."""

    starter_roles: tuple[Literal["developer", "research"], ...] = ()
    disabled_in_starters: bool = False

    @property
    def managed(self) -> bool:
        """Whether this entry offers a validated descriptor and factory right now."""
        return self.config_model is not None and self.factory is not None

    def policy(self) -> CapabilityPolicy:
        if self.default_policy is not None:
            return self.default_policy
        editable = set(self.config_model.model_fields) if self.config_model is not None else set()
        return CapabilityPolicy(
            available=self.tier is not CatalogTier.UNAVAILABLE,
            enabled_by_default=False,
            client_can_toggle=True,
            editable_fields=editable,
            change_mode=self.change_mode,
        )

    @model_validator(mode="after")
    def _policy_lifecycle_mirror(self) -> CatalogEntry:
        # The protocol makes the policy the authoritative lifecycle source; the descriptor's
        # top-level change_mode is only a mirror. Keep catalog data consistent up front.
        if self.default_policy is not None and self.default_policy.change_mode != self.change_mode:
            raise ValueError(
                "default_policy.change_mode must equal the catalog entry's change_mode; "
                "policy is authoritative for lifecycle"
            )
        return self

    def descriptor(self) -> PluginDescriptor:
        """Convert to the runtime's management descriptor."""
        if self.factory is None:
            raise HarnessError(
                "CAPABILITY_UNAVAILABLE",
                f"Catalog entry {self.plugin_id!r} has no factory in this release and cannot be "
                "registered as a plugin.",
                details={"plugin_id": self.plugin_id, "tier": self.tier.value},
            )
        constraint_parts = list(self.notes)
        if self.tier is CatalogTier.UNAVAILABLE:
            constraint_parts.insert(0, "not present in the pinned upstream release")
        compatibility = CompatibilityConstraints(
            runtime_compatible=self.tier is not CatalogTier.UNAVAILABLE,
            requires_extras=list(self.requires_modules),
            constraint=" ".join(constraint_parts) or None,
        )
        return PluginDescriptor(
            plugin_id=self.plugin_id,
            version=self.version,
            description=self.description,
            config_model=self.config_model,
            factory=self.factory,
            requirements=list(self.requirements),
            conflicts=list(self.conflicts),
            change_mode=self.change_mode,
            state_mode=self.state_mode,
            presentation=list(self.presentation),
            default_policy=self.policy(),
            compatibility=compatibility,
        )

    def info(self) -> PluginInfo:
        """Honest wire view, including tier metadata where the protocol exposes it."""
        return PluginInfo(
            plugin_id=self.plugin_id,
            version=self.version,
            description=self.description,
            config_schema=(
                self.config_model.model_json_schema(mode="validation")
                if self.config_model is not None
                else {}
            ),
            change_mode=self.change_mode,
            state_mode=self.state_mode,
            runtime_compatible=self.tier is not CatalogTier.UNAVAILABLE,
            managed=self.config_model is not None and self.factory is not None,
            presentation=self.presentation[0] if self.presentation else "generic",
            # PluginInfo is informational; resource needs are visible to clients without
            # becoming binding-dependency edges in the policy graph.
            requirements=list(self.deployment_resources),
            constraint=" ".join(self.notes) or None,
        )


PLUGIN_VERSION = {
    "pydantic-ai-slim": "2.57.0",
    "pydantic-ai-harness": "0.57.0",
}

CORE_EXTRA = "pydantic-ai-slim"
HARNESS_EXTRA = "pydantic-ai-harness"


def _install(extra_group: str, *extras: str) -> str:
    joined = ",".join(extras)
    return f"{extra_group}[{joined}]"


# ---------------------------------------------------------------------------
# Factory implementations
# ---------------------------------------------------------------------------

_WORKSPACE_DIR_TYPES = (str, Path)


def _workspace_dir(plugin_id: str, binding: BindingContext) -> Any:
    """Resolve the deployment workspace into a path accepted by ``LocalWorkspace``."""
    value = _resource(
        binding,
        "workspace",
        plugin_id=plugin_id,
        description="local working directory or a WorkspaceBackend with a working_dir",
    )
    if isinstance(value, _WORKSPACE_DIR_TYPES):
        return value
    ref = getattr(value, "ref", None)
    ref_id = getattr(ref, "id", None)
    if getattr(ref, "provider", None) == "local" and isinstance(ref_id, _WORKSPACE_DIR_TYPES):
        return ref_id
    for attribute in ("working_dir", "root", "path"):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, _WORKSPACE_DIR_TYPES):
            return candidate
    raise HarnessError(
        "INVALID_CONFIGURATION",
        f"Plugin {plugin_id!r} cannot derive a local working directory from the bound workspace "
        f"resource ({type(value).__name__}); bind a path or a LocalWorkspaceBackend, or use a "
        "sandbox capability binding instead.",
        details={"plugin_id": plugin_id, "resource": "workspace"},
    )


def _build_local_workspace(_cls: Any, config: Any, binding: BindingContext) -> Any:
    return _cls(
        working_dir=_workspace_dir("pydantic.local_workspace", binding),
        read_only=_cfg(config, "read_only", False),
    )


def _build_web_search(_cls: Any, config: Any, binding: BindingContext) -> Any:
    local = _cfg(config, "local", False)
    native = _cfg(config, "native", True)
    if local:
        _require_module(
            "pydantic.web_search",
            "ddgs",
            _install(CORE_EXTRA, "duckduckgo"),
        )
    if not native and not local:
        raise HarnessError(
            "INVALID_CONFIGURATION",
            "pydantic.web_search needs native=True and/or local=True; both were disabled.",
            details={"plugin_id": "pydantic.web_search"},
        )
    kwargs: dict[str, Any] = {
        "native": native,
        "local": True if local else None,
        "search_context_size": _cfg(config, "search_context_size"),
        "max_uses": _cfg(config, "max_uses"),
    }
    allowed = _as_list(_cfg(config, "allowed_domains", ()))
    blocked = _as_list(_cfg(config, "blocked_domains", ()))
    if allowed:
        kwargs["allowed_domains"] = allowed
    if blocked:
        kwargs["blocked_domains"] = blocked
    return _cls(**kwargs)


def _build_web_fetch(_cls: Any, config: Any, binding: BindingContext) -> Any:
    local = _cfg(config, "local", False)
    native = _cfg(config, "native", True)
    if local:
        _require_module(
            "pydantic.web_fetch",
            "markdownify",
            _install(CORE_EXTRA, "web-fetch"),
        )
    if not native and not local:
        raise HarnessError(
            "INVALID_CONFIGURATION",
            "pydantic.web_fetch needs native=True and/or local=True; both were disabled.",
            details={"plugin_id": "pydantic.web_fetch"},
        )
    kwargs: dict[str, Any] = {
        "native": native,
        "local": True if local else None,
        "max_uses": _cfg(config, "max_uses"),
        "enable_citations": _cfg(config, "enable_citations", False),
        "max_content_tokens": _cfg(config, "max_content_tokens"),
    }
    allowed = _as_list(_cfg(config, "allowed_domains", ()))
    blocked = _as_list(_cfg(config, "blocked_domains", ()))
    if allowed:
        kwargs["allowed_domains"] = allowed
    if blocked:
        kwargs["blocked_domains"] = blocked
    return _cls(**kwargs)


def _build_x_search(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {
        "local": _cfg(config, "local", False),
        "allowed_x_handles": _as_list(_cfg(config, "allowed_x_handles", ())) or None,
        "excluded_x_handles": _as_list(_cfg(config, "excluded_x_handles", ())) or None,
    }
    return _cls(**{key: value for key, value in kwargs.items() if value is not None})


def _build_browser_use(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {
        "max_steps": _cfg(config, "max_steps", 50),
        "use_vision": _cfg(config, "use_vision", True),
        "session_scope": _cfg(config, "session_scope", "call"),
        "block_ip_addresses": _cfg(config, "block_ip_addresses", True),
    }
    for field in ("llm", "headless", "guidance"):
        value = _cfg(config, field)
        if value is not None:
            kwargs[field] = value
    domains = _as_list(_cfg(config, "allowed_domains", ()))
    if domains:
        kwargs["allowed_domains"] = domains
    return _cls(**kwargs)


def _build_mcp(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {"url": _cfg(config, "url")}
    allowed = _as_list(_cfg(config, "allowed_tools"))
    if allowed is not None:
        kwargs["allowed_tools"] = allowed
    if _cfg(config, "description") is not None:
        kwargs["description"] = _cfg(config, "description")
    return _cls(**kwargs)


def _build_mcp_native(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {"native": True, "url": _cfg(config, "url")}
    allowed = _as_list(_cfg(config, "allowed_tools"))
    if allowed is not None:
        kwargs["allowed_tools"] = allowed
    if _cfg(config, "description") is not None:
        kwargs["description"] = _cfg(config, "description")
    return _cls(**kwargs)


def _build_image_generation(_cls: Any, config: Any, binding: BindingContext) -> Any:
    local = _cfg(config, "local", False)
    native = _cfg(config, "native", True)
    kwargs: dict[str, Any] = {"native": native}
    if local:
        kwargs["local"] = _resource(
            binding,
            "image.generator",
            plugin_id="pydantic.image_generation",
            description="an ImageGenerator for local (non-native) generation",
        )
    for field in (
        "action",
        "background",
        "moderation",
        "quality",
        "size",
        "output_format",
        "image_model",
    ):
        value = _cfg(config, field)
        if value is not None:
            kwargs[field] = value
    return _cls(**kwargs)


def _build_prefix_tools(_cls: Any, config: Any, binding: BindingContext) -> Any:
    wrapped = _resource(
        binding,
        "wrapped.capability",
        plugin_id="pydantic.prefix_tools",
        description="the native capability whose tools/instructions are prefixed",
    )
    return _cls(wrapped=wrapped, prefix=_cfg(config, "prefix"))


def _build_include_schemas(_cls: Any, config: Any, binding: BindingContext) -> Any:
    tools = _as_list(_cfg(config, "tools"))
    return _cls(tools=tools if tools is not None else "all")


def _build_set_tool_metadata(_cls: Any, config: Any, binding: BindingContext) -> Any:
    tools = _as_list(_cfg(config, "tools"))
    metadata = dict(_cfg(config, "metadata", {}) or {})
    return _cls(tools=tools if tools is not None else "all", **metadata)


def _build_hooks(_cls: Any, config: Any, binding: BindingContext) -> Any:
    return _resource(
        binding,
        "hooks",
        plugin_id="pydantic.hooks",
        description="a deployment-built pydantic_ai.capabilities.Hooks instance",
    )


def _build_use_thread_executor(_cls: Any, config: Any, binding: BindingContext) -> Any:
    executor = _resource(
        binding,
        "thread.executor",
        plugin_id="pydantic.use_thread_executor",
        description="a concurrent.futures.Executor supplied by the host",
    )
    return _cls(executor=executor)


def _build_handle_deferred(_cls: Any, config: Any, binding: BindingContext) -> Any:
    handler = _resource(
        binding,
        "deferred.handler",
        plugin_id="pydantic.handle_deferred_tool_calls",
        description="host callback returning DeferredToolResults for approvals",
    )
    return _cls(handler=handler)


def _callback(
    import_ref: str, plugin_id: str, resource: str, description: str
) -> Callable[..., Any]:
    def factory(_config: Any, binding: BindingContext) -> Any:
        callback = _resource(binding, resource, plugin_id=plugin_id, description=description)
        return _load(plugin_id, import_ref, None)(callback)

    factory.__name__ = f"build_{plugin_id.replace('.', '_')}"
    return factory


def _prepared(import_ref: str, plugin_id: str) -> Callable[..., Any]:
    def factory(_config: Any, binding: BindingContext) -> Any:
        callback = _resource(
            binding,
            "callbacks.prepare_tools",
            plugin_id=plugin_id,
            description="prepare callback (RunContext, ToolDefinition) -> list[ToolDefinition]",
        )
        return _load(plugin_id, import_ref, None)(callback)

    factory.__name__ = f"build_{plugin_id.replace('.', '_')}"
    return factory


# — harness: files and workspaces —


def _build_filesystem(_cls: Any, config: Any, binding: BindingContext) -> Any:
    _resource(binding, "workspace", plugin_id="harness.filesystem", description="run workspace")
    kwargs: dict[str, Any] = {
        "root_dir": _cfg(config, "root_dir"),
        "read_only": _cfg(config, "read_only", False),
        "allowed_patterns": list(_cfg(config, "allowed_patterns", ()) or ()),
        "denied_patterns": list(_cfg(config, "denied_patterns", ()) or ()),
        "max_read_lines": _cfg(config, "max_read_lines", 2_000),
        "max_read_chars": _cfg(config, "max_read_chars", 50_000),
        "content_hashes": _cfg(config, "content_hashes", True),
    }
    patterns = _as_list(_cfg(config, "read_only_patterns"))
    if patterns is not None:
        kwargs["read_only_patterns"] = patterns
    tools = _as_list(_cfg(config, "tools"))
    if tools is not None:
        kwargs["tools"] = tools
    return _cls(**kwargs)


def _build_shell(_cls: Any, config: Any, binding: BindingContext) -> Any:
    _resource(binding, "workspace", plugin_id="harness.shell", description="run workspace")
    kwargs: dict[str, Any] = {
        "allowed_commands": list(_cfg(config, "allowed_commands", ()) or ()),
        "default_timeout": _cfg(config, "default_timeout", 30.0),
        "max_output_chars": _cfg(config, "max_output_chars", 50_000),
        "persist_cwd": _cfg(config, "persist_cwd", False),
        "allow_interactive": _cfg(config, "allow_interactive", False),
    }
    denied = _as_list(_cfg(config, "denied_commands"))
    if denied is not None:
        kwargs["denied_commands"] = denied
    if _cfg(config, "max_file_bytes") is not None:
        kwargs["max_file_bytes"] = _cfg(config, "max_file_bytes")
    return _cls(**kwargs)


def _build_coder(_cls: Any, config: Any, binding: BindingContext) -> Any:
    _resource(binding, "workspace", plugin_id="harness.coder", description="run workspace")
    kwargs: dict[str, Any] = {
        "instructions": _cfg(config, "instructions"),
        "unrestricted_filesystem": _cfg(config, "unrestricted_filesystem", False),
        "repo_context": _cfg(config, "repo_context", True),
        "sub_agents": _cfg(config, "sub_agents", False),
        "caching": _cfg(config, "caching", True),
    }
    folders = _as_list(_cfg(config, "agent_folders"))
    if folders is not None:
        kwargs["agent_folders"] = folders
    return _cls(**{key: value for key, value in kwargs.items() if value is not None})


def _build_researcher(_cls: Any, config: Any, binding: BindingContext) -> Any:
    _resource(
        binding,
        "workspace",
        plugin_id="harness.researcher",
        description="workspace for spill-to-workspace tool output limits",
    )
    kwargs: dict[str, Any] = {}
    if (
        _cfg(config, "replace_default_instructions", False)
        or _cfg(config, "instructions") is not None
    ):
        kwargs["instructions"] = _cfg(config, "instructions")
    if not _cfg(config, "include_default_subagent", True):
        kwargs["subagents"] = []
    return _cls(**kwargs)


def _build_ssh(_cls: Any, config: Any, binding: BindingContext) -> Any:
    return _cls(
        destination=_cfg(config, "destination"),
        working_dir=_cfg(config, "working_dir"),
        read_only=_cfg(config, "read_only", False),
        ssh_args=tuple(_cfg(config, "ssh_args", ()) or ()),
    )


def _build_sandbox_fields(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {}
    for field in ("working_dir", "idle_timeout", "warn_if_no_tools"):
        value = _cfg(config, field)
        if value is not None:
            kwargs[field] = value
    return _cls(**kwargs)


def _build_smol(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {"working_dir": _cfg(config, "working_dir", "/workspace")}
    for field in ("image", "cpus", "memory_mb", "allow_internet_access"):
        value = _cfg(config, field)
        if value is not None:
            kwargs[field] = value
    return _cls(**kwargs)


def _build_bubblewrap(_cls: Any, config: Any, binding: BindingContext) -> Any:
    wrapped = _resource(
        binding,
        "wrapped.workspace",
        plugin_id="harness.sandbox_bubblewrap",
        description="a workspace capability instance (LocalWorkspace or SSHWorkspace)",
    )
    return _cls(
        wrapped,
        network=_cfg(config, "network", False),
        bwrap_args=tuple(_cfg(config, "bwrap_args", ()) or ()),
    )


def _build_macroscope(_cls: Any, config: Any, binding: BindingContext) -> Any:
    _resource(binding, "workspace", plugin_id="harness.macroscope", description="run workspace")
    return _cls(
        base=_cfg(config, "base"),
        command=_cfg(config, "command", "macroscope"),
        timeout=_cfg(config, "timeout", 600.0),
        guidance=_cfg(config, "guidance"),
    )


def _build_localstack(_cls: Any, config: Any, binding: BindingContext) -> Any:
    return _cls(
        endpoint_url=_cfg(config, "endpoint_url"),
        region=_cfg(config, "region"),
        allowed_services=list(_cfg(config, "allowed_services", ()) or ()),
        denied_services=list(_cfg(config, "denied_services", ()) or ()),
        manage_container=_cfg(config, "manage_container", False),
    )


# — harness: reasoning —


def _build_planning(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {
        "guidance": _cfg(config, "guidance"),
        "cache_ttl": _cfg(config, "cache_ttl", "5m"),
        "enable_subtasks": _cfg(config, "enable_subtasks", False),
        "inject": _cfg(config, "inject", True),
    }
    store = _cfg(config, "store", "run")
    if store == "resource":
        kwargs["store"] = _resource(
            binding,
            "plan.store",
            plugin_id="harness.planning",
            description="durable PlanStore surviving across runs",
        )
    tools = _as_list(_cfg(config, "tools"))
    if tools is not None:
        kwargs["tools"] = tools
    return _cls(**kwargs)


def _build_subagents(_cls: Any, config: Any, binding: BindingContext) -> Any:
    agents = _resource(
        binding,
        "subagents.agents",
        plugin_id="harness.subagents",
        description="a sequence of SubAgent entries with unique names and models",
    )
    return _cls(
        agents=agents,
        forward_usage=_cfg(config, "forward_usage", True),
        include_self=_cfg(config, "include_self", False),
        max_depth=_cfg(config, "max_depth", 3),
        tool_name=_cfg(config, "tool_name", "delegate_task"),
    )


def _build_dynamic_workflow(_cls: Any, config: Any, binding: BindingContext) -> Any:
    agents = _resource(
        binding,
        "dynamic.agents",
        plugin_id="harness.dynamic_workflow",
        description="named agents callable from the model-written workflow",
    )
    return _cls(
        agents=agents,
        tool_name=_cfg(config, "tool_name", "run_workflow"),
        max_agent_calls=_cfg(config, "max_agent_calls", 50),
        max_retries=_cfg(config, "max_retries", 3),
        forward_usage=_cfg(config, "forward_usage", True),
        inherit_model=_cfg(config, "inherit_model", False),
    )


def _build_advisor(_cls: Any, config: Any, binding: BindingContext) -> Any:
    model = _resource(
        binding,
        "model.advisor",
        plugin_id="harness.advisor",
        description="the advisor model (Model, provider string, or callback)",
    )
    return _cls(
        model,
        mode=_cfg(config, "mode", "auto"),
        max_uses=_cfg(config, "max_uses"),
        max_tokens=_cfg(config, "max_tokens"),
        caching=_cfg(config, "caching"),
        forward_history=_cfg(config, "forward_history", False),
    )


def _build_background_tools(_cls: Any, config: Any, binding: BindingContext) -> Any:
    tools = _cfg(config, "tools", "all")
    selector: Any = "all" if tools == "all" else list(tools)
    metadata = _cfg(config, "metadata")
    if metadata:
        selector = dict(metadata)
    return _cls(tools=selector)


# — context management —


def _build_tool_search(_cls: Any, config: Any, binding: BindingContext) -> Any:
    return _cls(
        max_results=_cfg(config, "max_results", 10),
        tool_description=_cfg(config, "tool_description"),
        parameter_description=_cfg(config, "parameter_description"),
    )


def _build_summarizing(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {}
    for field in (
        "model",
        "max_messages",
        "max_tokens",
        "max_fraction",
        "keep_messages",
        "keep_tokens",
        "incremental",
        "keep_user_messages",
        "instructions",
    ):
        value = _cfg(config, field)
        if value is not None:
            kwargs[field] = value
    return _cls(**kwargs)


def _build_tiered(_cls: Any, config: Any, binding: BindingContext) -> Any:
    tiers = _resource(
        binding,
        "compaction.tiers",
        plugin_id="harness.tiered_compaction",
        description="ordered CompactionStrategy instances (cheap to expensive)",
    )
    kwargs: dict[str, Any] = {"tiers": tiers}
    for field in ("target_tokens", "target_fraction", "context_window", "fallback_context_window"):
        value = _cfg(config, field)
        if value is not None:
            kwargs[field] = value
    return _cls(**kwargs)


def _build_fallback_compaction(_cls: Any, config: Any, binding: BindingContext) -> Any:
    chain = _resource(
        binding,
        "compaction.fallback_chain",
        plugin_id="harness.fallback_compaction",
        description="ordered compaction strategies tried only when one raises",
    )
    kwargs: dict[str, Any] = {"fallback_chain": chain}
    for field in ("max_tokens", "max_fraction"):
        value = _cfg(config, field)
        if value is not None:
            kwargs[field] = value
    return _cls(**kwargs)


def _build_deduplicate(_cls: Any, config: Any, binding: BindingContext) -> Any:
    file_key = _resource(
        binding,
        "callbacks.file_key",
        plugin_id="harness.deduplicate_reads",
        description="callable ToolCallPart -> str | None identifying superseded file reads",
    )
    return _cls(
        file_key=file_key,
        placeholder=_cfg(config, "placeholder", "[superseded file read]"),
        max_messages=_cfg(config, "max_messages"),
        max_tokens=_cfg(config, "max_tokens"),
    )


def _make_action(spec: BandAction, module: Any) -> Any:
    """Translate the serializable action tree into upstream ``Truncate``/``Spill`` objects."""
    if spec.kind == "passthrough":
        return module.Passthrough()
    if spec.kind == "spill":
        then = _make_action(spec.then, module) if spec.then is not None else None
        return module.Spill(preview_chars=spec.preview_chars, then=then)
    then = _make_action(spec.then, module) if spec.then is not None else None
    return module.Truncate(
        strategy=module.TruncationStrategy(spec.strategy),
        max_chars=spec.max_chars,
        then=then,
    )


def _build_tool_output_limits(_cls: Any, config: Any, binding: BindingContext) -> Any:
    import pydantic_ai_harness.tool_output_limits as limits_module  # noqa: PLC0415 - lazy

    bands_spec = _cfg(config, "bands", ()) or ()
    has_spill = any(action_has_spill(band.action) for band in bands_spec)
    store_mode = _cfg(config, "store", "workspace")
    kwargs: dict[str, Any] = {
        "bands": [
            limits_module.Band(over=band.over, action=_make_action(band.action, limits_module))
            for band in bands_spec
        ],
        "over_tokens": _cfg(config, "over_tokens", False),
        "strip_ansi": _cfg(config, "strip_ansi", False),
    }
    per_tool = _cfg(config, "per_tool", {}) or {}
    if per_tool:
        kwargs["per_tool"] = {
            name: [
                limits_module.Band(over=band.over, action=_make_action(band.action, limits_module))
                for band in bands
            ]
            for name, bands in per_tool.items()
        }
    tool_filter = _as_list(_cfg(config, "tool_filter"))
    if tool_filter is not None:
        kwargs["tool_filter"] = tool_filter
    if _cfg(config, "summary_prompt") is not None:
        kwargs["summary_prompt"] = _cfg(config, "summary_prompt")
    if store_mode == "resource":
        kwargs["store"] = _resource(
            binding,
            "tool_overflow.store",
            plugin_id="harness.tool_output_limits",
            description="overflow store for lossless spill",
        )
    elif store_mode == "workspace":
        if has_spill:
            _resource(
                binding,
                "workspace",
                plugin_id="harness.tool_output_limits",
                description="run workspace for spill files",
            )
        kwargs["store"] = None
    else:  # local_file: an explicit host choice, never a silent fallback
        kwargs["store"] = limits_module.LocalFileStore()
    return _cls(**kwargs)


def action_has_spill(action: BandAction | None) -> bool:
    """Whether a configured action tree can spill to an overflow store."""
    if action is None:
        return False
    if action.kind == "spill":
        return True
    return action_has_spill(action.then)


def _build_memory(_cls: Any, config: Any, binding: BindingContext) -> Any:
    store = _resource(
        binding,
        "memory.store",
        plugin_id="harness.memory",
        description="a MemoryStore (FileStore, SqliteMemoryStore, PostgresMemoryStore, ...)",
    )
    return _cls(
        store,
        agent_name=_cfg(config, "agent_name", "main"),
        namespace=_cfg(config, "namespace", ""),
        inject_memory=_cfg(config, "inject_memory", True),
        max_tokens=_cfg(config, "max_tokens", 2_000),
        max_lines=_cfg(config, "max_lines", 200),
        max_memory_size=_cfg(config, "max_memory_size", 65_536),
        max_search_results=_cfg(config, "max_search_results", 10),
        max_search_result_chars=_cfg(config, "max_search_result_chars", 4_000),
        max_search_files=_cfg(config, "max_search_files", 1_000),
        guidance=_cfg(config, "guidance"),
        injection_errors=_cfg(config, "injection_errors", "ignore"),
        heading=_cfg(config, "heading", ""),
    )


def _build_conversation_search(_cls: Any, config: Any, binding: BindingContext) -> Any:
    source = _resource(
        binding,
        "conversations",
        plugin_id="harness.conversation_search",
        description="a HistorySource over the StepPersistence store",
    )
    return _cls(
        source,
        scope=_cfg(config, "scope", "conversation"),
        max_matches=_cfg(config, "max_matches", 10),
        context_lines=_cfg(config, "context_lines", 5),
        bm25_k1=_cfg(config, "bm25_k1", 1.5),
        bm25_b=_cfg(config, "bm25_b", 0.75),
        add_instructions=_cfg(config, "add_instructions", True),
        tool_id=_cfg(config, "tool_id", "conversation-search"),
    )


def _build_skills(_cls: Any, config: Any, binding: BindingContext) -> Any:
    _resource(binding, "workspace", plugin_id="harness.skills", description="run workspace")
    directories = list(_cfg(config, "directories", (".agents/skills",)) or ())
    kwargs: dict[str, Any] = {
        "directories": directories if len(directories) > 1 else directories[0],
        "missing_directories": _cfg(config, "missing_directories", "error"),
        "duplicate_names": _cfg(config, "duplicate_names", "error"),
    }
    include = _as_list(_cfg(config, "include"))
    exclude = _as_list(_cfg(config, "exclude"))
    if include is not None:
        kwargs["include"] = include
    if exclude is not None:
        kwargs["exclude"] = exclude
    return _cls(**kwargs)


def _build_repo_context(_cls: Any, config: Any, binding: BindingContext) -> Any:
    _resource(binding, "workspace", plugin_id="harness.repo_context", description="run workspace")
    return _cls(
        home_dir=_cfg(config, "home_dir"),
        filenames=list(_cfg(config, "filenames", ("CLAUDE.md", "AGENTS.md")) or ()),
        autoload_instructions=_cfg(config, "autoload_instructions", True),
        expose_inventory_tool=_cfg(config, "expose_inventory_tool", False),
        inventory_tool_name=_cfg(config, "inventory_tool_name", "inventory_agent_context"),
        nested_traversal=_cfg(config, "nested_traversal", False),
        nested_inject=_cfg(config, "nested_inject", "pointer"),
    )


def _build_docs(_cls: Any, config: Any, binding: BindingContext) -> Any:
    local_path = _cfg(config, "local_docs_path")
    if local_path is not None:
        _resource(
            binding,
            "workspace",
            plugin_id="harness.pydantic_ai_docs",
            description="workspace containing local Pydantic AI docs",
        )
    return _cls(
        local_docs_path=Path(local_path) if local_path is not None else None,
        cache=_cfg(config, "cache", True),
    )


# — controls —


def _detector(spec: DetectorConfig, detectors_module: Any) -> Any:
    options: dict[str, Any] = {}
    if spec.only is not None:
        options["only"] = list(spec.only)
    if spec.extra is not None:
        options["extra"] = dict(spec.extra)
    if spec.kind == "blocked_keywords":
        return detectors_module.blocked_keywords(
            list(spec.keywords or ()),
            case_sensitive=spec.case_sensitive,
            whole_words=spec.whole_words,
            message=spec.message,
        )
    if spec.kind == "redact_secrets":
        # ``redact_secrets`` is a ready detector instance; ``secret_data()`` builds configured ones.
        if options:
            return detectors_module.secret_data(**options)
        return detectors_module.redact_secrets
    if options:
        return detectors_module.personal_data(**options)
    return detectors_module.redact_personal_data


def _detector_chain(config: Any, plugin_id: str) -> list[Any]:
    import pydantic_ai_harness.guardrails.detectors as detectors  # noqa: PLC0415 - lazy

    specs = _cfg(config, "detectors", ()) or ()
    return [_detector(spec, detectors) for spec in specs]


def _build_input_guardrail(_cls: Any, config: Any, binding: BindingContext) -> Any:
    return _cls(
        guard=_detector_chain(config, "harness.input_guardrail"),
        parallel=_cfg(config, "parallel", False),
    )


def _build_output_guardrail(_cls: Any, config: Any, binding: BindingContext) -> Any:
    import pydantic_ai_harness.guardrails.detectors as detectors  # noqa: PLC0415 - lazy

    chain = [
        detectors.for_text(detector)
        for detector in _detector_chain(config, "harness.output_guardrail")
    ]
    return _cls(guard=chain)


def _build_tool_guardrail(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {
        "tools": _as_list(_cfg(config, "tools")),
        "hidden": list(_cfg(config, "hidden", ()) or ()),
    }
    resources = getattr(binding, "resources", None)
    if resources is not None and "callbacks.tool_guardrail" in resources:
        kwargs["guard"] = _resource(
            binding,
            "callbacks.tool_guardrail",
            plugin_id="harness.tool_guardrail",
            description="ToolCallInfo -> GuardrailResult callable",
        )
    return _cls(**{key: value for key, value in kwargs.items() if value is not None})


def _build_injection_defender(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {
        "block_high_risk": _cfg(config, "block_high_risk"),
        "semantic_detection": _cfg(config, "semantic_detection", False),
        "blocked_message": _cfg(config, "blocked_message"),
    }
    resources = getattr(binding, "resources", None)
    if resources is not None and "defense" in resources:
        defense = _resource(binding, "defense", plugin_id="harness.prompt_injection_defender")
        return _cls(defense, **{key: value for key, value in kwargs.items() if value is not None})
    if kwargs["block_high_risk"] is None and not kwargs["semantic_detection"]:
        _resource(
            binding,
            "defense",
            plugin_id="harness.prompt_injection_defender",
            description="a configured stackone_defender PromptDefense",
        )
    tool_filter = _cfg(config, "tool_filter", "all")
    kwargs["tool_filter"] = list(tool_filter) if tool_filter != "all" else "all"
    if kwargs["block_high_risk"] is None:
        kwargs.pop("block_high_risk")
    if kwargs["semantic_detection"] is False:
        kwargs.pop("semantic_detection")
    if kwargs["blocked_message"] is None:
        kwargs.pop("blocked_message")
    return _cls(**kwargs)


def _build_tool_call_judge(_cls: Any, config: Any, binding: BindingContext) -> Any:
    model = _resource(
        binding,
        "model.judge",
        plugin_id="harness.tool_call_judge",
        description="the judge model (Model or provider string)",
    )
    kwargs: dict[str, Any] = {
        "question": _cfg(config, "question"),
        "include_conversation": _cfg(config, "include_conversation", False),
        "conversation_window": _cfg(config, "conversation_window", 4_000),
        "on_uncertain": _cfg(config, "on_uncertain", "block"),
        "denial_message": _cfg(config, "denial_message"),
    }
    tools = _as_list(_cfg(config, "tools"))
    if tools is not None:
        kwargs["tools"] = tools
    return _cls(model, **{key: value for key, value in kwargs.items() if value is not None})


def _build_approval_reviewer(_cls: Any, config: Any, binding: BindingContext) -> Any:
    model = _resource(
        binding,
        "model.judge",
        plugin_id="harness.approval_reviewer",
        description="the reviewer model (Model or provider string)",
    )
    kwargs: dict[str, Any] = {
        "question": _cfg(config, "question"),
        "include_conversation": _cfg(config, "include_conversation", False),
        "conversation_window": _cfg(config, "conversation_window", 4_000),
    }
    tools = _as_list(_cfg(config, "tools"))
    if tools is not None:
        kwargs["tools"] = tools
    return _cls(model, **kwargs)


def _build_spend_limits(_cls: Any, config: Any, binding: BindingContext) -> Any:
    import pydantic_ai_harness.spend as spend  # noqa: PLC0415 - lazy

    budgets = [
        spend.Budget(
            usd=budget.usd,
            tokens=budget.tokens,
            window=budget.window,
            warn_at=budget.warn_at,
            name=budget.name,
        )
        for budget in (_cfg(config, "budgets", ()) or ())
    ]
    kwargs: dict[str, Any] = {
        "budgets": budgets,
        "on_unpriced": _cfg(config, "on_unpriced", "zero"),
        "expose_tools": _cfg(config, "expose_tools", False),
    }
    if _cfg(config, "store", "in_memory") == "resource":
        kwargs["store"] = _resource(
            binding,
            "spend.store",
            plugin_id="harness.spend_limits",
            description="shared, restart-safe spend store",
        )
    return _cls(**kwargs)


def _build_ask_user(_cls: Any, config: Any, binding: BindingContext) -> Any:
    answerer = _resource(
        binding,
        "user_answers",
        plugin_id="harness.ask_user",
        description="async (AskUserRequest) -> AskUserResponse host answerer",
    )
    return _cls(answerer=answerer, timeout=_cfg(config, "timeout"))


def _build_system_reminders(_cls: Any, config: Any, binding: BindingContext) -> Any:
    import pydantic_ai_harness.system_reminders as reminders  # noqa: PLC0415 - lazy

    static = [
        reminders.Reminder(
            spec.content,
            interval=spec.interval,
            first_after=spec.first_after,
            max_fires=spec.max_fires,
            tag=spec.tag,
        )
        for spec in (_cfg(config, "reminders", ()) or ())
    ]
    dynamic = []
    resources = getattr(binding, "resources", None)
    if resources is not None and "callbacks.dynamic_reminders" in resources:
        dynamic = list(
            _resource(
                binding,
                "callbacks.dynamic_reminders",
                plugin_id="harness.system_reminders",
                description="sequence of dynamic reminder callables",
            )
        )
    if _cfg(config, "goal_reanchor", False):
        dynamic.append(reminders.GoalReanchor())
    return _cls(
        reminders=static, dynamic_reminders=dynamic, cache_ttl=_cfg(config, "cache_ttl", "5m")
    )


def _build_trajectory_judge(_cls: Any, config: Any, binding: BindingContext) -> Any:
    model = _resource(
        binding,
        "model.judge",
        plugin_id="harness.trajectory_judge",
        description="the judge model (Model or provider string)",
    )
    return _cls(
        model=model,
        every=_cfg(config, "every", 10),
        window=_cfg(config, "window", 20_000),
        name=_cfg(config, "name"),
        instructions=_cfg(config, "instructions"),
    )


def _build_capability_creation(_cls: Any, config: Any, binding: BindingContext) -> Any:
    _resource(
        binding,
        "workspace",
        plugin_id="harness.capability_creation",
        description="writable LocalWorkspace whose Python code the host admits",
    )
    return _cls(
        directory=Path(_cfg(config, "directory", ".authored")), guidance=_cfg(config, "guidance")
    )


# — services and durability —


def _build_step_persistence(_cls: Any, config: Any, binding: BindingContext) -> Any:
    store = _resource(
        binding,
        "step.store",
        plugin_id="harness.step_persistence",
        description="StepStore (FileStepStore, SqliteStepStore, PostgresStepStore, ...)",
    )
    kwargs: dict[str, Any] = {
        "store": store,
        "capture_frontier": _cfg(config, "capture_frontier", False),
        "metadata": dict(_cfg(config, "metadata", {}) or {}),
    }
    if _cfg(config, "agent_name") is not None:
        kwargs["agent_name"] = _cfg(config, "agent_name")
    return _cls(**kwargs)


def _build_instrumentation(_cls: Any, config: Any, binding: BindingContext) -> Any:
    settings_cls = _load(
        "pydantic.instrumentation",
        "pydantic_ai:InstrumentationSettings",
        None,
    )
    settings = settings_cls(
        include_content=_cfg(config, "include_content", True),
        include_binary_content=_cfg(config, "include_binary_content", True),
        include_model_request_parameters=_cfg(config, "include_model_request_parameters", True),
        version=_cfg(config, "version", 5),
        use_aggregated_usage_attribute_names=_cfg(
            config, "use_aggregated_usage_attribute_names", True
        ),
    )
    return _cls(settings=settings)


def _build_managed_prompt(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {
        "default": _cfg(config, "default"),
        "label": _cfg(config, "label", "production"),
        "render_template": _cfg(config, "render_template", False),
    }
    return _cls(_cfg(config, "name"), **kwargs)


def _build_durability(_cls: Any, config: Any, binding: BindingContext) -> Any:
    kwargs: dict[str, Any] = {}
    if _cfg(config, "name") is not None:
        kwargs["name"] = _cfg(config, "name")
    if _cfg(config, "parallel_execution_mode") is not None:
        kwargs["parallel_execution_mode"] = _cfg(config, "parallel_execution_mode")
    return _cls(**kwargs)


def _build_render(_cls: Any, config: Any, binding: BindingContext) -> Any:
    app = _resource(
        binding,
        "render.app",
        plugin_id="harness.render_workflows",
        description="the live Render Workflows app object",
    )
    return _cls(app)


def _hosted_factory(
    plugin_id: str,
    import_ref: str,
    install_hint: str,
    *,
    config_model: type[BaseModel],
    extra_fields: Mapping[str, str] | None = None,
) -> Callable[[Any, BindingContext], Any]:
    """Factory for the shared hosted-MCP integration pattern (auth comes from host env)."""

    def factory(config: Any, binding: BindingContext) -> Any:
        cls = _load(plugin_id, import_ref, install_hint)
        kwargs: dict[str, Any] = {
            "include_instructions": _cfg(config, "include_instructions", True)
        }
        if _present(config, "read_only"):
            kwargs["read_only"] = _cfg(config, "read_only", False)
        identifier = _cfg(config, "id")
        if identifier is not None:
            kwargs["id"] = identifier
        for config_field, parameter in (extra_fields or {}).items():
            value = _cfg(config, config_field)
            if value is not None:
                kwargs[parameter] = list(value) if isinstance(value, tuple) else value
        return cls(**kwargs)

    factory.__name__ = f"build_{plugin_id.replace('.', '_')}"
    return factory


# — media store resource factories —


def _media_disk(cls: Any, config: Any, binding: BindingContext) -> Any:
    return cls(directory=_cfg(config, "directory"))


def _media_sqlite(cls: Any, config: Any, binding: BindingContext) -> Any:
    return cls(database=_cfg(config, "database"))


def _media_s3(cls: Any, config: Any, binding: BindingContext) -> Any:
    credentials = _resource(
        binding,
        "media.s3_credentials",
        plugin_id="harness.media.s3",
        description="mapping with access_key_id and secret_access_key",
    )
    return cls(
        bucket=_cfg(config, "bucket"),
        endpoint=_cfg(config, "endpoint"),
        region=_cfg(config, "region"),
        key_prefix=_cfg(config, "key_prefix", ""),
        access_key_id=credentials["access_key_id"],
        secret_access_key=credentials["secret_access_key"],
    )


def _media_postgres(cls: Any, config: Any, binding: BindingContext) -> Any:
    pool = _resource(
        binding,
        "media.postgres_pool",
        plugin_id="harness.media.postgres",
        description="an asyncpg-compatible Postgres pool",
    )
    return cls(pool)


# ---------------------------------------------------------------------------
# Catalog entries
# ---------------------------------------------------------------------------

CORE = "pydantic-ai-slim"
HARNESS = "pydantic-ai-harness"
CORE_VERSION = PLUGIN_VERSION[CORE]
HARNESS_VERSION = PLUGIN_VERSION[HARNESS]


def _entry(**kwargs: Any) -> CatalogEntry:
    integration = kwargs.pop("integration")
    version = kwargs.pop("version", None) or (
        CORE_VERSION if integration == CORE else HARNESS_VERSION
    )
    return CatalogEntry(integration=integration, version=version, **kwargs)


CATALOG: tuple[CatalogEntry, ...] = (
    # -- complete compositions --------------------------------------------
    _entry(
        plugin_id="harness.coder",
        name="Coder",
        description="Combined coding capability: file tools, shell, repo context and delegation.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.coder:Coder",
        group=CatalogGroup.COMPOSITIONS,
        tier=CatalogTier.VERIFIED,
        config_model=CoderConfig,
        factory=_call_with("harness.coder", "pydantic_ai_harness.coder:Coder", None, _build_coder),
        extras=(_install(HARNESS, "coder"),),
        deployment_resources=("workspace",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.files", "tool.shell", "activity.plan"),
        notes=(
            "Requires an explicit workspace binding; no host fallback.",
            "``sub_agents=True`` needs a Python-factory definition that binds managed_capabilities "
            "at agent construction (the upstream self-delegation constraint).",
            "The [coder] extra installs ripgrep; without it file search is slower.",
        ),
        starter_roles=("developer",),
    ),
    _entry(
        plugin_id="harness.researcher",
        name="Researcher",
        description="Combined research stack: search, fetch, delegation and output limits.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.researcher:Researcher",
        group=CatalogGroup.COMPOSITIONS,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=ResearcherConfig,
        factory=_call_with(
            "harness.researcher",
            "pydantic_ai_harness.researcher:Researcher",
            None,
            _build_researcher,
        ),
        extras=(_install(HARNESS, "researcher"),),
        requires_modules=("ddgs", "markdownify"),
        deployment_resources=("workspace",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.web", "activity.delegation"),
        notes=(
            "Runtime search uses pydantic-ai-slim[duckduckgo,web-fetch]; installing the "
            "researcher extra supplies them.",
            "Spills oversized tool output into the run workspace; bind one or use components.",
        ),
        starter_roles=("research",),
    ),
    # -- files and execution ----------------------------------------------
    _entry(
        plugin_id="pydantic.local_workspace",
        name="Local workspace",
        description="Run file and shell tools in a bound local working directory (no isolation).",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:LocalWorkspace",
        group=CatalogGroup.FILES,
        tier=CatalogTier.VERIFIED,
        config_model=LocalWorkspaceConfig,
        factory=_call_with(
            "pydantic.local_workspace",
            "pydantic_ai.capabilities:LocalWorkspace",
            None,
            _build_local_workspace,
        ),
        deployment_resources=("workspace",),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.files", "tool.shell"),
        notes=(
            "POSIX hosts only; isolates nothing.",
            "The working directory comes from the bound 'workspace' resource, never from the "
            "client's cwd.",
        ),
        starter_roles=("developer",),
    ),
    _entry(
        plugin_id="harness.filesystem",
        name="File system tools",
        description="Read, write, edit, list, search and inspect files in the run workspace.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.filesystem:FileSystem",
        group=CatalogGroup.FILES,
        tier=CatalogTier.VERIFIED,
        config_model=FileSystemConfig,
        factory=_call_with(
            "harness.filesystem",
            "pydantic_ai_harness.filesystem:FileSystem",
            None,
            _build_filesystem,
        ),
        deployment_resources=("workspace",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.files", "diff"),
        notes=("root_dir limits only file tools; Shell is not confined by it.",),
        starter_roles=("developer",),
    ),
    _entry(
        plugin_id="harness.shell",
        name="Shell commands",
        description="Run, start, check and stop commands in the run workspace.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.shell:Shell",
        group=CatalogGroup.FILES,
        tier=CatalogTier.VERIFIED,
        config_model=ShellConfig,
        factory=_call_with("harness.shell", "pydantic_ai_harness.shell:Shell", None, _build_shell),
        deployment_resources=("workspace",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.shell", "command.output"),
        notes=("Allow/deny lists are guardrails against accidents, not isolation.",),
        starter_roles=("developer",),
    ),
    _entry(
        plugin_id="pydantic.ssh_workspace",
        name="SSH workspace",
        description="Run commands and file operations on a remote host over ssh.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.ssh_workspace:SSHWorkspace",
        group=CatalogGroup.FILES,
        tier=CatalogTier.VERIFIED,
        config_model=SSHWorkspaceConfig,
        factory=_call_with(
            "pydantic.ssh_workspace",
            "pydantic_ai_harness.ssh_workspace:SSHWorkspace",
            None,
            _build_ssh,
        ),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.shell", "tool.files"),
        notes=("Key auth only (BatchMode); the remote host needs a POSIX sh. Not isolation.",),
    ),
    _entry(
        plugin_id="pydantic.sandbox_modal",
        name="Modal sandbox",
        description="Cloud workspace sandbox on Modal.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.modal_sandbox:ModalSandbox",
        group=CatalogGroup.FILES,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=SandboxConfig,
        factory=_call_with(
            "pydantic.sandbox_modal",
            "pydantic_ai_harness.modal_sandbox:ModalSandbox",
            _install(HARNESS, "modal"),
            _build_sandbox_fields,
        ),
        extras=(_install(HARNESS, "modal"),),
        requires_modules=("modal",),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.shell", "tool.files"),
        notes=(
            "Sandboxes bill until explicitly destroyed; persist the upstream WorkspaceRef on the host.",
            "Constructor arguments reviewed in the 0.57.0 upstream documentation; the extra was "
            "not installed in the verification environment.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="pydantic.sandbox_e2b",
        name="E2B sandbox",
        description="Cloud workspace sandbox on E2B.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.e2b_sandbox:E2BSandbox",
        group=CatalogGroup.FILES,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=SandboxConfig,
        factory=_call_with(
            "pydantic.sandbox_e2b",
            "pydantic_ai_harness.e2b_sandbox:E2BSandbox",
            _install(HARNESS, "e2b"),
            _build_sandbox_fields,
        ),
        extras=(_install(HARNESS, "e2b"),),
        requires_modules=("e2b",),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.shell", "tool.files"),
        notes=(
            "Sandboxes bill until explicitly destroyed.",
            "Constructor arguments reviewed in the 0.57.0 upstream documentation; not "
            "instantiated offline.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="pydantic.sandbox_sprites",
        name="Sprites sandbox",
        description="Cloud workspace sandbox on Sprites.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.sprites_sandbox:SpritesSandbox",
        group=CatalogGroup.FILES,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=SandboxConfig,
        factory=_call_with(
            "pydantic.sandbox_sprites",
            "pydantic_ai_harness.sprites_sandbox:SpritesSandbox",
            _install(HARNESS, "sprites"),
            _build_sandbox_fields,
        ),
        extras=(_install(HARNESS, "sprites"),),
        requires_modules=("sprites",),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.shell", "tool.files"),
        notes=(
            "Sandboxes bill until explicitly destroyed.",
            "Constructor arguments reviewed in the 0.57.0 upstream documentation; not "
            "instantiated offline.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="pydantic.sandbox_smol",
        name="Smol sandbox",
        description="Local smolvm microVM workspace (Apple-silicon macOS or Linux/KVM).",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.smol_sandbox:SmolSandbox",
        group=CatalogGroup.FILES,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=SmolSandboxConfig,
        factory=_call_with(
            "pydantic.sandbox_smol",
            "pydantic_ai_harness.smol_sandbox:SmolSandbox",
            _install(HARNESS, "smol"),
            _build_smol,
        ),
        extras=(_install(HARNESS, "smol"),),
        requires_modules=("smolmachines",),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.shell", "tool.files"),
        notes=(
            "Needs macOS on Apple silicon, or Linux x86_64/aarch64 with /dev/kvm.",
            "VMs have no TTL; destroy explicitly. Verified signature from the installed docs.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.sandbox_bubblewrap",
        name="Bubblewrap sandbox",
        description="Wrap a workspace capability's commands in a Linux bwrap sandbox.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.bubblewrap_sandbox:BubblewrapSandbox",
        group=CatalogGroup.FILES,
        tier=CatalogTier.VERIFIED,
        config_model=BubblewrapConfig,
        factory=_call_with(
            "harness.sandbox_bubblewrap",
            "pydantic_ai_harness.bubblewrap_sandbox:BubblewrapSandbox",
            None,
            _build_bubblewrap,
        ),
        deployment_resources=("wrapped.workspace",),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.shell",),
        notes=(
            "Requires the bwrap executable and user namespaces on the target host.",
            "Binds a wrapped workspace capability through the 'wrapped.workspace' resource.",
            "Not a hostile-agent boundary; see upstream caveats.",
        ),
    ),
    # -- tools and media ---------------------------------------------------
    _entry(
        plugin_id="pydantic.mcp",
        name="MCP (local transport)",
        description="Connect an MCP server over a local client transport derived from a URL.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:MCP",
        group=CatalogGroup.TOOLS_MEDIA,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=MCPConfig,
        factory=_call_with(
            "pydantic.mcp", "pydantic_ai.capabilities:MCP", _install(CORE, "mcp"), _build_mcp
        ),
        extras=(_install(CORE, "mcp"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=(
            "Local MCP transports need pydantic-ai-slim[mcp]; native provider MCP does not.",
            "Hosted HTTP MCP endpoints must be permitted by deployment policy.",
        ),
    ),
    _entry(
        plugin_id="pydantic.mcp_native",
        name="MCP (provider-native)",
        description="Use the model provider's native MCP server tools (no local transport).",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:MCP",
        group=CatalogGroup.TOOLS_MEDIA,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=MCPNativeConfig,
        factory=_call_with(
            "pydantic.mcp_native",
            "pydantic_ai.capabilities:MCP",
            _install(CORE, "mcp"),
            _build_mcp_native,
        ),
        extras=(_install(CORE, "mcp"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=(
            "Provider-native MCP still needs pydantic-ai-slim[mcp] in 2.57.0 (the URL is parsed by "
            "core MCP support), and a provider whose model supports native MCP tools.",
        ),
    ),
    _entry(
        plugin_id="pydantic.image_generation",
        name="Image generation",
        description="Native or local image generation/editing capability.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:ImageGeneration",
        group=CatalogGroup.TOOLS_MEDIA,
        tier=CatalogTier.VERIFIED,
        config_model=ImageGenerationConfig,
        factory=_call_with(
            "pydantic.image_generation",
            "pydantic_ai.capabilities:ImageGeneration",
            None,
            _build_image_generation,
        ),
        deployment_resources=(),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.image",),
        notes=(
            "native=True needs provider support; local=True requires an image.generator resource.",
            "Fallback subagent models are Python-factory concerns and not exposed here.",
        ),
    ),
    _entry(
        plugin_id="harness.localstack",
        name="LocalStack",
        description="AWS CLI against an emulated LocalStack endpoint.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.localstack:LocalStack",
        group=CatalogGroup.TOOLS_MEDIA,
        tier=CatalogTier.VERIFIED,
        config_model=LocalStackConfig,
        factory=_call_with(
            "harness.localstack",
            "pydantic_ai_harness.localstack:LocalStack",
            None,
            _build_localstack,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.shell",),
        notes=(
            "Runs aws/docker on the agent host, not in the run workspace.",
            "manage_container needs Docker; the default image needs LOCALSTACK_AUTH_TOKEN.",
        ),
    ),
    _entry(
        plugin_id="harness.macroscope",
        name="Macroscope review",
        description="Run the Macroscope code-review CLI inside the run workspace.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.macroscope:Macroscope",
        group=CatalogGroup.TOOLS_MEDIA,
        tier=CatalogTier.VERIFIED,
        config_model=MacroscopeConfig,
        factory=_call_with(
            "harness.macroscope",
            "pydantic_ai_harness.macroscope:Macroscope",
            None,
            _build_macroscope,
        ),
        deployment_resources=("workspace",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.shell",),
        notes=("The macroscope CLI must be installed and signed in inside the workspace.",),
    ),
    # -- business integrations --------------------------------------------
    _entry(
        plugin_id="harness.github",
        name="GitHub",
        description="GitHub MCP integration (issues, pull requests, repositories).",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.github:GitHub",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=GitHubConfig,
        factory=_hosted_factory(
            "harness.github",
            "pydantic_ai_harness.github:GitHub",
            _install(HARNESS, "github"),
            config_model=GitHubConfig,
            extra_fields={"toolsets": "toolsets", "url": "url"},
        ),
        extras=(_install(HARNESS, "github"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=(
            "Credential: GITHUB_TOKEN in the worker environment (PAT or GitHub App user token).",
            "Server-side read_only removes write tools. Not an access boundary; token scopes are.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.linear",
        name="Linear",
        description="Linear issue tracking over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.linear:Linear",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=HostedIntegrationConfig,
        factory=_hosted_factory(
            "harness.linear",
            "pydantic_ai_harness.linear:Linear",
            _install(HARNESS, "linear"),
            config_model=HostedIntegrationConfig,
        ),
        extras=(_install(HARNESS, "linear"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: LINEAR_ACCESS_TOKEN (API key or OAuth token).",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.notion",
        name="Notion",
        description="Notion pages and databases over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.notion:Notion",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=HostedIntegrationConfig,
        factory=_hosted_factory(
            "harness.notion",
            "pydantic_ai_harness.notion:Notion",
            _install(HARNESS, "notion"),
            config_model=HostedIntegrationConfig,
        ),
        extras=(_install(HARNESS, "notion"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: NOTION_ACCESS_TOKEN (OAuth token only).",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.slack",
        name="Slack",
        description="Slack conversations over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.slack:Slack",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=HostedIntegrationConfig,
        factory=_hosted_factory(
            "harness.slack",
            "pydantic_ai_harness.slack:Slack",
            _install(HARNESS, "slack"),
            config_model=HostedIntegrationConfig,
        ),
        extras=(_install(HARNESS, "slack"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: SLACK_USER_TOKEN (xoxp- user token, not xoxb-).",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.google_workspace",
        name="Google Workspace",
        description="Google Workspace services over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.google_workspace:GoogleWorkspace",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=GoogleWorkspaceConfig,
        factory=_hosted_factory(
            "harness.google_workspace",
            "pydantic_ai_harness.google_workspace:GoogleWorkspace",
            _install(HARNESS, "google-workspace"),
            config_model=GoogleWorkspaceConfig,
            extra_fields={"services": "services"},
        ),
        extras=(_install(HARNESS, "google-workspace"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: GOOGLE_ACCESS_TOKEN (OAuth access token). 'services' is required.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.logfire_mcp",
        name="Logfire MCP",
        description="Logfire observability queries over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.logfire_mcp:LogfireMCP",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=LogfireMCPConfig,
        factory=_hosted_factory(
            "harness.logfire_mcp",
            "pydantic_ai_harness.logfire_mcp:LogfireMCP",
            _install(HARNESS, "logfire-mcp"),
            config_model=LogfireMCPConfig,
            extra_fields={"url": "url"},
        ),
        extras=(_install(HARNESS, "logfire-mcp"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: LOGFIRE_API_KEY. url selects US/EU/self-hosted.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.ordinal",
        name="Ordinal",
        description="Ordinal over MCP (no read-only mode upstream).",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.ordinal:Ordinal",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=HostedIntegrationConfig,
        factory=_hosted_factory(
            "harness.ordinal",
            "pydantic_ai_harness.ordinal:Ordinal",
            _install(HARNESS, "ordinal"),
            config_model=HostedIntegrationConfig,
        ),
        extras=(_install(HARNESS, "ordinal"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: ORDINAL_ACCESS_TOKEN. Upstream exposes no read_only switch.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.grain",
        name="Grain",
        description="Grain meeting notes over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.grain:Grain",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=HostedIntegrationConfig,
        factory=_hosted_factory(
            "harness.grain",
            "pydantic_ai_harness.grain:Grain",
            _install(HARNESS, "grain"),
            config_model=HostedIntegrationConfig,
        ),
        extras=(_install(HARNESS, "grain"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: GRAIN_ACCESS_TOKEN.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.day_ai",
        name="Day AI",
        description="Day AI over MCP (no read-only mode upstream).",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.day_ai:DayAI",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=HostedIntegrationConfig,
        factory=_hosted_factory(
            "harness.day_ai",
            "pydantic_ai_harness.day_ai:DayAI",
            _install(HARNESS, "day-ai"),
            config_model=HostedIntegrationConfig,
        ),
        extras=(_install(HARNESS, "day-ai"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: DAY_AI_ACCESS_TOKEN. Upstream exposes no read_only switch.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.posthog",
        name="PostHog",
        description="PostHog analytics over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.posthog:PostHog",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=PostHogConfig,
        factory=_hosted_factory(
            "harness.posthog",
            "pydantic_ai_harness.posthog:PostHog",
            _install(HARNESS, "posthog"),
            config_model=PostHogConfig,
            extra_fields={"features": "features"},
        ),
        extras=(_install(HARNESS, "posthog"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: POSTHOG_PERSONAL_API_KEY.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.pylon",
        name="Pylon",
        description="Pylon customer support over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.pylon:Pylon",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=HostedIntegrationConfig,
        factory=_hosted_factory(
            "harness.pylon",
            "pydantic_ai_harness.pylon:Pylon",
            _install(HARNESS, "pylon"),
            config_model=HostedIntegrationConfig,
        ),
        extras=(_install(HARNESS, "pylon"),),
        requires_modules=("fastmcp",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=("Credential: PYLON_ACCESS_TOKEN (OAuth only).",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.stackone",
        name="StackOne",
        description="StackOne unified SaaS actions over MCP.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.stackone:StackOne",
        group=CatalogGroup.HOSTED,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=StackOneConfig,
        factory=_call_with(
            "harness.stackone",
            "pydantic_ai_harness.stackone:StackOne",
            _install(HARNESS, "stackone"),
            lambda cls, config, binding: cls(
                account_id=_cfg(config, "account_id"),
                actions=_as_list(_cfg(config, "actions")),
            ),
        ),
        extras=(_install(HARNESS, "stackone"),),
        requires_modules=("fastmcp", "stackone_defender"),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.mcp",),
        notes=(
            "Credential: STACKONE_API_KEY. account_id is required; actions narrow the surface.",
        ),
        signature_verified=False,
    ),
    # -- web and research ---------------------------------------------------
    _entry(
        plugin_id="pydantic.web_search",
        name="Web search",
        description="Provider-native web search with an optional DuckDuckGo fallback.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:WebSearch",
        group=CatalogGroup.WEB,
        tier=CatalogTier.VERIFIED,
        config_model=WebSearchConfig,
        factory=_call_with(
            "pydantic.web_search", "pydantic_ai.capabilities:WebSearch", None, _build_web_search
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.web",),
        notes=(
            "native=True requires a provider that supports web search.",
            "local=True needs pydantic-ai-slim[duckduckgo].",
            "Tool name 'web_search' collides with Exa/You.com search; use one search capability or PrefixTools.",
        ),
        starter_roles=("research",),
    ),
    _entry(
        plugin_id="pydantic.web_fetch",
        name="Web fetch",
        description="Provider-native URL fetching with an optional local fallback.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:WebFetch",
        group=CatalogGroup.WEB,
        tier=CatalogTier.VERIFIED,
        config_model=WebFetchConfig,
        factory=_call_with(
            "pydantic.web_fetch", "pydantic_ai.capabilities:WebFetch", None, _build_web_fetch
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.web",),
        notes=(
            "native=True requires provider support.",
            "local=True needs pydantic-ai-slim[web-fetch].",
        ),
        starter_roles=("research",),
    ),
    _entry(
        plugin_id="pydantic.x_search",
        name="X search",
        description="Search X posts through provider-native search or a local fallback.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:XSearch",
        group=CatalogGroup.WEB,
        tier=CatalogTier.VERIFIED,
        config_model=XSearchConfig,
        factory=_call_with(
            "pydantic.x_search", "pydantic_ai.capabilities:XSearch", None, _build_x_search
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.web",),
        notes=(
            "Native X search needs provider support; local fallback requires a configured model.",
        ),
    ),
    _entry(
        plugin_id="harness.exa_search",
        name="Exa search",
        description="Exa web search and page extraction.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.exa:ExaSearch",
        group=CatalogGroup.WEB,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=None,
        factory=None,
        extras=(_install(HARNESS, "exa"),),
        requires_modules=("exa_py",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.web",),
        notes=(
            "Needs [exa] and EXA_API_KEY (or a Python-injected client).",
            "Config fields are documented upstream but not exposed as a managed model yet.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.exa_agent",
        name="Exa agent",
        description="Run an Exa Agent research task as a deferred tool call.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.exa:ExaAgent",
        group=CatalogGroup.WEB,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=None,
        factory=None,
        extras=(_install(HARNESS, "exa"),),
        requires_modules=("exa_py",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.web",),
        notes=(
            "Needs [exa] and EXA_API_KEY.",
            "execution='external' additionally needs the agent output type [str, DeferredToolRequests].",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.you_search",
        name="You.com search",
        description="You.com web search and page extraction.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.youdotcom:YouSearch",
        group=CatalogGroup.WEB,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=None,
        factory=None,
        extras=(_install(HARNESS, "youdotcom"),),
        requires_modules=("youdotcom",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.web",),
        notes=("Needs [youdotcom] and YDC_API_KEY. Config fields are not exposed as a model yet.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.you_research",
        name="You.com research",
        description="You.com one-call answers, multi-step research and finance research.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.youdotcom:YouResearch",
        group=CatalogGroup.WEB,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=None,
        factory=None,
        extras=(_install(HARNESS, "youdotcom"),),
        requires_modules=("youdotcom",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("tool.web",),
        notes=("Needs [youdotcom] and YDC_API_KEY. Config fields are not exposed as a model yet.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.browser_use",
        name="Browser Use",
        description="Hand a goal to a browser-use agent driving real Chromium.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.browser_use:BrowserUse",
        group=CatalogGroup.WEB,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=BrowserUseConfig,
        factory=_call_with(
            "harness.browser_use",
            "pydantic_ai_harness.browser_use:BrowserUse",
            _install(HARNESS, "browser-use"),
            _build_browser_use,
        ),
        extras=(_install(HARNESS, "browser-use"),),
        requires_modules=("browser_use",),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.browser",),
        notes=(
            "Needs [browser-use]; Python 3.11+.",
            "The browser sub-agent settings here follow the 0.57.0 documentation; the extra was "
            "not installed during catalog verification.",
        ),
        signature_verified=False,
        starter_roles=("research",),
    ),
    _entry(
        plugin_id="harness.playwright_browser",
        name="Playwright browser",
        description="Let the model drive Chromium through Playwright.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.playwright:PlaywrightBrowser",
        group=CatalogGroup.WEB,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=None,
        factory=None,
        extras=(_install(HARNESS, "playwright"),),
        requires_modules=("playwright",),
        change_mode="new_session",
        state_mode="external",
        presentation=("tool.browser",),
        notes=(
            "Needs [playwright] and `playwright install chromium`.",
            "A live browser cannot survive durable replay; upstream refuses durability together.",
        ),
        signature_verified=False,
    ),
    # -- reasoning and delegation ------------------------------------------
    _entry(
        plugin_id="harness.planning",
        name="Planning",
        description="A model-owned task plan shown as a cache-stable tail reminder.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.planning:Planning",
        group=CatalogGroup.REASONING,
        tier=CatalogTier.VERIFIED,
        config_model=PlanningConfig,
        factory=_call_with(
            "harness.planning", "pydantic_ai_harness.planning:Planning", None, _build_planning
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.plan",),
        notes=(
            "Default store is fresh per run; store='resource' binds a durable plan.store.",
            "Plans persist separately from conversation history.",
        ),
        starter_roles=("developer", "research"),
    ),
    _entry(
        plugin_id="harness.subagents",
        name="Sub-agents",
        description="One delegate_task tool over named child agents.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.subagents:SubAgents",
        group=CatalogGroup.REASONING,
        tier=CatalogTier.VERIFIED,
        config_model=SubAgentsConfig,
        factory=_call_with(
            "harness.subagents", "pydantic_ai_harness.subagents:SubAgents", None, _build_subagents
        ),
        deployment_resources=("subagents.agents",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.delegation",),
        notes=(
            "Child agents are live Python objects; bind them through 'subagents.agents'.",
            "include_self=True requires binding at Agent construction (Python factory with managed_capabilities).",
        ),
        starter_roles=("developer", "research"),
    ),
    _entry(
        plugin_id="harness.dynamic_workflow",
        name="Dynamic workflow",
        description="The model writes a Python workflow that fans out to sub-agents in a sandbox.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.dynamic_workflow:DynamicWorkflow",
        group=CatalogGroup.REASONING,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=DynamicWorkflowConfig,
        factory=_call_with(
            "harness.dynamic_workflow",
            "pydantic_ai_harness.dynamic_workflow:DynamicWorkflow",
            _install(HARNESS, "dynamic-workflow"),
            _build_dynamic_workflow,
        ),
        extras=(_install(HARNESS, "dynamic-workflow"),),
        requires_modules=("pydantic_monty",),
        deployment_resources=("dynamic.agents",),
        change_mode="new_session",
        state_mode="run",
        presentation=("activity.delegation",),
        notes=(
            "Agents are live objects bound through 'dynamic.agents'.",
            "Workflows do not nest; children need their own workspaces.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.advisor",
        name="Advisor",
        description="Consult a second model mid-run (native provider tool or local function tool).",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.advisor:Advisor",
        group=CatalogGroup.REASONING,
        tier=CatalogTier.VERIFIED,
        config_model=AdvisorConfig,
        factory=_call_with(
            "harness.advisor", "pydantic_ai_harness.advisor:Advisor", None, _build_advisor
        ),
        deployment_resources=("model.advisor",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.delegation",),
        notes=(
            "Native mode requires Anthropic/OpenRouter string models shared with the executor.",
            "Typed advisor output is Python-only (output_type is not serialized).",
        ),
    ),
    _entry(
        plugin_id="harness.background_tools",
        name="Background tools",
        description="Run selected slow tools in the background and deliver results later.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.background_tools:BackgroundTools",
        group=CatalogGroup.REASONING,
        tier=CatalogTier.VERIFIED,
        config_model=BackgroundToolsConfig,
        factory=_call_with(
            "harness.background_tools",
            "pydantic_ai_harness.background_tools:BackgroundTools",
            None,
            _build_background_tools,
        ),
        change_mode="next_turn",
        state_mode="session",
        presentation=("activity.background",),
        notes=("Follow-up completions are delivered as user-role reports, marked untrusted.",),
    ),
    # -- context management ------------------------------------------------
    _entry(
        plugin_id="harness.code_mode",
        name="Code Mode",
        description="Expose tools behind one run_code tool executed in a Monty sandbox.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.code_mode:CodeMode",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=None,
        factory=None,
        extras=(_install(HARNESS, "codemode"),),
        requires_modules=("pydantic_monty",),
        change_mode="new_session",
        state_mode="run",
        presentation=("tool.code",),
        notes=(
            "Needs pydantic-monty; Monty runs a Python subset with no third-party imports.",
            "Provider-native tools bypass Code Mode entirely.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="pydantic.tool_search",
        name="Tool search",
        description="Let the model discover tools on demand instead of loading every definition.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:ToolSearch",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=ToolSearchConfig,
        factory=_call_with(
            "pydantic.tool_search", "pydantic_ai.capabilities:ToolSearch", None, _build_tool_search
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.tool_search",),
        notes=(
            "Custom local/native ToolSearch strategies hold callables and stay Python-only.",
            "A disabled capability cannot be revived by model-driven deferred loading.",
        ),
    ),
    _entry(
        plugin_id="pydantic.caching",
        name="Prompt caching",
        description="Provider prompt-cache retention settings and cache points.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:Caching",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=CachingConfig,
        factory=_constructor(
            "pydantic.caching",
            "pydantic_ai.capabilities:Caching",
            None,
            fields={"retention": "retention", "messages": "messages"},
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.usage",),
        notes=("retention may be True (provider default), '5m' or '1h'.",),
    ),
    _entry(
        plugin_id="pydantic.openai_compaction",
        name="OpenAI native compaction",
        description="OpenAI Responses server-side compaction capability.",
        integration=CORE,
        upstream_import="pydantic_ai.models.openai:OpenAICompaction",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=None,
        factory=None,
        extras=(_install(CORE, "openai"),),
        requires_modules=("openai",),
        change_mode="new_session",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=(
            "Only works on OpenAI Responses API models.",
            "Constructor variants (token_threshold / message_count_threshold) are not managed here yet.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="pydantic.anthropic_compaction",
        name="Anthropic native compaction",
        description="Anthropic server-side compaction capability.",
        integration=CORE,
        upstream_import="pydantic_ai.models.anthropic:AnthropicCompaction",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=None,
        factory=None,
        extras=(_install(CORE, "anthropic"),),
        requires_modules=("anthropic",),
        change_mode="new_session",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=("Anthropic only; constructor arguments are not managed here yet.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.clear_tool_results",
        name="Clear tool results",
        description="Blank old tool returns while preserving tool-call/return pairs.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:ClearToolResults",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=ClearToolResultsConfig,
        factory=_constructor(
            "harness.clear_tool_results",
            "pydantic_ai_harness.compaction:ClearToolResults",
            None,
            fields={
                "max_messages": "max_messages",
                "max_tokens": "max_tokens",
                "max_fraction": "max_fraction",
                "keep_pairs": "keep_pairs",
                "placeholder": "placeholder",
                "clear_tool_inputs": "clear_tool_inputs",
                "min_clear_tokens": "min_clear_tokens",
            },
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=("Zero-LLM; every rewrite busts the provider prompt cache from the edit point.",),
        starter_roles=("developer", "research"),
    ),
    _entry(
        plugin_id="harness.sliding_window",
        name="Sliding window compaction",
        description="Drop the oldest whole messages (no model call).",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:SlidingWindowCompaction",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=SlidingWindowConfig,
        factory=_constructor(
            "harness.sliding_window",
            "pydantic_ai_harness.compaction:SlidingWindowCompaction",
            None,
            fields={
                "max_messages": "max_messages",
                "max_tokens": "max_tokens",
                "max_fraction": "max_fraction",
                "keep_messages": "keep_messages",
                "keep_tokens": "keep_tokens",
                "preserve_first_user_message": "preserve_first_user_message",
                "receipts": "receipts",
            },
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=(
            "max_fraction resolves against the model context window; TestModel cannot resolve one.",
        ),
    ),
    _entry(
        plugin_id="harness.summarizing_compaction",
        name="Summarizing compaction",
        description="Replace old history with a model-written summary.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:SummarizingCompaction",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=SummarizingConfig,
        factory=_call_with(
            "harness.summarizing_compaction",
            "pydantic_ai_harness.compaction:SummarizingCompaction",
            None,
            _build_summarizing,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=(
            "A model=None summary inherits the run model; non-text models need an explicit model.",
            "Summary usage counts toward the run's usage limits.",
        ),
        starter_roles=("developer", "research"),
    ),
    _entry(
        plugin_id="harness.tiered_compaction",
        name="Tiered compaction",
        description="Run cheap compaction tiers first, stopping once under the target.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:TieredCompaction",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=TieredConfig,
        factory=_call_with(
            "harness.tiered_compaction",
            "pydantic_ai_harness.compaction:TieredCompaction",
            None,
            _build_tiered,
        ),
        deployment_resources=("compaction.tiers",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=("Tiers are live strategy objects bound through 'compaction.tiers'.",),
    ),
    _entry(
        plugin_id="harness.fallback_compaction",
        name="Fallback compaction",
        description="Try the next strategy only when one raises.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:FallbackCompaction",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=FallbackCompactionConfig,
        factory=_call_with(
            "harness.fallback_compaction",
            "pydantic_ai_harness.compaction:FallbackCompaction",
            None,
            _build_fallback_compaction,
        ),
        deployment_resources=("compaction.fallback_chain",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=("The strategy chain is bound through 'compaction.fallback_chain'.",),
    ),
    _entry(
        plugin_id="harness.clamp_oversized",
        name="Clamp oversized messages",
        description="Head/tail truncate one oversized text part or tool-call argument blob.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:ClampOversizedMessages",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=ClampOversizedConfig,
        factory=_constructor(
            "harness.clamp_oversized",
            "pydantic_ai_harness.compaction:ClampOversizedMessages",
            None,
            fields={
                "max_part_tokens": "max_part_tokens",
                "max_part_chars": "max_part_chars",
                "keep_head_chars": "keep_head_chars",
                "keep_tail_chars": "keep_tail_chars",
                "clamp_tool_call_args": "clamp_tool_call_args",
            },
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=("Never touches tool returns or user prompts.",),
    ),
    _entry(
        plugin_id="harness.deduplicate_reads",
        name="Deduplicate file reads",
        description="Replace superseded file-read results with a placeholder.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:DeduplicateFileReads",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=DeduplicateReadsConfig,
        factory=_call_with(
            "harness.deduplicate_reads",
            "pydantic_ai_harness.compaction:DeduplicateFileReads",
            None,
            _build_deduplicate,
        ),
        deployment_resources=("callbacks.file_key",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.compaction",),
        notes=(
            "The file_key callable is required and cannot be inferred safely; bind it as a resource.",
        ),
    ),
    _entry(
        plugin_id="harness.warn_near_limits",
        name="Warn near limits",
        description="Warn the model as iteration or context limits approach (no history edits).",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:WarnNearLimits",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=WarnNearLimitsConfig,
        factory=_constructor(
            "harness.warn_near_limits",
            "pydantic_ai_harness.compaction:WarnNearLimits",
            None,
            fields={
                "max_iterations": "max_iterations",
                "max_context_tokens": "max_context_tokens",
                "max_total_tokens": "max_total_tokens",
                "max_context_fraction": "max_context_fraction",
                "warning_threshold": "warning_threshold",
                "critical_remaining_iterations": "critical_remaining_iterations",
            },
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.usage",),
        notes=(
            "For operator alerts subscribe to ContextUsageEvent; this capability only steers the model.",
        ),
    ),
    _entry(
        plugin_id="harness.report_context_usage",
        name="Report context usage",
        description="Emit a ContextUsageEvent with a live context-window gauge.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.compaction:ReportContextUsage",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=ReportContextUsageConfig,
        factory=_constructor(
            "harness.report_context_usage",
            "pydantic_ai_harness.compaction:ReportContextUsage",
            None,
            fields={
                "context_window": "context_window",
                "fallback_context_window": "fallback_context_window",
            },
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.usage",),
        notes=("resolved=False means the window is a fallback guess, not model metadata.",),
    ),
    _entry(
        plugin_id="harness.tool_output_limits",
        name="Tool output limits",
        description="Truncate, spill or summarize oversized tool returns when produced.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.tool_output_limits:ToolOutputLimits",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=ToolOutputLimitsConfig,
        factory=_call_with(
            "harness.tool_output_limits",
            "pydantic_ai_harness.tool_output_limits:ToolOutputLimits",
            None,
            _build_tool_output_limits,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.tool_output",),
        notes=(
            "store='workspace' needs a workspace only when a band can spill.",
            "store='local_file' is an explicit host choice, not a silent fallback.",
            "Provider-native tools bypass these limits.",
        ),
        starter_roles=("developer", "research"),
    ),
    _entry(
        plugin_id="harness.warn_cache_busts",
        name="Warn on cache busts",
        description="Warn when a request falls far short of the established cached prefix.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.warn_on_cache_busts:WarnOnCacheBusts",
        group=CatalogGroup.CONTEXT,
        tier=CatalogTier.VERIFIED,
        config_model=WarnOnCacheBustsConfig,
        factory=_constructor(
            "harness.warn_cache_busts",
            "pydantic_ai_harness.warn_on_cache_busts:WarnOnCacheBusts",
            None,
            fields={
                "collapse_ratio": "collapse_ratio",
                "min_prefix_tokens": "min_prefix_tokens",
                "min_missed_ratio": "min_missed_ratio",
                "min_missed_tokens": "min_missed_tokens",
                "cache_ttl_seconds": "cache_ttl_seconds",
            },
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.usage",),
        notes=("Observation only: emits a CacheBustWarning, never edits history.",),
    ),
    # -- knowledge and memory ----------------------------------------------
    _entry(
        plugin_id="harness.memory",
        name="Memory",
        description="A persistent Markdown notebook the model writes and searches.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.memory:Memory",
        group=CatalogGroup.KNOWLEDGE,
        tier=CatalogTier.VERIFIED,
        config_model=MemoryConfig,
        factory=_call_with(
            "harness.memory", "pydantic_ai_harness.memory:Memory", None, _build_memory
        ),
        deployment_resources=("memory.store",),
        change_mode="next_turn",
        state_mode="external",
        presentation=("activity.memory",),
        notes=(
            "The store is explicit: the catalog never defaults to process-local InMemoryStore.",
            "Memory content is model-written and therefore untrusted.",
        ),
        starter_roles=("developer", "research"),
    ),
    _entry(
        plugin_id="harness.conversation_search",
        name="Conversation search",
        description="BM25 recall over history that StepPersistence already stored.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.conversation_search:ConversationSearch",
        group=CatalogGroup.KNOWLEDGE,
        tier=CatalogTier.VERIFIED,
        config_model=ConversationSearchConfig,
        factory=_call_with(
            "harness.conversation_search",
            "pydantic_ai_harness.conversation_search:ConversationSearch",
            None,
            _build_conversation_search,
        ),
        deployment_resources=("conversations",),
        change_mode="next_turn",
        state_mode="external",
        presentation=("activity.search",),
        notes=(
            "Pair with StepPersistence over the same store; scope is explicit to silence deprecation.",
        ),
    ),
    _entry(
        plugin_id="harness.skills",
        name="Agent Skills",
        description="Load SKILL.md libraries as deferred capabilities.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.skills:Skills",
        group=CatalogGroup.KNOWLEDGE,
        tier=CatalogTier.VERIFIED,
        config_model=SkillsConfig,
        factory=_call_with(
            "harness.skills", "pydantic_ai_harness.skills:Skills", None, _build_skills
        ),
        extras=(_install(HARNESS, "skills"),),
        deployment_resources=("workspace",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.skills",),
        notes=(
            "The [skills] extra is PyYAML; the harness[runtime] extra already depends on PyYAML.",
            "Skill bodies become model instructions: load only trusted libraries.",
        ),
        starter_roles=("developer",),
    ),
    _entry(
        plugin_id="harness.repo_context",
        name="Repository context",
        description="Load CLAUDE.md/AGENTS.md instructions and expose an asset inventory tool.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.repo_context:RepoContext",
        group=CatalogGroup.KNOWLEDGE,
        tier=CatalogTier.VERIFIED,
        config_model=RepoContextConfig,
        factory=_call_with(
            "harness.repo_context",
            "pydantic_ai_harness.repo_context:RepoContext",
            None,
            _build_repo_context,
        ),
        deployment_resources=("workspace",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.context",),
        notes=("home_dir is a workspace path; '~' raises UserError upstream.",),
        starter_roles=("developer",),
    ),
    _entry(
        plugin_id="harness.pydantic_ai_docs",
        name="Pydantic AI docs",
        description="Read pinned Pydantic AI documentation pages on demand.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.pydantic_ai_docs:PydanticAIDocs",
        group=CatalogGroup.KNOWLEDGE,
        tier=CatalogTier.VERIFIED,
        config_model=PydanticAIDocsConfig,
        factory=_call_with(
            "harness.pydantic_ai_docs",
            "pydantic_ai_harness.pydantic_ai_docs:PydanticAIDocs",
            None,
            _build_docs,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.docs",),
        notes=(
            "With local_docs_path the read goes through the run workspace; set one deliberately.",
        ),
    ),
    # -- interaction and controls ------------------------------------------
    _entry(
        plugin_id="harness.input_guardrail",
        name="Input guardrail",
        description="Block, redact or retry prompts before they reach the model.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.guardrails:InputGuardrail",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=InputGuardrailConfig,
        factory=_call_with(
            "harness.input_guardrail",
            "pydantic_ai_harness.guardrails:InputGuardrail",
            None,
            _build_input_guardrail,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.guardrail",),
        notes=("Custom guard callables are Python-only; built-in detectors are serialized here.",),
    ),
    _entry(
        plugin_id="harness.output_guardrail",
        name="Output guardrail",
        description="Block, redact or retry the final model output.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.guardrails:OutputGuardrail",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=OutputGuardrailConfig,
        factory=_call_with(
            "harness.output_guardrail",
            "pydantic_ai_harness.guardrails:OutputGuardrail",
            None,
            _build_output_guardrail,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.guardrail",),
        notes=("Streaming already sent chunks cannot be un-sent when the final check fails.",),
    ),
    _entry(
        plugin_id="harness.tool_guardrail",
        name="Tool guardrail",
        description="Hide tools or gate/redact tool arguments and results.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.guardrails:ToolGuardrail",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=ToolGuardrailConfig,
        factory=_call_with(
            "harness.tool_guardrail",
            "pydantic_ai_harness.guardrails:ToolGuardrail",
            None,
            _build_tool_guardrail,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.guardrail",),
        notes=(
            "Static 'hidden' lists are serialized; approval/redaction guards need a "
            "'callbacks.tool_guardrail' resource.",
            "Provider-native and deferred tools bypass argument guards.",
        ),
    ),
    _entry(
        plugin_id="harness.prompt_injection_defender",
        name="Prompt-injection defender",
        description="Classify locally executed tool results for indirect prompt injection.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.prompt_injection_defender:PromptInjectionDefender",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=PromptInjectionDefenderConfig,
        factory=_call_with(
            "harness.prompt_injection_defender",
            "pydantic_ai_harness.prompt_injection_defender:PromptInjectionDefender",
            _install(HARNESS, "prompt-injection-defender"),
            _build_injection_defender,
        ),
        extras=(_install(HARNESS, "prompt-injection-defender"),),
        requires_modules=("stackone_defender",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.guardrail",),
        notes=(
            "Report-only unless block_high_risk is set.",
            "Custom PromptDefense objects are Python-only and bind through 'defense'.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.tool_call_judge",
        name="Tool-call judge",
        description="Ask a second model whether each selected tool call is safe to run.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.tool_call_judge:ToolCallJudge",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=ToolCallJudgeConfig,
        factory=_call_with(
            "harness.tool_call_judge",
            "pydantic_ai_harness.tool_call_judge:ToolCallJudge",
            None,
            _build_tool_call_judge,
        ),
        deployment_resources=("model.judge",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.guardrail",),
        notes=(
            "A model-based filter, not an authorization boundary; arguments leave the process.",
            "on_uncertain='ask' needs deferred tool requests in the outer agent's output type.",
        ),
    ),
    _entry(
        plugin_id="harness.approval_reviewer",
        name="Approval reviewer",
        description="Let a second model pre-approve low-risk calls; people see the rest.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.approval_reviewer:ApprovalReviewer",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=ApprovalReviewerConfig,
        factory=_call_with(
            "harness.approval_reviewer",
            "pydantic_ai_harness.approval_reviewer:ApprovalReviewer",
            None,
            _build_approval_reviewer,
        ),
        deployment_resources=("model.judge",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.guardrail",),
        notes=(
            "It only approves; pair with the tool-call judge to block.",
            "Reviewed arguments leave the process for the reviewer provider.",
        ),
    ),
    _entry(
        plugin_id="harness.spend_limits",
        name="Spend limits",
        description="Cap USD or token spend across run, conversation, day, month or total windows.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.spend:SpendLimits",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=SpendLimitsConfig,
        factory=_call_with(
            "harness.spend_limits",
            "pydantic_ai_harness.spend:SpendLimits",
            None,
            _build_spend_limits,
        ),
        change_mode="next_turn",
        state_mode="external",
        presentation=("activity.usage",),
        notes=(
            "Guarantees no request starts after exhaustion; the crossing request completes.",
            "InMemory store is per-process; use store='resource' for a shared store.",
            "Per-tenant scope callables are Python-only.",
        ),
    ),
    _entry(
        plugin_id="harness.ask_user",
        name="Ask user",
        description="Ask the user 1-10 multiple-choice questions mid-run and wait.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.ask_user:AskUser",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=AskUserConfig,
        factory=_call_with(
            "harness.ask_user", "pydantic_ai_harness.ask_user:AskUser", None, _build_ask_user
        ),
        deployment_resources=("user_answers",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("interaction.question",),
        notes=(
            "There is no default answerer; the host/ACP adapter supplies 'user_answers'.",
            "answerer=None defers calls to DeferredToolRequests instead of inventing an answer.",
        ),
    ),
    _entry(
        plugin_id="harness.system_reminders",
        name="System reminders",
        description="Cache-safe reminders appended to the tail of model requests.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.system_reminders:SystemReminders",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=SystemRemindersConfig,
        factory=_call_with(
            "harness.system_reminders",
            "pydantic_ai_harness.system_reminders:SystemReminders",
            None,
            _build_system_reminders,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.reminder",),
        notes=(
            "Static schedules and GoalReanchor are serialized; dynamic callables need a resource.",
            "Not spec-serializable upstream; the catalog descriptor supplies the schema instead.",
        ),
    ),
    _entry(
        plugin_id="harness.trajectory_judge",
        name="Trajectory judge",
        description="Periodically review the run with a judge model and steer when it drifts.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.trajectory_judge:TrajectoryJudge",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=TrajectoryJudgeConfig,
        factory=_call_with(
            "harness.trajectory_judge",
            "pydantic_ai_harness.trajectory_judge:TrajectoryJudge",
            None,
            _build_trajectory_judge,
        ),
        deployment_resources=("model.judge",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.guardrail",),
        notes=(
            "Rejected at run start inside durable workflows (Temporal/DBOS/Prefect).",
            "Sends a transcript window to the judge provider concurrently with the run.",
        ),
    ),
    _entry(
        plugin_id="harness.repair_tool_arguments",
        name="Repair tool arguments",
        description="Repair malformed JSON tool arguments before validation.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.repair_tool_arguments:RepairToolArguments",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_constructor(
            "harness.repair_tool_arguments",
            "pydantic_ai_harness.repair_tool_arguments:RepairToolArguments",
            None,
            fields={},
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.tool",),
        notes=("Heuristic repair; wrong types still go through normal validation.",),
    ),
    _entry(
        plugin_id="pydantic.handle_deferred_tool_calls",
        name="Deferred tool-call handler",
        description="Resolve deferred calls/approvals with a host-supplied handler.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:HandleDeferredToolCalls",
        group=CatalogGroup.INTERACTION,
        tier=CatalogTier.VERIFIED,
        config_model=DeferredToolCallsConfig,
        factory=_call_with(
            "pydantic.handle_deferred_tool_calls",
            "pydantic_ai.capabilities:HandleDeferredToolCalls",
            None,
            _build_handle_deferred,
        ),
        deployment_resources=("deferred.handler",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("interaction.approval",),
        notes=(
            "ACP permission prompts drive the host handler; no silent auto-approval.",
            "The handler is a callable resource and never serialized.",
        ),
        starter_roles=("developer",),
    ),
    # -- self-extension ----------------------------------------------------
    _entry(
        plugin_id="harness.capability_creation",
        name="Runtime capability creation",
        description="Let the model author and load new capabilities on later runs (opt-in).",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.capability_creation:CapabilityCreation",
        group=CatalogGroup.SELF_EXTENSION,
        tier=CatalogTier.VERIFIED,
        config_model=CapabilityCreationConfig,
        factory=_call_with(
            "harness.capability_creation",
            "pydantic_ai_harness.capability_creation:CapabilityCreation",
            None,
            _build_capability_creation,
        ),
        deployment_resources=("workspace",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.code",),
        notes=(
            "Disabled in starter definitions; a deployment explicitly authorizes host-side code loading.",
            "Authored code executes in the worker process and needs a writable LocalWorkspace.",
            "It cannot install packages or expand remote plugin rights.",
        ),
        disabled_in_starters=True,
    ),
    # -- execution services ------------------------------------------------
    _entry(
        plugin_id="pydantic.instrumentation",
        name="Instrumentation",
        description="OpenTelemetry instrumentation for agent runs and tool calls.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:Instrumentation",
        group=CatalogGroup.SERVICES,
        tier=CatalogTier.VERIFIED,
        config_model=InstrumentationConfig,
        factory=_call_with(
            "pydantic.instrumentation",
            "pydantic_ai.capabilities:Instrumentation",
            None,
            _build_instrumentation,
        ),
        change_mode="new_session",
        state_mode="run",
        presentation=("activity.usage",),
        notes=(
            "Process-level observability: use upstream OpenTelemetry; no Logfire requirement.",
            "Sensitive content can be excluded via include_content=False.",
        ),
    ),
    _entry(
        plugin_id="harness.step_persistence",
        name="Step persistence",
        description="Append-only step log, history snapshots and a tool-effect ledger.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.step_persistence:StepPersistence",
        group=CatalogGroup.SERVICES,
        tier=CatalogTier.VERIFIED,
        config_model=StepPersistenceConfig,
        factory=_call_with(
            "harness.step_persistence",
            "pydantic_ai_harness.step_persistence:StepPersistence",
            None,
            _build_step_persistence,
        ),
        deployment_resources=("step.store",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.persistence",),
        notes=(
            "The store is explicit; the catalog never defaults to InMemoryStepStore.",
            "It does not replay effects or promise exactly-once execution.",
        ),
    ),
    _entry(
        plugin_id="pydantic.use_thread_executor",
        name="Thread executor",
        description="Run synchronous tools/hooks on a host-provided executor.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:UseThreadExecutor",
        group=CatalogGroup.SERVICES,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_call_with(
            "pydantic.use_thread_executor",
            "pydantic_ai.capabilities:UseThreadExecutor",
            None,
            _build_use_thread_executor,
        ),
        deployment_resources=("thread.executor",),
        change_mode="new_session",
        state_mode="run",
        presentation=("activity.runtime",),
        notes=("The executor is a host resource; the catalog never creates a hidden global pool.",),
    ),
    _entry(
        plugin_id="harness.managed_prompt",
        name="Managed prompt",
        description="Resolve a Logfire-managed prompt once per run as the agent instructions.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.logfire:ManagedPrompt",
        group=CatalogGroup.SERVICES,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=ManagedPromptConfig,
        factory=_call_with(
            "harness.managed_prompt",
            "pydantic_ai_harness.logfire:ManagedPrompt",
            _install(HARNESS, "logfire"),
            _build_managed_prompt,
        ),
        extras=(_install(HARNESS, "logfire"),),
        requires_modules=("logfire",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.prompt",),
        notes=(
            "Needs the [logfire] extra and a configured Logfire project; falls back to 'default'.",
            "Pin label='production' to keep provider prompt caches warm.",
        ),
        signature_verified=False,
        disabled_in_starters=True,
    ),
    # -- durability --------------------------------------------------------
    _entry(
        plugin_id="pydantic.temporal_durability",
        name="Temporal durability",
        description="Core Temporal durable execution capability.",
        integration=CORE,
        upstream_import="pydantic_ai.durable_exec.temporal:TemporalDurability",
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=DurabilityConfig,
        factory=_constructor(
            "pydantic.temporal_durability",
            "pydantic_ai.durable_exec.temporal:TemporalDurability",
            _install(CORE, "temporal"),
            fields={"name": "name"},
        ),
        extras=(_install(CORE, "temporal"),),
        requires_modules=("temporalio",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.durability",),
        notes=(
            "Deployment-bound: durable compositions are preregistered and pinned per session.",
            "Dynamic model selection and workspace configuration have upstream durable constraints.",
            "Constructor arguments are not managed beyond 'name'.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="pydantic.dbos_durability",
        name="DBOS durability",
        description="Core DBOS durable execution capability.",
        integration=CORE,
        upstream_import="pydantic_ai.durable_exec.dbos:DBOSDurability",
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=DurabilityConfig,
        factory=_constructor(
            "pydantic.dbos_durability",
            "pydantic_ai.durable_exec.dbos:DBOSDurability",
            _install(CORE, "dbos"),
            fields={"name": "name"},
        ),
        extras=(_install(CORE, "dbos"),),
        requires_modules=("dbos",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.durability",),
        notes=(
            "Deployment-bound durability engine; constructor arguments are not managed beyond 'name'.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="pydantic.prefect_durability",
        name="Prefect durability",
        description="Core Prefect durable execution capability.",
        integration=CORE,
        upstream_import="pydantic_ai.durable_exec.prefect:PrefectDurability",
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=DurabilityConfig,
        factory=_constructor(
            "pydantic.prefect_durability",
            "pydantic_ai.durable_exec.prefect:PrefectDurability",
            _install(CORE, "prefect"),
            fields={"name": "name"},
        ),
        extras=(_install(CORE, "prefect"),),
        requires_modules=("prefect",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.durability",),
        notes=(
            "Deployment-bound durability engine; constructor arguments are not managed beyond 'name'.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.aws_lambda_durability",
        name="AWS Lambda durability",
        description="Checkpoint every model/tool step as an AWS Lambda durable step.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.aws_lambda:AWSLambdaDurability",
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=DurabilityConfig,
        factory=_constructor(
            "harness.aws_lambda_durability",
            "pydantic_ai_harness.aws_lambda:AWSLambdaDurability",
            _install(HARNESS, "aws-lambda"),
            fields={"name": "name"},
        ),
        extras=(_install(HARNESS, "aws-lambda"),),
        requires_modules=("aws_durable_execution_sdk_python",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.durability",),
        notes=(
            "A run is durable only inside durable_agent_handler; plain runs checkpoint nothing.",
            "3,000 operations / 100 MB checkpointed state budget.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.absurd_durability",
        name="Absurd durability",
        description="Checkpoint agent steps as Absurd Postgres tasks.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.absurd:AbsurdDurability",
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=ParallelDurabilityConfig,
        factory=_constructor(
            "harness.absurd_durability",
            "pydantic_ai_harness.absurd:AbsurdDurability",
            _install(HARNESS, "absurd"),
            fields={"name": "name", "parallel_execution_mode": "parallel_execution_mode"},
        ),
        extras=(_install(HARNESS, "absurd"),),
        requires_modules=("absurd_sdk",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.durability",),
        notes=("Postgres-backed; tool results are stored as JSON. Keep side effects idempotent.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.pgtask_durability",
        name="pgtask durability",
        description="Checkpoint agent steps as pgtask Postgres tasks.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.pgtask:PGTaskDurability",
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=ParallelDurabilityConfig,
        factory=_constructor(
            "harness.pgtask_durability",
            "pydantic_ai_harness.pgtask:PGTaskDurability",
            _install(HARNESS, "pgtask"),
            fields={"name": "name", "parallel_execution_mode": "parallel_execution_mode"},
        ),
        extras=(_install(HARNESS, "pgtask"),),
        requires_modules=("pgtask",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.durability",),
        notes=("Postgres-backed; keep side effects idempotent across replays.",),
        signature_verified=False,
    ),
    _entry(
        plugin_id="harness.render_workflows",
        name="Render Workflows",
        description="Run model and tool operations as chained Render task runs.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.render:RenderWorkflows",
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.INSTALL_REQUIRED,
        config_model=_NoConfig,
        factory=_call_with(
            "harness.render_workflows",
            "pydantic_ai_harness.render:RenderWorkflows",
            _install(HARNESS, "render"),
            _build_render,
        ),
        extras=(_install(HARNESS, "render"),),
        requires_modules=("render",),
        deployment_resources=("render.app",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.durability",),
        notes=(
            "The live Render Workflows app is a Python resource; not spec-serializable.",
            "Retrying the entry task reruns the agent; the integration does not checkpoint steps.",
        ),
        signature_verified=False,
    ),
    _entry(
        plugin_id="pydantic.restate_durability",
        name="Restate durability (not shipped)",
        description="The concept inventory names a Restate durability integration.",
        integration=CORE,
        upstream_import=None,
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.UNAVAILABLE,
        config_model=None,
        factory=None,
        notes=(
            "pydantic-ai-slim 2.57.0 ships durable_exec for Temporal, DBOS and Prefect only; "
            "there is no Restate module. Do not advertise this as runtime-compatible.",
        ),
    ),
    _entry(
        plugin_id="pydantic.kitaru_durability",
        name="Kitaru durability (not shipped)",
        description="The concept inventory names a Kitaru durability integration.",
        integration=CORE,
        upstream_import=None,
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.UNAVAILABLE,
        config_model=None,
        factory=None,
        notes=(
            "No Kitaru module exists in pydantic-ai-slim 2.57.0; recorded here as an explicit gap.",
        ),
    ),
    _entry(
        plugin_id="pydantic.airflow_durability",
        name="Airflow durability (not shipped)",
        description="The concept inventory names an Airflow durability integration.",
        integration=CORE,
        upstream_import=None,
        group=CatalogGroup.DURABILITY,
        tier=CatalogTier.UNAVAILABLE,
        config_model=None,
        factory=None,
        notes=(
            "No Airflow module exists in pydantic-ai-slim 2.57.0; recorded here as an explicit gap.",
        ),
    ),
    # -- loop customization ------------------------------------------------
    _entry(
        plugin_id="pydantic.thinking",
        name="Thinking",
        description="Request provider reasoning effort where the model supports it.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:Thinking",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=ThinkingConfig,
        factory=_constructor(
            "pydantic.thinking",
            "pydantic_ai.capabilities:Thinking",
            None,
            fields={"effort": "effort"},
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.thinking",),
        notes=("Provider support varies; unsupported effort levels fail at model resolution.",),
    ),
    _entry(
        plugin_id="pydantic.hooks",
        name="Hooks",
        description="Attach native lifecycle hooks built by host code.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:Hooks",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_call_with("pydantic.hooks", "pydantic_ai.capabilities:Hooks", None, _build_hooks),
        deployment_resources=("hooks",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.runtime",),
        notes=(
            "Hook callbacks are live Python; a definition binds a host-built Hooks instance.",
            "Hook ordering follows upstream capability order.",
        ),
    ),
    _entry(
        plugin_id="pydantic.select_model",
        name="Select model",
        description="Choose the model per run with a host-supplied selector.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:SelectModel",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_callback(
            "pydantic_ai.capabilities:SelectModel",
            "pydantic.select_model",
            "callbacks.model_selector",
            "ModelSelector callable choosing a model from run context",
        ),
        deployment_resources=("callbacks.model_selector",),
        change_mode="new_session",
        state_mode="run",
        presentation=("activity.model",),
        notes=("Dynamic model selection has upstream durable-execution constraints.",),
    ),
    _entry(
        plugin_id="pydantic.resolve_model_id",
        name="Resolve model id",
        description="Map model ids through a host-supplied resolver.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:ResolveModelId",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_callback(
            "pydantic_ai.capabilities:ResolveModelId",
            "pydantic.resolve_model_id",
            "callbacks.model_id_resolver",
            "ModelIdResolver callable mapping requested ids to concrete models",
        ),
        deployment_resources=("callbacks.model_id_resolver",),
        change_mode="new_session",
        state_mode="run",
        presentation=("activity.model",),
        notes=("Resolver callables stay host-local and are never serialized.",),
    ),
    _entry(
        plugin_id="pydantic.prepare_tools",
        name="Prepare tools",
        description="Filter or rewrite function tool definitions before each request.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:PrepareTools",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_prepared("pydantic_ai.capabilities:PrepareTools", "pydantic.prepare_tools"),
        deployment_resources=("callbacks.prepare_tools",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.runtime",),
        notes=("The callback is live Python and binds through 'callbacks.prepare_tools'.",),
    ),
    _entry(
        plugin_id="pydantic.prepare_output_tools",
        name="Prepare output tools",
        description="Filter or rewrite output tool definitions before each request.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:PrepareOutputTools",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_prepared(
            "pydantic_ai.capabilities:PrepareOutputTools", "pydantic.prepare_output_tools"
        ),
        deployment_resources=("callbacks.prepare_tools",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.runtime",),
        notes=("The callback is live Python and binds through 'callbacks.prepare_tools'.",),
    ),
    _entry(
        plugin_id="pydantic.prefix_tools",
        name="Prefix tools",
        description="Prefix another capability's tool names to avoid collisions.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:PrefixTools",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=PrefixToolsConfig,
        factory=_call_with(
            "pydantic.prefix_tools",
            "pydantic_ai.capabilities:PrefixTools",
            None,
            _build_prefix_tools,
        ),
        deployment_resources=("wrapped.capability",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.runtime",),
        notes=("The wrapped native capability is bound as 'wrapped.capability'.",),
    ),
    _entry(
        plugin_id="pydantic.include_return_schemas",
        name="Include tool return schemas",
        description="Include tool return schemas in the tool inventory.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:IncludeToolReturnSchemas",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=IncludeReturnSchemasConfig,
        factory=_call_with(
            "pydantic.include_return_schemas",
            "pydantic_ai.capabilities:IncludeToolReturnSchemas",
            None,
            _build_include_schemas,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.tool",),
        notes=("tools=None means all tools.",),
    ),
    _entry(
        plugin_id="pydantic.set_tool_metadata",
        name="Set tool metadata",
        description="Attach metadata to selected tool definitions.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:SetToolMetadata",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=SetToolMetadataConfig,
        factory=_call_with(
            "pydantic.set_tool_metadata",
            "pydantic_ai.capabilities:SetToolMetadata",
            None,
            _build_set_tool_metadata,
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.tool",),
        notes=("Metadata is merged into the tool definitions sent to the model.",),
    ),
    _entry(
        plugin_id="pydantic.raise_content_filter_error",
        name="Raise content-filter errors",
        description="Turn provider content-filter stops into a raised error.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:RaiseContentFilterError",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_constructor(
            "pydantic.raise_content_filter_error",
            "pydantic_ai.capabilities:RaiseContentFilterError",
            None,
            fields={},
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.error",),
        notes=(),
    ),
    _entry(
        plugin_id="pydantic.reinject_system_prompt",
        name="Reinject system prompt",
        description="Re-send the system prompt near the tail of long histories.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:ReinjectSystemPrompt",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=ReinjectSystemPromptConfig,
        factory=_constructor(
            "pydantic.reinject_system_prompt",
            "pydantic_ai.capabilities:ReinjectSystemPrompt",
            None,
            fields={"replace_existing": "replace_existing"},
        ),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.runtime",),
        notes=(),
    ),
    _entry(
        plugin_id="pydantic.process_history",
        name="Process history",
        description="Transform model history with a host-supplied processor.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:ProcessHistory",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_callback(
            "pydantic_ai.capabilities:ProcessHistory",
            "pydantic.process_history",
            "callbacks.history",
            "HistoryProcessorFunc callable transforming message history",
        ),
        deployment_resources=("callbacks.history",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.runtime",),
        notes=("Prefer the managed compaction strategies for standard trimming.",),
    ),
    _entry(
        plugin_id="pydantic.process_event_stream",
        name="Process event stream",
        description="Observe or transform native run events with a host-supplied handler.",
        integration=CORE,
        upstream_import="pydantic_ai.capabilities:ProcessEventStream",
        group=CatalogGroup.LOOP,
        tier=CatalogTier.VERIFIED,
        config_model=_NoConfig,
        factory=_callback(
            "pydantic_ai.capabilities:ProcessEventStream",
            "pydantic.process_event_stream",
            "callbacks.event_stream",
            "EventStreamHandlerFunc callable observing native run events",
        ),
        deployment_resources=("callbacks.event_stream",),
        change_mode="next_turn",
        state_mode="run",
        presentation=("activity.runtime",),
        notes=("The runtime stream is separate from the public ACP transcript.",),
    ),
)


# Media-store resource factories are deployment resources, not agent capabilities.
MEDIA_STORES: tuple[CatalogEntry, ...] = (
    _entry(
        plugin_id="harness.media.disk",
        name="Disk media store",
        description="Content-addressed media files on the host filesystem.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.media:DiskMediaStore",
        group=CatalogGroup.SERVICES,
        tier=CatalogTier.VERIFIED,
        kind=CatalogKind.RESOURCE,
        config_model=DiskMediaStoreConfig,
        factory=_call_with(
            "harness.media.disk",
            "pydantic_ai_harness.media:DiskMediaStore",
            None,
            _media_disk,
        ),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.media",),
        notes=("Bind as the 'media.store' resource; not a toggleable agent binding.",),
    ),
    _entry(
        plugin_id="harness.media.sqlite",
        name="SQLite media store",
        description="Media blobs stored in a SQLite database.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.media:SqliteMediaStore",
        group=CatalogGroup.SERVICES,
        tier=CatalogTier.VERIFIED,
        kind=CatalogKind.RESOURCE,
        config_model=SqliteMediaStoreConfig,
        factory=_call_with(
            "harness.media.sqlite",
            "pydantic_ai_harness.media:SqliteMediaStore",
            None,
            _media_sqlite,
        ),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.media",),
        notes=("Bind as the 'media.store' resource; the database file lives on the runtime host.",),
    ),
    _entry(
        plugin_id="harness.media.s3",
        name="S3 media store",
        description="Media blobs in an S3-compatible bucket.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.media:S3MediaStore",
        group=CatalogGroup.SERVICES,
        tier=CatalogTier.VERIFIED,
        kind=CatalogKind.RESOURCE,
        config_model=S3MediaStoreConfig,
        factory=_call_with(
            "harness.media.s3",
            "pydantic_ai_harness.media:S3MediaStore",
            None,
            _media_s3,
        ),
        requires_modules=(),
        deployment_resources=("media.s3_credentials",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.media",),
        notes=(
            "Credentials never enter config: bind 'media.s3_credentials' as a host secret mapping.",
            "Bind the resulting store as the 'media.store' resource.",
        ),
    ),
    _entry(
        plugin_id="harness.media.postgres",
        name="Postgres media store",
        description="Media blobs in a PostgreSQL database through a host pool.",
        integration=HARNESS,
        upstream_import="pydantic_ai_harness.media:PostgresMediaStore",
        group=CatalogGroup.SERVICES,
        tier=CatalogTier.VERIFIED,
        kind=CatalogKind.RESOURCE,
        config_model=PostgresMediaStoreConfig,
        factory=_call_with(
            "harness.media.postgres",
            "pydantic_ai_harness.media:PostgresMediaStore",
            None,
            _media_postgres,
        ),
        deployment_resources=("media.postgres_pool",),
        change_mode="new_session",
        state_mode="external",
        presentation=("activity.media",),
        notes=(
            "The asyncpg-compatible pool is a host resource; its lifecycle stays with the host.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------

_BY_ID: dict[str, CatalogEntry] = {entry.plugin_id: entry for entry in (*CATALOG, *MEDIA_STORES)}
CATALOG_PLUGIN_IDS = frozenset(_BY_ID)
if len(_BY_ID) != len(CATALOG) + len(MEDIA_STORES):  # pragma: no cover - import-time invariant
    duplicates = sorted(
        {
            entry.plugin_id
            for entry in (*CATALOG, *MEDIA_STORES)
            if sum(1 for other in (*CATALOG, *MEDIA_STORES) if other.plugin_id == entry.plugin_id)
            > 1
        }
    )
    raise RuntimeError(f"duplicate catalog plugin ids: {duplicates}")


def iter_catalog(*, include_resources: bool = True) -> tuple[CatalogEntry, ...]:
    """Return all capability entries (and resource factories by default) in stable order."""
    if include_resources:
        return (*CATALOG, *MEDIA_STORES)
    return CATALOG


def iter_plugins(*, tiers: Iterable[CatalogTier] | None = None) -> tuple[CatalogEntry, ...]:
    """Return capability-kind entries with factories, optionally filtered by tier."""
    selected = set(tiers) if tiers is not None else None
    return tuple(
        entry
        for entry in CATALOG
        if entry.kind is CatalogKind.CAPABILITY
        and entry.factory is not None
        and (selected is None or entry.tier in selected)
    )


def media_store_entries() -> tuple[CatalogEntry, ...]:
    """Deployment resource factories for media stores."""
    return MEDIA_STORES


def unavailable_entries() -> tuple[CatalogEntry, ...]:
    """Entries the pinned upstream releases do not ship; never advertised as usable."""
    return tuple(entry for entry in CATALOG if entry.tier is CatalogTier.UNAVAILABLE)


def entry(plugin_id: str) -> CatalogEntry:
    """Return one entry by stable plugin id (raises ``CAPABILITY_UNAVAILABLE``)."""
    try:
        return _BY_ID[plugin_id]
    except KeyError:
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Catalog entry {plugin_id!r} is not bundled with this runtime.",
            details={"plugin_id": plugin_id, "known": sorted(_BY_ID)},
        ) from None


def plugin_infos() -> list[PluginInfo]:
    """Honest wire descriptors for every managed plugin (no imports performed)."""
    return [entry.info() for entry in iter_plugins()]


def resource_requirements(entry_: CatalogEntry) -> list:
    """Typed resource requirements a definition should declare for this entry."""
    from .resources import ResourceRequirement  # local import keeps the public surface small

    return [
        ResourceRequirement(
            name=name,
            kind=_resource_kind(name),
            description="Required by the managed binding.",
            required=True,
        )
        for name in entry_.deployment_resources
    ]


def register_catalog(
    registry: PluginRegistry,
    *,
    include_tiers: Iterable[CatalogTier] | None = None,
) -> list[str]:
    """Register every capability entry with a factory; return the registered plugin ids.

    ``UNAVAILABLE`` entries are never registered: the host must not offer a binding it
    cannot compose. Install-required entries are registered with their import gates, so a
    composition fails with an actionable install message instead of a silent downgrade.
    """
    accepted = (
        set(include_tiers)
        if include_tiers is not None
        else {
            CatalogTier.VERIFIED,
            CatalogTier.INSTALL_REQUIRED,
        }
    )
    registered: list[str] = []
    for catalog_entry in iter_plugins():
        if catalog_entry.tier not in accepted:
            continue
        registry.register(catalog_entry.descriptor())
        registered.append(catalog_entry.plugin_id)
    return registered


def build_catalog_registry(*, allow_local_binding_creation: bool = False) -> PluginRegistry:
    """Create a registry with the bundled catalog registered."""
    registry = PluginRegistry(allow_local_binding_creation=allow_local_binding_creation)
    register_catalog(registry)
    return registry


def describe_tiers() -> dict[str, list[str]]:
    """Group plugin ids by coverage tier (used by docs/tests)."""
    result: dict[str, list[str]] = {tier.value: [] for tier in CatalogTier}
    for catalog_entry in iter_catalog():
        result[catalog_entry.tier.value].append(catalog_entry.plugin_id)
    return result
