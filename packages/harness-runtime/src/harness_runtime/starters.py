"""Editable developer and research starter definitions.

Both starters are ordinary :class:`~harness_runtime.definitions.AgentDefinition` objects, not
privileged agent kinds.  They exist to illustrate the framework with broad-but-honest
coverage:

* **Developer** requires an explicit ``workspace`` resource (recommended local binding: the
  Smol sandbox; a ``LocalWorkspace`` binding is also supported for trusted work).  File,
  shell, planning, repository-context, skills and context-control bindings are enabled;
  memory, MCP, web, sub-agents and deferred approvals are off by default.
* **Research** requires only a model.  Search, fetch, planning and context controls are
  enabled; delegation, memory, MCP and browser capabilities are opt-in.  It deliberately has
  no workspace binding, so it runs without one.

Manifest forms mirror the builders under ``examples/starters/*.yaml``.  Use
:func:`definition_from_manifest` to load an edited manifest; it validates every plugin id
against the bundled :mod:`harness_runtime.catalog`, validates each configuration against the
plugin's real config model, and never silently drops an unknown binding.

Runtime Capability Creation (``harness.capability_creation``) is deliberately absent from
both starters; a deployment must opt in explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from harness_protocol import CapabilityPolicy, HarnessError
from pydantic import ValidationError
from pydantic_ai.agent.spec import AgentSpec

from . import catalog
from .catalog import CatalogTier
from .definitions import (
    AgentDefinition,
    DefinitionBinding,
    DefinitionManifest,
    parse_manifest_text,
)
from .resources import ResourceRequirement

__all__ = [
    "DEVELOPER_AGENT_ID",
    "DEVELOPER_BINDING_IDS",
    "DEVELOPER_VERSION",
    "RESEARCH_AGENT_ID",
    "RESEARCH_BINDING_IDS",
    "RESEARCH_VERSION",
    "definition_from_manifest",
    "developer_definition",
    "research_definition",
    "starter_definition",
    "starter_manifest",
    "starter_manifest_path",
    "load_starter_manifest",
]

DEVELOPER_AGENT_ID = "harness.developer"
RESEARCH_AGENT_ID = "harness.research"
DEVELOPER_VERSION = "1.0.0"
RESEARCH_VERSION = "1.0.0"

#: Stable binding ids offered by each starter. Session configuration, persistence and ACP
#: clients refer to these ids, so changing one is a compatibility event.
DEVELOPER_BINDING_IDS = (
    "workspace",
    "filesystem",
    "shell",
    "planning",
    "repo_context",
    "skills",
    "context_clear_tool_results",
    "context_tool_output_limits",
    "context_summarize",
    "deferred_approvals",
    "memory",
    "subagents",
    "web_search",
    "web_fetch",
    "mcp",
)
RESEARCH_BINDING_IDS = (
    "search",
    "fetch",
    "planning",
    "context_clear_tool_results",
    "context_tool_output_limits",
    "context_report_usage",
    "memory",
    "subagents",
    "mcp",
    "browser",
)

StarterName = Literal["developer", "research"]

_DEVELOPER_INSTRUCTIONS = """\
You are a software engineering agent working in the bound workspace.

- Read the repository before changing it; follow AGENTS.md / CLAUDE.md guidance.
- Plan multi-step work and keep the plan current while you work.
- Prefer small, verifiable changes and run the project's tests before reporting success.
- Use shell commands deliberately: the workspace is the only execution location.
- Never claim a command or test passed unless its output shows it did.
"""

_RESEARCH_INSTRUCTIONS = """\
You are a research agent.

- Search broadly before drawing conclusions, then read the sources that support each claim.
- Prefer primary and authoritative sources; cite direct links for factual claims.
- Distinguish sourced facts from your own inference.
- Plan multi-part questions and keep the plan current.
- State clearly when the available tools cannot verify a claim.
"""

#: Stable policy overlays per starter role.  The workspace binding is mandatory for the
#: developer starter (matching CONCEPT section 13.2: no implicit host fallback).
_DEVELOPER_MANDATORY = "workspace"


def _validate_binding_config(plugin_id: str, config: dict[str, Any]) -> dict[str, Any]:
    entry = catalog.entry(plugin_id)
    if entry.tier is CatalogTier.UNAVAILABLE:
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Starter references {plugin_id!r}, which the pinned upstream releases do not ship.",
            details={"plugin_id": plugin_id},
        )
    if not entry.managed:
        raise HarnessError(
            "CAPABILITY_UNAVAILABLE",
            f"Starter references {plugin_id!r}, which has no managed descriptor in this release. "
            "Compose it in Python instead of a starter manifest.",
            details={"plugin_id": plugin_id},
        )
    if entry.config_model is None:
        return dict(config)
    try:
        validated = entry.config_model.model_validate(config)
    except ValidationError as exc:
        raise HarnessError(
            "INVALID_CONFIGURATION",
            f"Starter configuration for {plugin_id!r} is invalid: "
            + "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:5]
            ),
            details={"plugin_id": plugin_id, "errors": exc.errors()[:10]},
        ) from exc
    return validated.model_dump(mode="json", exclude_none=True)


def _binding(
    binding_id: str,
    plugin_id: str,
    config: dict[str, Any] | None = None,
    *,
    enabled: bool,
    client_can_enable: bool = True,
    client_can_disable: bool = True,
) -> DefinitionBinding:
    entry = catalog.entry(plugin_id)
    payload = _validate_binding_config(plugin_id, config or {})
    mandatory = binding_id == _DEVELOPER_MANDATORY
    policy = CapabilityPolicy(
        available=True,
        enabled_by_default=enabled,
        client_can_enable=client_can_enable and not mandatory,
        client_can_disable=client_can_disable and not mandatory,
        editable_fields=set(entry.config_model.model_fields) if entry.config_model else set(),
        change_mode=entry.change_mode,
    )
    return DefinitionBinding(
        binding_id=binding_id,
        plugin_id=plugin_id,
        config=payload,
        enabled=enabled,
        policy=policy,
    )


def _requirement(name: str, *, required: bool = True) -> ResourceRequirement:
    descriptions = {
        "model.default": "Agent model (provider string or native Model instance).",
        "workspace": "Explicit workspace/service binding; never inferred from the client.",
        "memory.store": "MemoryStore backing the optional memory binding.",
        "subagents.agents": "Named child agents for the optional delegation binding.",
        "deferred.handler": "Host approval/deferred-call handler for ACP permission prompts.",
    }
    return ResourceRequirement(
        name=name,
        kind=catalog.RESOURCE_KINDS.get(name, "custom"),  # type: ignore[arg-type]
        description=descriptions.get(name, ""),
        required=required,
    )


def developer_bindings() -> list[DefinitionBinding]:
    """Bindings for the developer starter, enabled/disabled exactly as documented."""
    return [
        _binding(
            "workspace",
            "pydantic.local_workspace",
            {"read_only": False},
            enabled=True,
            client_can_enable=False,
            client_can_disable=False,
        ),
        _binding("filesystem", "harness.filesystem", {"read_only": False}, enabled=True),
        _binding("shell", "harness.shell", {"default_timeout": 30.0}, enabled=True),
        _binding("planning", "harness.planning", {"enable_subtasks": True}, enabled=True),
        _binding("repo_context", "harness.repo_context", {"nested_traversal": True}, enabled=True),
        _binding(
            "skills",
            "harness.skills",
            {"directories": [".agents/skills"], "missing_directories": "skip"},
            enabled=True,
        ),
        _binding(
            "context_clear_tool_results",
            "harness.clear_tool_results",
            {"max_tokens": 60_000, "keep_pairs": 4},
            enabled=True,
        ),
        _binding(
            "context_tool_output_limits",
            "harness.tool_output_limits",
            {"bands": [{"over": 20_000, "action": {"kind": "truncate", "max_chars": 4_000}}]},
            enabled=True,
        ),
        _binding(
            "context_summarize",
            "harness.summarizing_compaction",
            {"max_messages": 120, "keep_messages": 30},
            enabled=False,
        ),
        _binding("deferred_approvals", "pydantic.handle_deferred_tool_calls", {}, enabled=False),
        _binding("memory", "harness.memory", {}, enabled=False),
        _binding(
            "subagents",
            "harness.subagents",
            {"forward_usage": True},
            enabled=False,
        ),
        _binding("web_search", "pydantic.web_search", {"native": True}, enabled=False),
        _binding("web_fetch", "pydantic.web_fetch", {"native": True}, enabled=False),
        _binding("mcp", "pydantic.mcp", {"url": "https://example.org/mcp"}, enabled=False),
    ]


def research_bindings(*, web: bool = True) -> list[DefinitionBinding]:
    """Bindings for the research starter; ``web=False`` keeps it runnable offline.

    With ``web=False`` the definition uses planning + context controls only, so it composes
    against a ``TestModel`` and no workspace.  The shipped manifest enables web capabilities
    (they need a provider with native search/fetch, or the local fallback extras).
    """
    bindings = [
        _binding("search", "pydantic.web_search", {"native": True}, enabled=web),
        _binding("fetch", "pydantic.web_fetch", {"native": True}, enabled=web),
        _binding("planning", "harness.planning", {"enable_subtasks": True}, enabled=True),
        _binding(
            "context_clear_tool_results",
            "harness.clear_tool_results",
            {"max_tokens": 60_000, "keep_pairs": 4},
            enabled=True,
        ),
        _binding(
            "context_tool_output_limits",
            "harness.tool_output_limits",
            {
                # Explicit host-local spill: a research session has no workspace.
                "store": "local_file",
                "bands": [
                    {
                        "over": 30_000,
                        "action": {"kind": "truncate", "strategy": "head_tail", "max_chars": 8_000},
                    }
                ],
            },
            enabled=True,
        ),
        _binding(
            "context_report_usage",
            "harness.report_context_usage",
            {},
            enabled=False,
        ),
        _binding("memory", "harness.memory", {}, enabled=False),
        _binding("subagents", "harness.subagents", {"forward_usage": True}, enabled=False),
        _binding("mcp", "pydantic.mcp", {"url": "https://example.org/mcp"}, enabled=False),
        _binding("browser", "harness.browser_use", {}, enabled=False),
    ]
    return bindings


def developer_definition() -> AgentDefinition:
    """The developer starter as a normal reusable definition."""
    return AgentDefinition(
        agent_id=DEVELOPER_AGENT_ID,
        name="Developer starter",
        description=(
            "Filesystem, shell, planning, repository context, skills and context controls in an "
            "explicit workspace; optional memory, sub-agents, web, MCP and approvals."
        ),
        definition_version=DEVELOPER_VERSION,
        spec=AgentSpec(instructions=_DEVELOPER_INSTRUCTIONS),
        bindings=developer_bindings(),
        resources=[
            _requirement("model.default"),
            _requirement("workspace"),
            _requirement("memory.store", required=False),
            _requirement("subagents.agents", required=False),
            _requirement("deferred.handler", required=False),
        ],
        interaction_features=["plans"],
    )


def research_definition(*, web: bool = True) -> AgentDefinition:
    """The research starter as a normal reusable definition (no workspace requirement)."""
    return AgentDefinition(
        agent_id=RESEARCH_AGENT_ID,
        name="Research starter",
        description=(
            "Search, fetch, planning and context controls; optional memory, delegation, MCP and "
            "browser capabilities. Runs without a workspace."
        ),
        definition_version=RESEARCH_VERSION,
        spec=AgentSpec(instructions=_RESEARCH_INSTRUCTIONS),
        bindings=research_bindings(web=web),
        resources=[
            _requirement("model.default"),
            _requirement("memory.store", required=False),
            _requirement("subagents.agents", required=False),
        ],
        interaction_features=["plans"],
    )


def starter_definition(name: StarterName, *, web: bool = True) -> AgentDefinition:
    if name == "developer":
        return developer_definition()
    if name == "research":
        return research_definition(web=web)
    raise HarnessError(
        "INVALID_CONFIGURATION",
        f"Unknown starter {name!r}; expected 'developer' or 'research'.",
        details={"starter": name},
    )


def starter_manifest(name: StarterName, *, web: bool = True) -> DefinitionManifest:
    """Serializable manifest view of a starter (the YAML examples mirror this)."""
    return starter_definition(name, web=web).to_manifest()


def starter_manifest_path(name: StarterName) -> Path:
    """Repository path of the editable YAML manifest shipped under ``examples/starters``."""
    root = Path(__file__).resolve()
    for parent in root.parents:
        candidate = parent / "examples" / "starters" / f"{name}.yaml"
        if candidate.is_file():
            return candidate
    raise HarnessError(
        "DEPLOYMENT_UNAVAILABLE",
        f"The editable {name!r} starter manifest is not available in this installation; "
        "use starter_manifest() to export the equivalent data.",
        details={"starter": name},
    )


def definition_from_manifest(
    manifest: DefinitionManifest,
    *,
    starter: StarterName | None = None,
) -> AgentDefinition:
    """Validate an edited manifest against the catalog and convert it to a definition.

    Unknown or unmanaged plugins, configurations that fail their real schema, and unavailable
    starter plugins all fail here rather than at the first turn.
    """
    bindings: list[DefinitionBinding] = []
    if starter is None:
        # Infer the starter role so the mandatory workspace lock also applies when a host
        # loads the editable manifest directly (not via load_starter_manifest).
        starter = (
            "developer"
            if manifest.agent_id == DEVELOPER_AGENT_ID
            else "research"
            if manifest.agent_id == RESEARCH_AGENT_ID
            else None
        )
    for binding in manifest.bindings:
        catalog_entry = catalog.entry(binding.plugin_id)
        config = _validate_binding_config(binding.plugin_id, dict(binding.config))
        mandatory = starter == "developer" and binding.binding_id == _DEVELOPER_MANDATORY
        policy = catalog_entry.policy().model_copy(
            update={
                "enabled_by_default": binding.enabled,
                "client_can_disable": False if mandatory else True,
                "client_can_enable": False if mandatory else True,
            }
        )
        bindings.append(
            DefinitionBinding(
                binding_id=binding.binding_id,
                plugin_id=binding.plugin_id,
                config=config,
                enabled=binding.enabled,
                policy=policy,
            )
        )
    resources = [
        ResourceRequirement(
            name=requirement.name,
            kind=requirement.kind,
            description=requirement.description,
            required=requirement.required,
        )
        for requirement in manifest.resources
    ]
    return AgentDefinition(
        agent_id=manifest.agent_id,
        name=manifest.name,
        description=manifest.description,
        definition_version=manifest.definition_version,
        spec=manifest.spec,
        bindings=bindings,
        resources=resources,
        model=manifest.model,
        interaction_features=list(manifest.interaction_features),
    )


def load_starter_manifest(path: str | Path, *, starter: StarterName | None = None) -> AgentDefinition:
    """Parse and validate an editable starter YAML/JSON file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    manifest = parse_manifest_text(
        text,
        fmt="yaml" if path.suffix in {".yaml", ".yml"} else "json",
        source=str(path),
    )
    return definition_from_manifest(manifest, starter=starter)
