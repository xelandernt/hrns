# Capability catalog and coverage

`harness.runtime.catalog` is the runtime's bundled, lazy inventory of the reviewed Pydantic AI
and official Harness capability surface (CONCEPT.md section 9). It provides stable plugin ids,
real upstream import targets, validated serializable configuration, lazy factories, named
resource references, and an honest three-way coverage record.

This document is the coverage matrix: **what is actually tested**, what needs an optional
integration, and what the pinned upstream releases do not ship.

```python
from harness.runtime.catalog import build_catalog_registry, entry, iter_plugins

registry = build_catalog_registry()  # 92 reviewed capability plugins + the bundled example plugin
registry.discover()  # plus installed harness.capabilities entry points
```

Importing the catalog imports **no** integration: factories load `pydantic_ai_harness` and
optional dependencies only when a binding composes. A missing extra raises
`CAPABILITY_UNAVAILABLE` with the exact install command; it never falls back.

## Coverage levels

The three levels from the concept are independent and recorded independently:

| Level | Where it lives | Meaning |
| --- | --- | --- |
| Runtime compatibility | `CatalogTier` / `PluginInfo.runtime_compatible` | The upstream class exists and is usable as a native capability in Python. |
| Managed configuration | `config_model`, `factory`, `PluginDescriptor` | A strict Pydantic schema (`extra="forbid"`) and lazy factory make it usable from definitions, policy and ACP. |
| Presentation | `presentation` | Generic activity rendering always applies; the ids are optional renderer hints. |

### Tiers

| Tier | Count | What the tests prove |
| --- | --- | --- |
| `verified` | 66 entries (62 runtime capabilities + 4 media-store resources) | The import target was imported **and the capability constructed** offline in this workspace, with no optional extras, no provider credentials and no network. |
| `install_required` | 38 entries | The module path exists (`importlib.util.find_spec`); the optional extra is not installed here. Managed entries are registered with an import gate, so composing one raises `CAPABILITY_UNAVAILABLE` with the install command (`uv add "pydantic-ai-harness[modal]"`, `pydantic-ai-slim[mcp]`, …). |
| `unavailable` | 3 entries | The pinned upstream releases ship no such module. They are recorded with `upstream_import=None`, no factory, and are **never registered** as usable plugins. |

Upstream pins reviewed and installed for this matrix:

- `pydantic-ai-slim==2.57.0`
- `pydantic-ai-harness==0.57.0`
- ACP SDK `0.11.1`

“Verified” means exactly what the test names say (`tests/test_catalog.py`):
`test_verified_entries_construct_offline` builds every verified entry from its validated
example configuration and asserts the result is a native `AbstractCapability` (or an accepted
capability callable). It does not claim that a provider, sandbox account, or vendor API works;
those are integration needs listed below.

`signature_verified=False` marks entries whose upstream constructor signature could not be
inspected offline because the module itself needs the extra. Their config models are still
strict and real, and the notes record the documentation the parameters came from. The
constructor is then exercised for the first time on a host that installs the extra.

### Inventory summary

| Group | Entries | Verified | Install required | Unavailable | Managed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Compositions | 2 | 1 | 1 | 0 | 2 |
| Files and execution | 9 | 5 | 4 | 0 | 9 |
| Tools and media | 5 | 3 | 2 | 0 | 5 |
| Business integrations | 12 | 0 | 12 | 0 | 12 |
| Web and research | 9 | 3 | 6 | 0 | 4 |
| Reasoning and delegation | 5 | 4 | 1 | 0 | 5 |
| Context management | 16 | 13 | 3 | 0 | 13 |
| Knowledge and memory | 5 | 5 | 0 | 0 | 5 |
| Interaction and controls | 12 | 11 | 1 | 0 | 12 |
| Self-extension | 1 | 1 | 0 | 0 | 1 |
| Execution services | 8 | 7 | 1 | 0 | 8 |
| Durability | 10 | 0 | 7 | 3 | 7 |
| Loop customization | 13 | 13 | 0 | 0 | 13 |
| **Total** | **107** | **66** | **38** | **3** | **96** |

`92` reviewed capability plugins are registered (the four media stores are deployment *resource*
factories and are intentionally not offered as toggleable bindings).

## Full inventory

`Config` is the catalog's validated Pydantic model (inspect
`entry(plugin_id).config_model.model_json_schema()` at runtime). A dash means the entry has no
managed descriptor yet; those are listed again under “Documented gaps”. Lifecycle is
`change_mode/state_mode`.

### Complete compositions

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Coder | `harness.coder` | `pydantic_ai_harness.coder:Coder` | verified | `CoderConfig` | `workspace` | next_turn/run |
| Researcher | `harness.researcher` | `pydantic_ai_harness.researcher:Researcher` | install_required | `ResearcherConfig` | `workspace` | next_turn/run |

### Files and execution

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Local workspace | `pydantic.local_workspace` | `pydantic_ai.capabilities:LocalWorkspace` | verified | `LocalWorkspaceConfig` | `workspace` | new_session/external |
| File system tools | `harness.filesystem` | `pydantic_ai_harness.filesystem:FileSystem` | verified | `FileSystemConfig` | `workspace` | next_turn/run |
| Shell commands | `harness.shell` | `pydantic_ai_harness.shell:Shell` | verified | `ShellConfig` | `workspace` | next_turn/run |
| SSH workspace | `pydantic.ssh_workspace` | `pydantic_ai_harness.ssh_workspace:SSHWorkspace` | verified | `SSHWorkspaceConfig` | — | new_session/external |
| Modal sandbox | `pydantic.sandbox_modal` | `pydantic_ai_harness.modal_sandbox:ModalSandbox` | install_required | `SandboxConfig` | — | new_session/external |
| E2B sandbox | `pydantic.sandbox_e2b` | `pydantic_ai_harness.e2b_sandbox:E2BSandbox` | install_required | `SandboxConfig` | — | new_session/external |
| Sprites sandbox | `pydantic.sandbox_sprites` | `pydantic_ai_harness.sprites_sandbox:SpritesSandbox` | install_required | `SandboxConfig` | — | new_session/external |
| Smol sandbox | `pydantic.sandbox_smol` | `pydantic_ai_harness.smol_sandbox:SmolSandbox` | install_required | `SmolSandboxConfig` | — | new_session/external |
| Bubblewrap sandbox | `harness.sandbox_bubblewrap` | `pydantic_ai_harness.bubblewrap_sandbox:BubblewrapSandbox` | verified | `BubblewrapConfig` | `wrapped.workspace` | new_session/external |

### Tools and media

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| MCP (local transport) | `pydantic.mcp` | `pydantic_ai.capabilities:MCP` | install_required | `MCPConfig` | — | next_turn/run |
| MCP (provider-native) | `pydantic.mcp_native` | `pydantic_ai.capabilities:MCP` | install_required | `MCPNativeConfig` | — | next_turn/run |
| Image generation | `pydantic.image_generation` | `pydantic_ai.capabilities:ImageGeneration` | verified | `ImageGenerationConfig` | — | next_turn/run |
| LocalStack | `harness.localstack` | `pydantic_ai_harness.localstack:LocalStack` | verified | `LocalStackConfig` | — | next_turn/run |
| Macroscope review | `harness.macroscope` | `pydantic_ai_harness.macroscope:Macroscope` | verified | `MacroscopeConfig` | `workspace` | next_turn/run |

### Business integrations

All use the shared hosted-MCP pattern. Credentials never enter config: deployments provide the
vendor environment variable in the worker (or a host `auth` callable). `read_only` is not an
access boundary — token scopes are.

| Feature | Plugin id | Import target | Tier | Config | Credential (env) | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| GitHub | `harness.github` | `pydantic_ai_harness.github:GitHub` | install_required | `GitHubConfig` | `GITHUB_TOKEN` | next_turn/run |
| Linear | `harness.linear` | `pydantic_ai_harness.linear:Linear` | install_required | `HostedIntegrationConfig` | `LINEAR_ACCESS_TOKEN` | next_turn/run |
| Notion | `harness.notion` | `pydantic_ai_harness.notion:Notion` | install_required | `HostedIntegrationConfig` | `NOTION_ACCESS_TOKEN` | next_turn/run |
| Slack | `harness.slack` | `pydantic_ai_harness.slack:Slack` | install_required | `HostedIntegrationConfig` | `SLACK_USER_TOKEN` | next_turn/run |
| Google Workspace | `harness.google_workspace` | `pydantic_ai_harness.google_workspace:GoogleWorkspace` | install_required | `GoogleWorkspaceConfig` | `GOOGLE_ACCESS_TOKEN` | next_turn/run |
| Logfire MCP | `harness.logfire_mcp` | `pydantic_ai_harness.logfire_mcp:LogfireMCP` | install_required | `LogfireMCPConfig` | `LOGFIRE_API_KEY` | next_turn/run |
| Ordinal | `harness.ordinal` | `pydantic_ai_harness.ordinal:Ordinal` | install_required | `HostedIntegrationConfig` | `ORDINAL_ACCESS_TOKEN` | next_turn/run |
| Grain | `harness.grain` | `pydantic_ai_harness.grain:Grain` | install_required | `HostedIntegrationConfig` | `GRAIN_ACCESS_TOKEN` | next_turn/run |
| Day AI | `harness.day_ai` | `pydantic_ai_harness.day_ai:DayAI` | install_required | `HostedIntegrationConfig` | `DAY_AI_ACCESS_TOKEN` | next_turn/run |
| PostHog | `harness.posthog` | `pydantic_ai_harness.posthog:PostHog` | install_required | `PostHogConfig` | `POSTHOG_PERSONAL_API_KEY` | next_turn/run |
| Pylon | `harness.pylon` | `pydantic_ai_harness.pylon:Pylon` | install_required | `HostedIntegrationConfig` | `PYLON_ACCESS_TOKEN` | next_turn/run |
| StackOne | `harness.stackone` | `pydantic_ai_harness.stackone:StackOne` | install_required | `StackOneConfig` | `STACKONE_API_KEY` | next_turn/run |

Every row needs `pydantic-ai-harness[<extra>]` and that extra installs `pydantic-ai-slim[mcp]`.
`id` is configurable so two accounts stay distinguishable; identical tool names also need
`pydantic.prefix_tools` (or `PrefixTools` in Python).

### Web and research

| Feature | Plugin id | Import target | Tier | Config | Lifecycle |
| --- | --- | --- | --- | --- | --- |
| Web search | `pydantic.web_search` | `pydantic_ai.capabilities:WebSearch` | verified | `WebSearchConfig` | next_turn/run |
| Web fetch | `pydantic.web_fetch` | `pydantic_ai.capabilities:WebFetch` | verified | `WebFetchConfig` | next_turn/run |
| X search | `pydantic.x_search` | `pydantic_ai.capabilities:XSearch` | verified | `XSearchConfig` | next_turn/run |
| Exa search | `harness.exa_search` | `pydantic_ai_harness.exa:ExaSearch` | install_required | — (documented only) | next_turn/run |
| Exa agent | `harness.exa_agent` | `pydantic_ai_harness.exa:ExaAgent` | install_required | — (documented only) | next_turn/run |
| You.com search | `harness.you_search` | `pydantic_ai_harness.youdotcom:YouSearch` | install_required | — (documented only) | next_turn/run |
| You.com research | `harness.you_research` | `pydantic_ai_harness.youdotcom:YouResearch` | install_required | — (documented only) | next_turn/run |
| Browser Use | `harness.browser_use` | `pydantic_ai_harness.browser_use:BrowserUse` | install_required | `BrowserUseConfig` | new_session/external |
| Playwright browser | `harness.playwright_browser` | `pydantic_ai_harness.playwright:PlaywrightBrowser` | install_required | — (documented only) | new_session/external |

`pydantic.web_search` / `web_fetch` are managed for the native provider path; setting
`local=True` additionally requires `pydantic-ai-slim[duckduckgo]` / `[web-fetch]`. Their native
tool name (`web_search`) collides with Exa/You.com search; use one per agent or prefix one.

### Reasoning and delegation

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Planning | `harness.planning` | `pydantic_ai_harness.planning:Planning` | verified | `PlanningConfig` | opt. `plan.store` | next_turn/run |
| Sub-agents | `harness.subagents` | `pydantic_ai_harness.subagents:SubAgents` | verified | `SubAgentsConfig` | `subagents.agents` | next_turn/run |
| Dynamic workflow | `harness.dynamic_workflow` | `pydantic_ai_harness.dynamic_workflow:DynamicWorkflow` | install_required | `DynamicWorkflowConfig` | `dynamic.agents` | new_session/run |
| Advisor | `harness.advisor` | `pydantic_ai_harness.advisor:Advisor` | verified | `AdvisorConfig` | `model.advisor` | next_turn/run |
| Background tools | `harness.background_tools` | `pydantic_ai_harness.background_tools:BackgroundTools` | verified | `BackgroundToolsConfig` | — | next_turn/session |

### Context management

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Code Mode | `harness.code_mode` | `pydantic_ai_harness.code_mode:CodeMode` | install_required | — (documented only) | — | new_session/run |
| Tool search | `pydantic.tool_search` | `pydantic_ai.capabilities:ToolSearch` | verified | `ToolSearchConfig` | — | next_turn/run |
| Prompt caching | `pydantic.caching` | `pydantic_ai.capabilities:Caching` | verified | `CachingConfig` | — | next_turn/run |
| OpenAI native compaction | `pydantic.openai_compaction` | `pydantic_ai.models.openai:OpenAICompaction` | install_required | — (documented only) | — | new_session/run |
| Anthropic native compaction | `pydantic.anthropic_compaction` | `pydantic_ai.models.anthropic:AnthropicCompaction` | install_required | — (documented only) | — | new_session/run |
| Clear tool results | `harness.clear_tool_results` | `pydantic_ai_harness.compaction:ClearToolResults` | verified | `ClearToolResultsConfig` | — | next_turn/run |
| Sliding window | `harness.sliding_window` | `pydantic_ai_harness.compaction:SlidingWindowCompaction` | verified | `SlidingWindowConfig` | — | next_turn/run |
| Summarizing compaction | `harness.summarizing_compaction` | `pydantic_ai_harness.compaction:SummarizingCompaction` | verified | `SummarizingConfig` | — | next_turn/run |
| Tiered compaction | `harness.tiered_compaction` | `pydantic_ai_harness.compaction:TieredCompaction` | verified | `TieredConfig` | `compaction.tiers` | next_turn/run |
| Fallback compaction | `harness.fallback_compaction` | `pydantic_ai_harness.compaction:FallbackCompaction` | verified | `FallbackCompactionConfig` | `compaction.fallback_chain` | next_turn/run |
| Clamp oversized messages | `harness.clamp_oversized` | `pydantic_ai_harness.compaction:ClampOversizedMessages` | verified | `ClampOversizedConfig` | — | next_turn/run |
| Deduplicate file reads | `harness.deduplicate_reads` | `pydantic_ai_harness.compaction:DeduplicateFileReads` | verified | `DeduplicateReadsConfig` | `callbacks.file_key` | next_turn/run |
| Warn near limits | `harness.warn_near_limits` | `pydantic_ai_harness.compaction:WarnNearLimits` | verified | `WarnNearLimitsConfig` | — | next_turn/run |
| Report context usage | `harness.report_context_usage` | `pydantic_ai_harness.compaction:ReportContextUsage` | verified | `ReportContextUsageConfig` | — | next_turn/run |
| Tool output limits | `harness.tool_output_limits` | `pydantic_ai_harness.tool_output_limits:ToolOutputLimits` | verified | `ToolOutputLimitsConfig` | opt. `tool_overflow.store` / `workspace` | next_turn/run |
| Warn on cache busts | `harness.warn_cache_busts` | `pydantic_ai_harness.warn_on_cache_busts:WarnOnCacheBusts` | verified | `WarnOnCacheBustsConfig` | — | next_turn/run |

### Knowledge and memory

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Memory | `harness.memory` | `pydantic_ai_harness.memory:Memory` | verified | `MemoryConfig` | `memory.store` | next_turn/external |
| Conversation search | `harness.conversation_search` | `pydantic_ai_harness.conversation_search:ConversationSearch` | verified | `ConversationSearchConfig` | `conversations` | next_turn/external |
| Agent Skills | `harness.skills` | `pydantic_ai_harness.skills:Skills` | verified | `SkillsConfig` | `workspace` | next_turn/run |
| Repository context | `harness.repo_context` | `pydantic_ai_harness.repo_context:RepoContext` | verified | `RepoContextConfig` | `workspace` | next_turn/run |
| Pydantic AI docs | `harness.pydantic_ai_docs` | `pydantic_ai_harness.pydantic_ai_docs:PydanticAIDocs` | verified | `PydanticAIDocsConfig` | opt. `workspace` | next_turn/run |

`harness.skills` is verified because the `harness[runtime]` extra already depends on PyYAML; the upstream
`[skills]` extra exists for other distributions. Memory content and skill bodies are
model-visible untrusted data: bind trusted stores and libraries only.

### Interaction and controls

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Input guardrail | `harness.input_guardrail` | `pydantic_ai_harness.guardrails:InputGuardrail` | verified | `InputGuardrailConfig` | — | next_turn/run |
| Output guardrail | `harness.output_guardrail` | `pydantic_ai_harness.guardrails:OutputGuardrail` | verified | `OutputGuardrailConfig` | — | next_turn/run |
| Tool guardrail | `harness.tool_guardrail` | `pydantic_ai_harness.guardrails:ToolGuardrail` | verified | `ToolGuardrailConfig` | opt. `callbacks.tool_guardrail` | next_turn/run |
| Prompt-injection defender | `harness.prompt_injection_defender` | `pydantic_ai_harness.prompt_injection_defender:PromptInjectionDefender` | install_required | `PromptInjectionDefenderConfig` | opt. `defense` | next_turn/run |
| Tool-call judge | `harness.tool_call_judge` | `pydantic_ai_harness.tool_call_judge:ToolCallJudge` | verified | `ToolCallJudgeConfig` | `model.judge` | next_turn/run |
| Approval reviewer | `harness.approval_reviewer` | `pydantic_ai_harness.approval_reviewer:ApprovalReviewer` | verified | `ApprovalReviewerConfig` | `model.judge` | next_turn/run |
| Spend limits | `harness.spend_limits` | `pydantic_ai_harness.spend:SpendLimits` | verified | `SpendLimitsConfig` | opt. `spend.store` | next_turn/external |
| Ask user | `harness.ask_user` | `pydantic_ai_harness.ask_user:AskUser` | verified | `AskUserConfig` | `user_answers` | next_turn/run |
| System reminders | `harness.system_reminders` | `pydantic_ai_harness.system_reminders:SystemReminders` | verified | `SystemRemindersConfig` | opt. callbacks | next_turn/run |
| Trajectory judge | `harness.trajectory_judge` | `pydantic_ai_harness.trajectory_judge:TrajectoryJudge` | verified | `TrajectoryJudgeConfig` | `model.judge` | next_turn/run |
| Repair tool arguments | `harness.repair_tool_arguments` | `pydantic_ai_harness.repair_tool_arguments:RepairToolArguments` | verified | `_NoConfig` | — | next_turn/run |
| Deferred tool-call handler | `pydantic.handle_deferred_tool_calls` | `pydantic_ai.capabilities:HandleDeferredToolCalls` | verified | `DeferredToolCallsConfig` | `deferred.handler` | next_turn/run |

Deferred *approvals* themselves are a core Pydantic AI mechanism (`requires_approval=True`,
`DeferredToolRequests`, `DeferredToolResults`); the catalog manages the handler binding so the
ACP host can answer permission prompts. There is no silent auto-approval path: without a
handler resource, composing this plugin fails.

### Self-extension

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Runtime capability creation | `harness.capability_creation` | `pydantic_ai_harness.capability_creation:CapabilityCreation` | verified | `CapabilityCreationConfig` | `workspace` | new_session/external |

Opt-in only and absent from both starter definitions (`disabled_in_starters=True`). Authored
code executes in the worker process; a deployment that enables it explicitly authorizes that
host-side code loading. It cannot install packages or expand a remote client's rights.

### Execution services

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Instrumentation | `pydantic.instrumentation` | `pydantic_ai.capabilities:Instrumentation` | verified | `InstrumentationConfig` | — | new_session/run |
| Step persistence | `harness.step_persistence` | `pydantic_ai_harness.step_persistence:StepPersistence` | verified | `StepPersistenceConfig` | `step.store` | new_session/external |
| Thread executor | `pydantic.use_thread_executor` | `pydantic_ai.capabilities:UseThreadExecutor` | verified | `_NoConfig` | `thread.executor` | new_session/run |
| Managed prompt | `harness.managed_prompt` | `pydantic_ai_harness.logfire:ManagedPrompt` | install_required | `ManagedPromptConfig` | — | new_session/external |
| Disk media store | `harness.media.disk` | `pydantic_ai_harness.media:DiskMediaStore` | verified | `DiskMediaStoreConfig` | — | resource |
| SQLite media store | `harness.media.sqlite` | `pydantic_ai_harness.media:SqliteMediaStore` | verified | `SqliteMediaStoreConfig` | — | resource |
| S3 media store | `harness.media.s3` | `pydantic_ai_harness.media:S3MediaStore` | verified | `S3MediaStoreConfig` | `media.s3_credentials` | resource |
| Postgres media store | `harness.media.postgres` | `pydantic_ai_harness.media:PostgresMediaStore` | verified | `PostgresMediaStoreConfig` | `media.postgres_pool` | resource |

### Durability

| Feature | Plugin id | Import target | Tier | Config | Lifecycle |
| --- | --- | --- | --- | --- | --- |
| Temporal | `pydantic.temporal_durability` | `pydantic_ai.durable_exec.temporal:TemporalDurability` | install_required | `DurabilityConfig` | new_session/external |
| DBOS | `pydantic.dbos_durability` | `pydantic_ai.durable_exec.dbos:DBOSDurability` | install_required | `DurabilityConfig` | new_session/external |
| Prefect | `pydantic.prefect_durability` | `pydantic_ai.durable_exec.prefect:PrefectDurability` | install_required | `DurabilityConfig` | new_session/external |
| AWS Lambda | `harness.aws_lambda_durability` | `pydantic_ai_harness.aws_lambda:AWSLambdaDurability` | install_required | `DurabilityConfig` | new_session/external |
| Absurd | `harness.absurd_durability` | `pydantic_ai_harness.absurd:AbsurdDurability` | install_required | `ParallelDurabilityConfig` | new_session/external |
| pgtask | `harness.pgtask_durability` | `pydantic_ai_harness.pgtask:PGTaskDurability` | install_required | `ParallelDurabilityConfig` | new_session/external |
| Render Workflows | `harness.render_workflows` | `pydantic_ai_harness.render:RenderWorkflows` | install_required | `_NoConfig` | new_session/external |
| Restate | `pydantic.restate_durability` | — | **unavailable** | — | — |
| Kitaru | `pydantic.kitaru_durability` | — | **unavailable** | — | — |
| Airflow | `pydantic.airflow_durability` | — | **unavailable** | — | — |

Durability is deployment-bound: dynamic model selection and workspace configuration have
upstream durable-execution constraints; compositions are preregistered and pinned per session.
The three unavailable engines have no module in `pydantic-ai-slim 2.57.0` and are deliberately
not registered, so a definition referencing them fails instead of pretending.

### Loop customization

| Feature | Plugin id | Import target | Tier | Config | Resources | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- |
| Thinking | `pydantic.thinking` | `pydantic_ai.capabilities:Thinking` | verified | `ThinkingConfig` | — | next_turn/run |
| Hooks | `pydantic.hooks` | `pydantic_ai.capabilities:Hooks` | verified | `_NoConfig` | `hooks` | next_turn/run |
| Select model | `pydantic.select_model` | `pydantic_ai.capabilities:SelectModel` | verified | `_NoConfig` | `callbacks.model_selector` | new_session/run |
| Resolve model id | `pydantic.resolve_model_id` | `pydantic_ai.capabilities:ResolveModelId` | verified | `_NoConfig` | `callbacks.model_id_resolver` | new_session/run |
| Prepare tools | `pydantic.prepare_tools` | `pydantic_ai.capabilities:PrepareTools` | verified | `_NoConfig` | `callbacks.prepare_tools` | next_turn/run |
| Prepare output tools | `pydantic.prepare_output_tools` | `pydantic_ai.capabilities:PrepareOutputTools` | verified | `_NoConfig` | `callbacks.prepare_tools` | next_turn/run |
| Prefix tools | `pydantic.prefix_tools` | `pydantic_ai.capabilities:PrefixTools` | verified | `PrefixToolsConfig` | `wrapped.capability` | next_turn/run |
| Include tool return schemas | `pydantic.include_return_schemas` | `pydantic_ai.capabilities:IncludeToolReturnSchemas` | verified | `IncludeReturnSchemasConfig` | — | next_turn/run |
| Set tool metadata | `pydantic.set_tool_metadata` | `pydantic_ai.capabilities:SetToolMetadata` | verified | `SetToolMetadataConfig` | — | next_turn/run |
| Raise content-filter errors | `pydantic.raise_content_filter_error` | `pydantic_ai.capabilities:RaiseContentFilterError` | verified | `_NoConfig` | — | next_turn/run |
| Reinject system prompt | `pydantic.reinject_system_prompt` | `pydantic_ai.capabilities:ReinjectSystemPrompt` | verified | `ReinjectSystemPromptConfig` | — | next_turn/run |
| Process history | `pydantic.process_history` | `pydantic_ai.capabilities:ProcessHistory` | verified | `_NoConfig` | `callbacks.history` | next_turn/run |
| Process event stream | `pydantic.process_event_stream` | `pydantic_ai.capabilities:ProcessEventStream` | verified | `_NoConfig` | `callbacks.event_stream` | next_turn/run |

## Resource references

Definitions declare the resource names below; deployments bind host values
(`ResourceSet`). Secrets belong in resources, never manifests or wire DTOs. A missing required
resource raises `WORKSPACE_UNAVAILABLE` / `RESOURCE_UNAVAILABLE`; there is no host fallback.

Catalog plugins record them as `deployment_resources` (see
`catalog.resource_requirements(entry)`), **not** as `PluginDescriptor.requirements`: the
runtime policy treats descriptor requirements as *binding-id* dependencies, so publishing a
resource name there would create a false edge when a binding is named after its resource
(for example a binding called `workspace`). Resource needs are declared on the definition
(`AgentDefinition.resources`) and enforced by the plugin factory. This also means the ACP
wire's `requirements` field reports binding dependencies only; clients read resource needs
from the definition/deployment catalog.

| Resource | Kind | Used by | Expected value |
| --- | --- | --- | --- |
| `model.default` | model | definitions (model resolution) | provider string or native `Model` |
| `workspace` | workspace | Local workspace, FileSystem, Shell, Coder, Researcher, Macroscope, Skills, RepoContext, CapabilityCreation, ToolOutputLimits (spill) | path, `LocalWorkspaceBackend`, or a bound workspace backend |
| `wrapped.workspace` | service | Bubblewrap sandbox | a workspace capability instance (`LocalWorkspace`/`SSHWorkspace`) |
| `wrapped.capability` | service | Prefix tools | the native capability to wrap |
| `memory.store` | memory | Memory | `FileStore`, `SqliteMemoryStore`, `PostgresMemoryStore`, custom |
| `plan.store` | store | Planning (`store=resource`) | durable `PlanStore` |
| `spend.store` | store | Spend limits (`store=resource`) | `BatchSpendStore`/`SpendStore` |
| `step.store` | store | Step persistence | `FileStepStore`, `SqliteStepStore`, `PostgresStepStore`, … |
| `conversations` | store | Conversation search | `SnapshotHistorySource(store)` |
| `tool_overflow.store` | store | Tool output limits (`store=resource`) | upstream `OverflowStore` |
| `model.judge` | model | Tool-call judge, approval reviewer, trajectory judge | `Model` or provider string |
| `model.advisor` | model | Advisor | `Model` or provider string |
| `user_answers` | service | Ask user | async `(AskUserRequest) -> AskUserResponse` |
| `deferred.handler` | service | Deferred tool-call handler | host approval callback |
| `image.generator` | service | Image generation (`local=True`) | upstream `ImageGenerator` |
| `subagents.agents` | service | Sub-agents | named `SubAgent` objects |
| `dynamic.agents` | service | Dynamic workflow | named agents |
| `hooks` | custom | Hooks plugin | deployment-built `Hooks` instance |
| `compaction.tiers` / `compaction.fallback_chain` | custom | Tiered/fallback compaction | strategy instances |
| `callbacks.*` | custom | Loop customization, dedupe, guardrails, reminders | live callables |
| `thread.executor` | service | Thread executor | `concurrent.futures.Executor` |
| `defense` | service | Prompt-injection defender | configured `PromptDefense` |
| `render.app` | service | Render Workflows | live Render app |
| `media.s3_credentials` / `media.postgres_pool` | service | Media store factories | secret mapping / async pool |

## Documented gaps (install path exists, no managed descriptor yet)

These entries are in the inventory with a verified module path but intentionally expose no
`config_model`/`factory` in this release. They remain usable through normal Python composition
(`Agent(capabilities=[...])`); they are **not** advertised as managed plugins:

- `harness.exa_search`, `harness.exa_agent` — need `pydantic-ai-harness[exa]` + `EXA_API_KEY`.
- `harness.you_search`, `harness.you_research` — need `[youdotcom]` + `YDC_API_KEY`.
- `harness.playwright_browser` — needs `[playwright]` + `playwright install chromium`.
- `harness.code_mode` — needs `pydantic-monty` (`[codemode]`).
- `pydantic.openai_compaction`, `pydantic.anthropic_compaction` — provider modules with several
  constructor modes; not modeled as manifests yet.

Provider-native *tools* (raw `WebSearchTool`, `CodeExecutionTool`, …) are likewise model-level
objects, not standalone managed capabilities; the managed surface is the core
`pydantic.web_search` / `web_fetch` / `image_generation` entries.

## Starters

Both starters are ordinary `AgentDefinition` objects in `harness.runtime.starters`; the editable
YAML copies live in `examples/starters/`. Manifest loading validates every plugin id and
configuration against this catalog.

| Binding | Developer default | Research default | Notes |
| --- | --- | --- | --- |
| `workspace` | on, locked | — | Developer *requires* the resource; no host fallback |
| `filesystem`, `shell` | on | — | require the workspace binding |
| `planning` | on | on | run-scoped plan by default |
| `repo_context` | on | — | CLAUDE.md/AGENTS.md |
| `skills` | on (`missing_directories: skip`) | — | trusted libraries only |
| `context_clear_tool_results` | on | on | zero-LLM trimming |
| `context_tool_output_limits` | on (workspace spill not enabled; truncate band) | on (`store: local_file`) | no silent store fallback |
| `context_summarize` | off | — | model-written summary |
| `search`, `fetch` | off | on | Research manifest; need native provider or local extras |
| `memory` | off | off | bind `memory.store` |
| `subagents` | off | off | bind `subagents.agents` |
| `deferred_approvals` | off | — | bind `deferred.handler` |
| `mcp` | off | off | needs `pydantic-ai-slim[mcp]` |
| `browser` | — | off | Browser Use; needs `[browser-use]` |
| Runtime capability creation | absent | absent | opt-in feature; disabled by design |

## Examples

- `examples/custom_capability.py` — a custom `AbstractCapability` subclass with a validated
  config model, descriptor, registry registration and a real configuration transaction.
- `examples/starters/developer.yaml`, `examples/starters/research.yaml` — editable manifests.
- `examples/README.md` — run instructions and integration prerequisites.

## Verification evidence

| Test | What it proves |
| --- | --- |
| `tests/test_catalog.py::test_verified_entries_construct_offline` | 62 runtime capabilities construct with real config models and offline resources. |
| `tests/test_catalog.py::test_catalog_import_loads_no_optional_integrations` | Subprocess import of the catalog loads zero optional packages. |
| `tests/test_catalog.py::test_install_required_entries_fail_closed_through_the_registry` | Missing extras raise `CAPABILITY_UNAVAILABLE` with install guidance; no downgrade. |
| `tests/test_catalog.py::test_unavailable_entries_are_not_registered` | Restate/Kitaru/Airflow are explicit gaps, not fake plugins. |
| `tests/test_catalog.py::test_management_config_validation_is_real` | Unknown fields, bad ranges, conflicting triggers and missing required values are rejected. |
| `tests/test_catalog_factories.py` (17 tests) | Resource refs, exact-store binding, spill modes, detector chains, judge models, spend budgets, media-store resources, schema/descriptor round trips. |
| `tests/test_catalog_docs.py` (5 tests) | This document's inventory rows, import targets, tiers, lifecycle, configuration models and published totals match the executable catalog. |

Integration still required for `install_required` entries: install the named extra, provide
vendor credentials/accounts (see the hosted table), satisfy platform prerequisites
(`bwrap`, `/dev/kvm`, Playwright Chromium, Docker for LocalStack), and run a focused provider
check. Catalog presence alone is not proof of compatibility.

## Adding a managed descriptor

1. Add one `_entry(...)` in `catalog.py` with a stable `plugin_id`, the real
   `module:attr` import in the pinned release, a `CatalogGroup`, and the honest tier.
2. Write a strict `_Config` model (`extra="forbid"`) for every non-secret setting; required
   fields must have no default so bad manifests fail.
3. Map non-serializable dependencies to named resources; never create a hidden store, client,
   workspace, or default execution location.
4. Implement a lazy factory `(config, binding) -> AbstractCapability`; import the upstream
   module inside the factory and translate `ImportError` into an install hint.
5. Choose lifecycle: `next_turn` only when omitting the binding is safe mid-session; otherwise
   `new_session`. State is `run`, `session`, or `external` as appropriate.
6. Add tests: offline construction (verified), missing-extra failure (install required),
   config rejection, and resource-missing behavior.
