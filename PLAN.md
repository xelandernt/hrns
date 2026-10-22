# Implementation plan: plugin commands and Textual presentation

Status: implemented through Tasks 01–14 and 17–18; Task 15 remote hosting walkthrough and Task 16 (writable history) remain outstanding/deferred. See [tasks/todo.md](tasks/todo.md) and [docs/VERIFICATION.md](docs/VERIFICATION.md) for the executed scope.

Researched on 2026-10-22 against the installed dependencies and current checkout. This file is the design and detailed implementation plan requested by the user. Execution progress belongs in [tasks/todo.md](tasks/todo.md); there is no duplicate `tasks/plan.md`.

## 1. Confirmed intent

- A plugin packages a native Pydantic AI capability, optional user commands, and optional local Textual views.
- Commands use a namespace, for example `/git:status`. A command and an agent tool are independent contributions, even if their author shares implementation code internally.
- A direct command produces its own inline response, similar to Codex's `/status`. It is not presented as an agent tool call.
- Commands and their output are user-only by default. A command must explicitly request an interaction with the conversation before anything enters model history or starts a model run.
- Commands can also request existing conversation operations. Unsupported operations must be identified rather than implemented through private upstream state.
- Tool views can customize inputs, outputs, and the execution state delivered to the client, using standard Textual widgets and interactions.
- Harness maintains a small generic integration. Plugin authors own their command handlers, widgets, styling, and interactions. Installing another plugin must not require plugin-specific Harness code.
- A remote host supplies JSON data and presentation identifiers. Its custom UI component must be installed locally; otherwise the client shows a readable generic display.
- Plugin screens and dialogs are out of scope. UI remains inline. There is no new UI framework, plugin marketplace, auto-installer, or compatibility layer in this work.
- Do not write new tests. Use source inspection, the existing static/build gates, and focused manual/offline verification. Do not implement runtime changes while executing this planning request.

## 2. Baseline and evidence

Installed versions, verified in `.venv`: `pydantic-ai-slim==2.57.0`, `pydantic-ai-harness==0.57.0`, `agent-client-protocol==0.11.1`, `textual==8.2.8`.

The checkout has already migrated execution to `PydanticAIACPAgent`. An older migration note is not the current implementation. `HarnessACPAgent` supplies composition, scoped persistence, managed configuration, and metadata; the dependency owns model runs, tools, approval continuations, cancellation, history, and transcript replay. Preserve that boundary.

Existing uncommitted edits were present in `docs/CLIENT_API.md` and `src/harness/client/tui/{app,screens,state,widgets}.py`, plus untracked `docs/EDITING.md`. They are outside this planning change. Future implementation must integrate with their current contents and preserve unrelated edits.

| Existing surface | Finding | Consequence |
| --- | --- | --- |
| `runtime/registry.py:PluginDescriptor` | Native capability factory, validated config, policy, and presentation IDs; no command handlers. | Extend the descriptor rather than require a second capability superclass. |
| `PluginRegistry.discover` | `harness.capabilities` entry points currently denote capability factories; discovery does not load full plugin descriptors. | Installed plugins need an explicit descriptor-provider contract to contribute commands as well as native behavior. |
| `runtime/policy.py:PolicyCatalog.compose` | Produces `(binding_id, native_capability)` pairs in enabled order. | Bind command ownership to the same selection and revision. |
| `runtime/composition.py:Composition` | Holds the session agent/configuration/resources; configuration changes replace its contents in place. | Store extension bindings here and refresh them with the native composition. |
| `runtime/host.py:HarnessACPAgent` | Uses public upstream methods, `session_config`, and `session_store`; has a per-session prompt/configuration lock. | Add application commands without another agent execution loop. |
| `client/tui/screens.py:_send_agent_command` | Advertised agent commands currently become ordinary prompts. | Plugin commands need a separate dispatch route to preserve user-only behavior. |
| `client/renderers.py` | Local lazy discovery under `harness.renderers`; adapters return strings. | Replace the string-only extension contract with widget adapters; retain generic text fallback. |
| `client/tui/widgets.py:TranscriptPane` | Bounded blocks, tool-call registry, replay, search, and copy behavior. | Command output needs its own block type; plugin widgets must participate in retention and cleanup. |
| `protocol/` | Depends on neither Pydantic AI nor Textual; negotiated `_harness/` methods already exist. | Put only JSON contracts here; keep executable callbacks and widgets on their respective sides. |

### Documentation and source consulted

The live documentation explains the framework direction; the installed source is authoritative for availability on these pins. Some live examples contain newer behavior. Do not add APIs merely because they appear on `main`.

1. [Pydantic documentation index](https://pydantic.dev/docs/llms.txt) and [AI page index](https://pydantic.dev/docs/ai/llms.txt): used to identify relevant capability, history, tool, and ACP documentation.
2. [Custom capabilities](https://pydantic.dev/docs/ai/capabilities/custom/): native configuration, lifecycle, tool metadata hooks, event observation, and run isolation. Relevant installed modules: `pydantic_ai.capabilities.abstract` and `pydantic_ai._run_context` (public `RunContext` members only).
3. [Capability events](https://pydantic.dev/docs/ai/capabilities/overview/#capability-events) and [on-demand capabilities](https://pydantic.dev/docs/ai/capabilities/on-demand/): distinguish model loading from host selection; preserve native event ownership. Installed `CapabilityEvent`, `RunContext.emit`, and `RunContext.tools` are available.
4. [Advanced tool returns](https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/#advanced-tool-returns): `ToolReturn.metadata` is application data; `return_value` and `content` have model-facing meanings. Installed `pydantic_ai.messages` confirms the separation.
5. [Messages and history](https://pydantic.dev/docs/ai/core-concepts/message-history/#editing-existing-messages): native history transformations use new message/part objects. Native APIs alone do not establish that ACP exposes writable session history.
6. [Manual compaction](https://pydantic.dev/docs/ai/harness/compaction/#compacting-outside-a-run-compact_now): installed `pydantic_ai_harness.compaction.compact_now` can compute replacement messages outside a run, but installing them into an active ACP session is a separate requirement.
7. [Pydantic ACP integration](https://pydantic.dev/docs/ai/harness/acp/) and [its repository documentation](https://github.com/pydantic/pydantic-ai/blob/main/docs/harness/acp.md): session configuration, presentation, persistence, and current limitations. Installed `experimental.acp.{_adapter,_presentation,_session,_store}` inspected directly.
8. [ACP v1 slash commands](https://agentclientprotocol.com/protocol/v1/slash-commands), [tool calls](https://agentclientprotocol.com/protocol/v1/tool-calls), and [extensions](https://agentclientprotocol.com/protocol/v1/extensibility): standard slash commands travel through `session/prompt`; tool-call IDs identify calls within a session; custom requests/notifications and `_meta` are the extension mechanisms. Do not adopt the v2 draft for this feature or assume newer optional fields exist in the installed SDK.
9. Textual [widgets](https://textual.textualize.io/guide/widgets/), [events/messages](https://textual.textualize.io/guide/events/), and [workers](https://textual.textualize.io/guide/workers/): reuse widget composition, mounting, messaging, and worker lifetimes rather than implement parallel UI lifecycle machinery.

### Important upstream limits

**Commands:** `AbstractCapability` has no upstream command-registration hook, and the pinned ACP adapter does not implement slash-command advertisement/handling. Command definitions and dispatch are Harness application integration.

**Tool presentation:** the public `tool_presenter` callback receives `ToolCallPart` and returns `ToolCallPresentation` with title/kind/locations/content. It does not receive results or expose arbitrary tool `_meta`. The adapter emits `raw_input`/`raw_output`, but does not forward `ToolDefinition.metadata`, `ToolReturnPart.metadata`, or arbitrary capability events to ACP. Therefore `tool_presenter` alone cannot implement the requested widget API.

**History:** `PydanticAIACPAgent.session_history()` returns a read-only shallow snapshot. There is no public setter/replacement operation on the pinned adapter. Changing its returned objects is explicitly unsupported. `Conversation`, history processors, and `compact_now` do not solve installation into an already-resident ACP session.

## 3. Recommended architecture

### A. Native capabilities remain native

Keep `PluginDescriptor.factory -> AbstractCapability` intact as the behavioral boundary. Add optional command definitions to `PluginDescriptor`; their handlers receive validated plugin configuration through a host-owned `CommandContext`.

Do not add `get_commands()` or `render()` methods to Pydantic AI classes, instantiate a fake `RunContext` for commands, call tool functions through the tool registry, or manually invoke native `for_run()`/hook lifecycles. Commands are host application actions and use their own context. Shared business logic belongs to the plugin.

For package discovery, make `harness.capabilities` entry points point to a lightweight zero-argument descriptor provider. Load the provider lazily when its descriptor is needed; its native factory remains a lazy callable/import reference. The provider module must not import Textual or optional provider SDKs. Update examples/docs to this single contract; do not support both the former entry-point factory convention and the descriptor convention.

Programmatic `PluginDescriptor` registration remains the same public concept. A descriptor with no commands still contributes only its native capability.

### B. Names identify the configured binding

Use `/<binding_id>:<command_name>`. For a binding named `git`, the result is `/git:status`. A second account/binding has its own namespace, independent of the package's publisher-qualified `plugin_id` or upstream run-local capability ID.

Keep structured `binding_id` and local `command_name` on the wire. Local command names cannot contain `:` or whitespace; resolve the displayed name using the final `:` because existing binding IDs may themselves contain colons. Validate bounded ASCII names and reject ambiguous case-folded duplicates rather than silently slugifying them. Do not add unqualified aliases or first-plugin-wins resolution.

Commands are listed only for enabled, available bindings in the active composition. Refresh after new/load and successful configuration changes. A stale picker entry must fail at host dispatch when its binding is disabled or its revision has changed. Upstream deferred loading is a different concern: a user command does not have to ask the model to load the capability first.

### C. Separate authoring contracts from wire contracts

Proposed modules/names, to be finalized in Task 01:

| Contract | Location | Purpose |
| --- | --- | --- |
| `Command[ArgsT]` | `harness.runtime.extensions` | Name, help, argument model/parser, async handler. |
| `CommandContext` | `harness.runtime.extensions` | Session/binding identity, validated config, authorized resources, copied read-only session information, supported conversation operations. |
| `CommandResult` | `harness.protocol.extensions` | Optional presentation plus at most one explicit conversation action. |
| `CommandDescriptor`, invoke/cancel requests | `harness.protocol.extensions` | Serializable discovery and dispatch; never callbacks or import references. |
| `PresentationPayload` | existing protocol model | Renderer ID, payload schema version, JSON data, required fallback. |
| `ToolPresentationSnapshot` | `harness.protocol.extensions` | Tool-call identity, binding identity where known, input/output presentations, optional progress presentation. |
| `CommandViewState`, `ToolViewState` | `harness.client.tui.extensions` | Distinct typed snapshots for the two UI contributions. |
| `ViewHandle[StateT]` | `harness.client.tui.extensions` | Ordinary Textual `Widget` plus a synchronous `apply(state)` callback. Textual owns its lifecycle. |
| `ViewContext` | `harness.client.tui.extensions` | Submit a structured plugin command through the normal command route; includes session/replay state. No internal App/Screen access contract. |

Use Pydantic validation at the RPC/input boundaries, not repeated ad hoc validation through every internal call. `CommandContext` is not serializable and is never sent to clients. Neither the protocol nor runtime SDK imports Textual.

Arguments: default to a small `TextArguments(text: str)` model. A plugin may supply a parser from slash-tail text to a mapping and an `args_model` to validate it. Widget callbacks submit structured arguments directly, validated against the same model. Do not build a universal CLI flag parser or schema-generated form system.

Handlers are async. The host constructs a context from the current binding/configuration/resources; no private composition, adapter, connection, or mutable message objects escape through it. A history-reading helper returns a deep snapshot through public `session_history`, not the adapter's shared message objects.

### D. Direct commands use negotiated extension requests

Add a small family under the existing negotiated `_harness/` surface:

- `_harness/commands/list`: current descriptors, active configuration revision, and supported conversation operations.
- `_harness/commands/invoke`: session ID, binding ID, command name, request ID, expected revision, and exactly one of raw text or structured arguments.
- `_harness/commands/cancel`: cancel one known invocation, including one waiting to run.
- `_harness/commands/changed`: invalidate/replace the current command catalog after composition changes.
- `_harness/presentation/updated`: supplemental tool presentation snapshots keyed by session/tool-call identity, with a native run ID when available and a monotonic snapshot revision.
- `_harness/presentation/get`: obtain retained final presentations for a bounded set of replayed tool calls.

Use the protocol package's method catalog as the source of truth; remove the host's duplicate literal list while extending it. All methods must negotiate before use, with identical semantics over stdio and WebSocket hosting.

Do not advertise these application commands through ordinary `available_commands_update`: an ordinary ACP client would send them as prompts, violating their user-only default. The TUI merges local commands, negotiated plugin commands, and standard agent-advertised commands for discovery, but preserves their distinct dispatch routes. Existing standard agent commands continue to use ACP prompts as required by that protocol; that is interoperability, not a compatibility shim for the new plugin API.

Host dispatch rechecks session/principal/deployment ownership, binding availability, expected active revision, and argument validation. It uses the existing prompt/configuration lock for execution. Initially reject new plugin invocations with a clear busy response while the session is running a turn or another plugin command; do not create another work queue or background-job service. Built-in local UI commands retain their existing behavior.

Track invocation tasks and a bounded in-memory request-ID/result cache. A matching duplicate joins/replays the same invocation; a reused ID with different arguments fails. Check inside the serialized section as well as before waiting. Do not automatically retry after a disconnect or promise durable exactly-once side effects. Cancel before closing/replacing the resources an invocation uses; avoid acquiring the same session lock recursively.

A command is an explicit user action, not an agent tool call. Authorization and workspace/resource boundaries still apply. It does not inherit a fabricated tool approval, execute through Pydantic's tool machinery, or populate the agent's tool transcript.

### E. Presentation and conversation effects are independent

Conceptual return values:

```python
# User-only inline output; no model run and no model history entry.
CommandResult(presentation=PresentationPayload(
    renderer_id="example.git.status",
    schema_version=1,
    data=status_data,
    fallback=status_text,
))

# A deliberate conversation interaction; the command's UI output is still separate.
CommandResult(conversation=StartTurn(prompt="Review the current changes."))
```

The first release supports `StartTurn` through the existing client prompt path and read-only history/session inspection through the context. The client consumes the explicit action once through its existing turn coordinator; normal model execution, usage/cost reporting, approvals, and cancellation remain upstream-owned. Do not call `prompt()` while holding the command execution lock. Preserve the intended prompt exactly once, and do not accidentally attach files staged for a different prompt. A result restored for display cannot start another turn.

Represent only supported conversation actions. Do not add placeholder `ClearConversation`, `CompactConversation`, or `EditHistory` classes that claim to work. If a future operation is unavailable, fail before the command's side effects, using the required-operation declarations on its descriptor.

Direct-command invocation/output appears as a distinct user-facing command block. It is not a model user message, assistant reply, or tool event. It does not affect model token/cost totals, automatic session titles, or “copy latest assistant response.” Its fallback text participates in transcript search/copy. Retain it only with the current bounded UI session; durable command-output replay is not introduced here. A full agent `session/load` may reconstruct only the upstream transcript. Document that distinction rather than add a command journal.

### F. Bridge presentation through public capability events

Use `ToolDefinition.metadata` to declare presentation identity for inputs, and `ToolReturn.metadata['harness']['presentation']` for final input/output presentation snapshots. Payloads use `PresentationPayload`; never hide UI-only data in `ToolReturn.return_value` or `ToolReturn.content`, both of which may reach the model.

A single host-owned, always-on native observer capability listens to public function-tool call/result events and, optionally, one typed presentation-progress `CapabilityEvent`. It resolves final exposed tool definitions through public `RunContext.tools`, so tool prefixes and dynamic/deferred tools do not require guessing from display titles. Native capability IDs supplied by emitted events are used only when stable and explicitly mapped to the configured binding; the observer must not invent ownership.

The observer publishes validated, bounded snapshots through `_harness/presentation/updated`, using a session-scoped sink supplied at composition. It leaves the original events, tool results, hooks, approval handling, and native history unchanged. It does not wrap the adapter's private emitter or copy its translation/run loop. The application of this observer to the composed agent must be verified for both construction-bound and run-bound managed capabilities.

Ordinary ACP `ToolEvent`s remain the source of status, arguments, content, and raw outputs. Supplemental snapshots supply identity and plugin-specific data. Join the streams on `(session_id, tool_call_id)`, the identity both actually carry; the pinned standard tool updates do not carry the native run ID. Retain a supplemental run ID for diagnostics/staleness checks rather than require it on a standard event. Reject conflicting reuse of a session's tool-call ID rather than guess which card to update. Use a monotonic revision per supplemental snapshot so late progress cannot replace a newer/final presentation. Never join on title or arrival position. Bound the pending map because the supplemental event can arrive before the standard tool-call start. An unavailable/failing presentation sink must not turn a successful tool call into a failed model run.

For optional custom progress, define a small typed `CapabilityEvent` containing a `presentation` field; do not call it `data`, which is reserved by Pydantic's capability event envelope. Plugins emit it using public `ctx.emit()`. Do not build an arbitrary event subscription bus or map every upstream capability's domain events in Harness.

### G. Reuse native persistence; keep replay passive

`ToolReturnPart.metadata` is already part of stored native messages. Require final presentation metadata to be self-contained, including the input/output renderer IDs needed to reconstruct that completed view.

After `session/load`, request final snapshots for the retained tool-call IDs through `_harness/presentation/get`. The host extracts them from its public, read-only committed history snapshot. Merge them into replayed tool cards. Do not query or re-run tools, handlers, or model requests to rebuild the display, and do not import a plugin's native capability in the TUI.

Intermediate progress is live-only. If a cancelled/uncommitted turn, history compaction, absent metadata, missing plugin, or unknown schema leaves no reconstructable custom snapshot, use the ordinary ACP input/output and readable fallback. Do not add a second tool-event database, transcript journal, store interception layer, or private adapter-state access to preserve every intermediate frame.

### H. Let Textual implement the UI

Evolve `harness.renderers` into a single local adapter-provider contract for a renderer ID and payload schema version. A provider can offer independent `command_view` and `tool_view` factories; an invocation requests the appropriate kind explicitly. Missing factory/version is a normal fallback, not an excuse to turn a command into a tool card. Replace the old string-only adapter convention without a dual API.

Each factory receives its typed state and `ViewContext`, and returns `ViewHandle(widget, apply)`. Harness mounts the ordinary widget inline and applies successive immutable snapshots to that same instance. Plugins own children, CSS, and local events. No serialized widget trees, remote import paths, renderer-generated model messages, or custom Textual lifecycle base class.

Tool widgets receive structured raw input/output, standard execution status, supplemental presentations, and live/replay state. A plugin may lay out its input and output regions freely inside the inline tool body. Harness retains the outer tool identity/status and its approval handling. Command widgets live in an independent command-result block.

Native Textual unmounting and widget-owned workers handle teardown when blocks are pruned, sessions switch, or the connection closes. Use scoped/default CSS and ordinary focus/message handling. Widget interactions can call `ViewContext.invoke_command(binding_id, command_name, arguments)`; they cannot invoke arbitrary agent tools. Old/replayed widgets require a current enabled binding and valid revision before acting. Rendering/mounting/replay never implicitly submits actions.

Catch loader, factory, and `apply` failures at Harness integration boundaries and replace that view with sanitized fallback. Installed Python widgets run in-process: this is not an exception/security sandbox, and arbitrary plugin event handlers can still fail. Authors own those handlers and worker error behavior. Do not introduce a global exception monkeypatch or claim that Harness can contain every plugin failure.

## 4. Custom work and decision gates

These are the actual Harness-owned additions, not features supplied automatically by Pydantic AI:

| ID | Addition or gap | Recommendation and boundary |
| --- | --- | --- |
| D1 | Commands, descriptor discovery, and direct RPC execution | Implement one generic application registry/dispatcher. Keep handlers outside native tool execution; never duplicate the upstream agent lifecycle. |
| D2 | Metadata/progress are not carried by the pinned ACP adapter | Implement one public native observer plus negotiated presentation notifications. Prove event ordering, cancellation, isolation, and replay extraction before building custom widgets. If this needs private state or per-plugin cases, stop that approach and present the gap. |
| D3 | No public writable ACP history API | Support inspection and explicit `StartTurn` now. Clear/compact/edit remain unavailable on these pins. If required in this delivery, first obtain/adopt a public upstream replacement operation with locking, persistence, and replay semantics; then implement conditional Task 16. Do not silently choose a custom history engine, close/save/reload workaround, or next-turn mutation masquerading as an immediate operation. |
| D4 | Arbitrary Textual code executes in the TUI | Offer ordinary widgets and a small context. Keep failure handling at supported boundaries; plugin authors own their widgets. Screens/dialogs remain excluded. |
| D5 | User-only command output is not in upstream persisted updates | Keep it in bounded current UI history. Durable replay would require a separate reviewed persistence decision and is not required for the `/status` behavior discussed. |

D1/D2 are required custom application integration and should be reviewed as part of this plan before implementation. D3 is an upstream dependency gap, not a claim that full history mutation is implemented. The supported subset can ship independently; completion reporting must still name D3 explicitly if it remains unresolved.

## 5. Implementation order and task definitions

All tasks start unchecked in [tasks/todo.md](tasks/todo.md). Every task should fit one focused session and touch at most approximately five files. If it grows beyond that, split it before implementation. No task adds a test suite or test files. In task file lists, abbreviated `runtime/`, `client/`, and `protocol/` paths are relative to `src/harness/`.

Dependency spine:

```text
01 contracts -> 02 composition/discovery -> 03 SDK command execution
                                         -> 04 TUI fallback -> 05 lifecycle -> 06 StartTurn
01 + 02 -> 07 native presentation observer -> 08 client merge -> 09 replay
01 -> 10 local widget adapters -> 11 command views / 12 tool views
05 + 09 + 11 + 12 -> 13 interactive commands -> 14 cleanup -> 15 transport verification
06 + public upstream history operation -> 16 conditional conversation writes
15 -> 17 external plugin example -> 18 author documentation -> 19 final verification
```

### Task 01: Define the minimal extension contracts

**Description:** Implement the non-executable wire models and small author-facing command contracts described above. Freeze one supported conversation action (`StartTurn`) and the operation-discovery/error contract.

**Acceptance criteria:**
- [ ] Wire models accept only bounded JSON, explicit discriminators, and mutually exclusive argument representations; `PresentationPayload` remains the shared data envelope.
- [ ] Async command definitions, optional parser/argument models, required operations, and `CommandContext` have typed contracts without Textual, fake `RunContext`, or mutable host/session internals.
- [ ] Results separate presentation from one optional conversation action; unsupported history operations are represented as unavailable capabilities, not nonfunctional action types.

**Verification:** Validate representative success/error payloads with Pydantic in a temporary offline smoke; run Ruff and Pyrefly on changed modules. No persistent test files.

**Dependencies:** None.

**Files likely touched:** `src/harness/protocol/extensions.py` (new), `protocol/__init__.py`, `runtime/extensions.py` (new), `runtime/__init__.py`.

**Estimated scope:** Medium, 4 files.

### Task 02: Bind commands to the native plugin composition

**Description:** Extend descriptors and lazy installed discovery, then collect command bindings alongside the actual enabled native capabilities. Refresh the extension state with managed configuration changes.

**Acceptance criteria:**
- [ ] A descriptor-provider entry point exposes config/native factory/commands without importing Textual; native-only descriptors remain valid and no old entry-point fallback is retained.
- [ ] Namespaces use binding IDs; duplicate/invalid names fail deterministically, including bindings with colons and multiple instances of one plugin.
- [ ] Command contexts use current validated configuration/resources; disabling/reconfiguring a binding replaces its command registrations without executing another native lifecycle.

**Verification:** Offline composition of two differently configured bindings; toggle/reconfigure and inspect registrations. Verify a deferred native capability need not be model-loaded for its explicit user command to exist.

**Dependencies:** 01.

**Files likely touched:** `runtime/registry.py`, `runtime/policy.py`, `runtime/composition.py`, `runtime/host.py`.

**Estimated scope:** Medium, 4 files.

### Task 03: Invoke a user-only command through the client SDK

**Description:** Add negotiated command discovery/invocation and typed client methods, delivering a complete headless SDK path with fallback output before any widget work.

**Acceptance criteria:**
- [ ] The host and `SessionProxy` can list/invoke an enabled command using one shared extension-method catalog and return a validated result.
- [ ] Session scope, active revision, and arguments are checked on the host; unavailable/unsupported operations fail before the handler executes.
- [ ] A direct command causes zero model requests, no tool events, and no changes to committed native history, title, or model usage.

**Verification:** Temporary offline counter-backed model/handler smoke through the real ACP client/host; inspect public `session_history`, model-call count, fallback, and error responses.

**Dependencies:** 01, 02.

**Files likely touched:** `runtime/commands.py` (new), `runtime/host.py`, `client/client.py`, `protocol/transport.py`.

**Estimated scope:** Medium, 4 files.

### Checkpoint A: After Tasks 01–03

Demonstrate a direct command end to end through ACP with no model-history pollution. Review D1 and confirm no fake tool execution or duplicate agent loop was introduced. Static checks pass for the slice.

### Task 04: Surface namespaced commands with inline fallback output

**Description:** Integrate plugin discovery into the slash picker, palette, and help, while preserving separate local/plugin/standard-agent dispatch routes. Add a distinct command-output block using sanitized text first.

**Acceptance criteria:**
- [ ] Typing `/binding:command`, choosing it in the palette, and using help all resolve from the same command descriptors; catalogs refresh after load/configuration changes.
- [ ] Direct output appears inline as command output, never an assistant message or `ToolCard`; search/copy include its fallback without changing “latest assistant response.”
- [ ] Ordinary agent-advertised ACP commands still follow their normal prompt route; unknown/stale plugin commands are never silently sent to the model.

**Verification:** Textual manual/offline interaction with local and plugin commands, standard ACP advertised commands, malformed namespace, stale descriptors, and unavailable extension negotiation.

**Dependencies:** 03.

**Files likely touched:** `client/tui/commands.py`, `client/tui/screens.py`, `client/tui/widgets.py`, `client/events.py`.

**Estimated scope:** Medium, 4 files.

### Task 05: Bound command execution and cancellation

**Description:** Complete invocation task ownership, busy behavior, request-ID deduplication, cancellation, and cleanup under the existing session/configuration boundaries.

**Acceptance criteria:**
- [ ] Duplicate IDs with identical payloads share/replay one execution; conflicting reuse fails; caches/tasks are bounded and cleared on session close.
- [ ] Busy sessions reject new plugin actions predictably; cancellation and close unwind handlers before resources are replaced, without recursive lock acquisition or automatic retries.
- [ ] Disconnects and ambiguous side effects are reported honestly; one session/principal cannot cancel or read another's invocation.

**Verification:** Offline async handler with an explicit wait point; exercise duplicate requests, cancellation, close, disconnect, and concurrent configuration update. Confirm handler execution count.

**Dependencies:** 03, 04.

**Files likely touched:** `runtime/commands.py`, `runtime/host.py`, `client/client.py`, `client/tui/screens.py`.

**Estimated scope:** Medium, 4 files.

### Task 06: Execute explicit StartTurn results

**Description:** Consume a command's explicit conversation action through the existing turn coordinator rather than run a model from the command dispatcher.

**Acceptance criteria:**
- [ ] `StartTurn` creates exactly one normal prompt with the intended content and normal usage/approval/cancellation handling, after the invocation lock is released.
- [ ] Display-only output remains out of model history; staged attachments and queued prompts are not consumed by accident, and a stale session result cannot start a turn in another session.
- [ ] Duplicate responses and replay do not repeat the action; unavailable conversation actions fail before handler side effects.

**Verification:** FunctionModel-based offline smoke inspecting the exact next model history and request count, plus an attachment/session-switch/duplicate-result manual check.

**Dependencies:** 05.

**Files likely touched:** `runtime/commands.py`, `client/client.py`, `client/tui/screens.py`, `client/tui/app.py`.

**Estimated scope:** Medium, 4 files.

### Checkpoint B: After Tasks 04–06

Demonstrate `/example:status` as a user-only inline readout and `/example:ask` as an explicit normal turn. Confirm no plugin-name branches in TUI code and no command output added to model messages by default.

### Task 07: Forward tool presentation through one native observer

**Description:** Implement and prove D2 early: a session-scoped native observer forwards tool metadata and optional typed presentation progress through the negotiated extension while leaving upstream events unchanged.

**Acceptance criteria:**
- [ ] Input selection uses public tool definitions/metadata; final payloads come from native return metadata; explicit progress uses a typed `CapabilityEvent` and public `ctx.emit`.
- [ ] Exact identity survives prefixes, deferred tools, parallel calls, and approval continuations; no display-title matching, private adapter emitter/state, or event-loop replacement is used.
- [ ] Presentation payloads are validated/bounded and sink failure degrades display without changing model-facing results or successful execution.

**Verification:** Offline native capability with model-visible sentinel and different UI-only sentinel, parallel/prefixed tools, deferred loading, approval, retry, and a failing sink. Capture provider-facing request JSON through an offline mock to establish that the UI-only sentinel is not transmitted. A FunctionModel can observe native application metadata, so its raw message objects alone cannot prove provider-level exclusion.

**Dependencies:** 01, 02.

**Files likely touched:** `runtime/presentation.py` (new), `runtime/host.py`, `runtime/composition.py`, `protocol/extensions.py`.

**Estimated scope:** Medium, 4 files.

### Task 08: Merge supplemental presentation into standard tool state

**Description:** Add typed client events and bounded state merging for supplemental snapshots, preserving ordinary ACP tool status and raw arguments/results.

**Acceptance criteria:**
- [ ] Standard and supplemental events join by session/tool-call identity regardless of arrival order; supplemental run IDs and monotonic revisions reject conflicting/stale data without requiring a run ID on standard ACP events.
- [ ] Late/duplicate snapshots cannot reset completed status or resurrect an evicted card; pending state is bounded and discarded on session switch/close.
- [ ] Generic tool display still works with no metadata, missing extension, malformed payload, or no renderer installed.

**Verification:** Feed a short offline event sequence in both orderings, including parallel calls, eviction, failed/cancelled tools, and a non-Harness ACP peer.

**Dependencies:** 07.

**Files likely touched:** `client/client.py`, `client/events.py`, `client/tui/state.py`, `client/tui/widgets.py`.

**Estimated scope:** Medium, 4 files.

### Task 09: Restore final tool presentation passively

**Description:** Expose bounded snapshot reads from public committed history and hydrate retained replayed tool cards after load. Preserve native persistence ownership.

**Acceptance criteria:**
- [ ] Final native return metadata contains enough input/output presentation information to reconstruct a completed custom view; no additional SQL schema or store interception is introduced.
- [ ] New/load/reconnect sequencing hydrates the correct retained cards without executing commands, tools, models, or UI actions.
- [ ] Missing/compacted/uncommitted metadata yields generic fallback; intermediate progress is explicitly live-only, and direct command output is not misrepresented as durable upstream history.

**Verification:** Offline SQLite save/load with native metadata; repeat after removing a renderer and with absent final metadata. Verify invocation/model counters remain unchanged during replay.

**Dependencies:** 08.

**Files likely touched:** `runtime/presentation.py`, `runtime/host.py`, `client/client.py`, `client/tui/screens.py`.

**Estimated scope:** Medium, 4 files.

### Checkpoint C: After Tasks 07–09

Review D2 against actual pinned APIs. Tool execution/model messages must be identical with and without presentation. Demonstrate live and restored final snapshots without a private hook or second persistence engine. If the approach cannot meet these boundaries, present the specific gap before proceeding with widget implementation.

### Task 10: Define local Textual adapter discovery

**Description:** Replace the text-only renderer extension with a local provider returning independent command/tool view factories for exact renderer/schema identities.

**Acceptance criteria:**
- [ ] `harness.renderers` discovery remains lazy; duplicate identities fail deterministically, and remote identifiers cannot become import paths.
- [ ] `ViewHandle`, separate view states, and `ViewContext` use normal Textual widgets without runtime imports; headless/client-core usage does not import Textual through the registry.
- [ ] Missing factory/version/provider and factory/apply failures yield readable fallback; no string-renderer compatibility adapter is retained.

**Verification:** Import-boundary checks in runtime-only/client-only contexts and local provider-discovery smoke with missing/duplicate/broken adapters.

**Dependencies:** 01.

**Files likely touched:** `client/renderers.py`, `client/tui/extensions.py` (new), `client/tui/__init__.py`.

**Estimated scope:** Medium, 3 files.

### Task 11: Mount custom command-result views

**Description:** Allow a direct command's presentation to instantiate its local command widget in the distinct inline command block.

**Acceptance criteria:**
- [ ] Plugin-owned widgets display inside command output without an agent role/tool status or any new screen/dialog API.
- [ ] Updates target the existing view instance; fallback/search/copy remain usable and the result is still excluded from model history and usage.
- [ ] A missing/failing command factory degrades that block only; it never invokes the tool-view factory as a substitute.

**Verification:** Manual/offline Textual table/checklist command view, fallback provider, copy/search, and repeated snapshot application.

**Dependencies:** 04, 10.

**Files likely touched:** `client/tui/widgets.py`, `client/tui/screens.py`, `client/tui/extensions.py`.

**Estimated scope:** Medium, 3 files.

### Task 12: Mount custom tool input/output views

**Description:** Mount the plugin-owned tool body and feed it merged immutable tool snapshots while Harness retains the standard outer identity/status/approval behavior.

**Acceptance criteria:**
- [ ] One tool view can render raw inputs, running state, outputs, and supplemental data; it is updated in place without resetting focus/selection on every event.
- [ ] Command and tool factories remain independent, and native approvals/outcomes remain understandable outside the custom body.
- [ ] Generic and custom views coexist across live execution, failures, parallel calls, and restored completed cards.

**Verification:** Offline input/result/progress sample with widget identity/focus checks, approval and cancellation, plus restore with its provider missing.

**Dependencies:** 08, 09, 10.

**Files likely touched:** `client/tui/widgets.py`, `client/tui/state.py`, `client/tui/extensions.py`.

**Estimated scope:** Medium, 3 files.

### Checkpoint D: After Tasks 10–12

Demonstrate two independently registered view factories from an external module. Standard Textual composition/styles must do the UI work. Confirm no Textual dependency leaks into protocol/runtime and no plugin-specific TUI conditionals.

### Task 13: Route widget interactions through plugin commands

**Description:** Provide one structured invocation helper for interactive controls in command/tool widgets, sharing the same host command contract as slash invocation.

**Acceptance criteria:**
- [ ] A button/selection explicitly invokes a command with validated structured arguments; it cannot directly execute arbitrary agent tools.
- [ ] Current session, enabled binding, operation availability, and active revision are rechecked; stale/replayed widgets cannot silently target another session or configuration.
- [ ] Mount/apply/replay causes no backend action; explicit `StartTurn` results follow the same once-only route as slash commands.

**Verification:** Offline interactive widget activation followed by disable/reconfigure, session switch, replay, duplicate click, and a result requesting a normal turn.

**Dependencies:** 05, 06, 09, 11, 12.

**Files likely touched:** `client/tui/extensions.py`, `client/tui/screens.py`, `client/tui/widgets.py`, `runtime/commands.py`.

**Estimated scope:** Medium, 4 files.

### Task 14: Finish widget retention and failure boundaries

**Description:** Integrate custom widgets with bounded transcript retention and native Textual teardown; document the limits of in-process plugin error containment.

**Acceptance criteria:**
- [ ] Pruning, session switching, reconnect, and shutdown release view references and widget-owned tasks; supplemental pending maps cannot grow past their bounds.
- [ ] Loader/factory/apply failures replace the affected block with fallback and useful local diagnostics; ordinary UI remains usable at those supported boundaries.
- [ ] CSS/focus stays local to the contributed body, and no parallel lifecycle/worker manager, global exception monkeypatch, or screen/dialog API is introduced.

**Verification:** Manual retention overflow and session-switch smoke with a widget-owned worker; exercise factory/apply errors and inspect cleanup/reference counts. Verify host model execution is unaffected.

**Dependencies:** 11, 12, 13.

**Files likely touched:** `client/tui/extensions.py`, `client/tui/widgets.py`, `client/tui/screens.py`, `client/renderers.py`.

**Estimated scope:** Medium, 4 files.

### Task 15: Verify stdio, remote, and headless behavior

**Description:** Exercise the same contracts across existing transports and installation boundaries. Keep this as verification rather than add another transport abstraction.

**Acceptance criteria:**
- [ ] Local stdio and FastAPI/WebSocket hosting use the same methods, scope checks, errors, cancellation, and bounded data contracts.
- [ ] A remote host works with the UI package installed only on the client; runtime-only hosting and headless consumption require no Textual imports and retain fallback information.
- [ ] Missing negotiation, unknown schema, malicious import-like renderer identifiers, cross-session requests, and connection loss behave predictably without remote code loading or auto-retry.

**Verification:** Offline local-process and loopback server smokes using an existing local configuration/temporary store, plus a standard ACP peer with no Harness extension. Record exact environment and results; no provider keys or paid model calls required.

**Dependencies:** 05, 06, 09, 14.

**Files likely touched:** `docs/VERIFICATION.md`; fixes, if necessary, limited to the owning client/host/presentation modules and split out if broad.

**Estimated scope:** Small, 1 documentation file plus narrowly justified fixes.

### Checkpoint E: After Tasks 13–15

Confirm one external plugin can deliver interactive inline UI over both hosting modes without changing transport code or adding plugin cases to Harness. Record offline/manual results separately from live-provider verification. Review D3 before claiming any writable-history support.

### Task 16: Add conversation writes only after a public upstream operation exists

**Description:** Conditional task. The current pins do not meet its prerequisite. If clear/compact/edit are required now, first review adopting/contributing a public adapter operation that safely replaces resident history and defines persistence/replay semantics. Do not implement a workaround during unrelated tasks.

**Acceptance criteria:**
- [ ] A public upstream contract covers locking/active turns, history replacement, persistence, and user-visible transcript semantics; the dependency decision is recorded before implementation.
- [ ] Only actual supported actions are advertised; compaction reuses `compact_now`/native strategies, preserves tool-call/result validity, and reports any model usage/cost separately and accurately.
- [ ] New message/part objects are used; no private state, mutation of shared history snapshots, close/save/reload trick, or duplicate session/history engine is added.

**Verification:** After the prerequisite is satisfied, offline before/after native history and replay checks for each enabled operation, including cancellation and persistence failure. Until then verify unsupported-operation rejection, and report this task as deferred rather than implemented.

**Dependencies:** 06 and a separately reviewed public upstream history operation/dependency version.

**Files likely touched:** `runtime/commands.py`, `runtime/host.py`, `protocol/extensions.py`, `client/client.py`, `pyproject.toml` (only with an approved dependency decision).

**Estimated scope:** Medium, at most 5 local files; upstream contribution/release work requires its own task breakdown.

### Task 17: Publish a small installable example plugin

**Description:** Add an example package that authors can install without changing Harness: native capability, separate status/ask commands, and independent command/tool widgets under optional UI dependencies.

**Acceptance criteria:**
- [ ] Its descriptor provider supplies `/example:status` and `/example:ask`; the former is user-only and the latter explicitly starts a turn. Its agent tool has an independent handler and renderer.
- [ ] It demonstrates native return metadata and optional typed progress, with no Textual imports from runtime registration and no Harness source modifications to recognize the example.
- [ ] Installation on host/client independently demonstrates custom UI versus readable fallback using deterministic offline behavior.

**Verification:** Build/install the package in temporary environments and follow its documented offline commands over stdio and loopback hosting. No test files and no new production Git integration.

**Dependencies:** 15; include Task 16 operations only if actually implemented.

**Files likely touched:** `examples/plugins/example_status/pyproject.toml`, `src/example_status/__init__.py`, `src/example_status/tui.py`, `README.md` (all within that example package).

**Estimated scope:** Medium, 4 files.

### Task 18: Document the extension authoring API

**Description:** Publish a focused author guide and update existing protocol/runtime/client references to the final contract, including its exact unsupported boundaries.

**Acceptance criteria:**
- [ ] Document descriptor and UI entry points, namespace/arguments/context/results, separate command/tool factories, explicit conversation actions, and interactive callbacks with examples.
- [ ] Explain local installation, schema/fallback rules, passive replay, live-only progress, current-UI-only command output, and supported exception boundaries.
- [ ] Document current supported conversation operations honestly, no compatibility aliases/dual contracts, and the distinction between standard ACP agent commands and Harness user-only plugin commands.

**Verification:** Walk the guide from a clean temporary installation using Task 17; check snippets against actual signatures and verify referenced relative links.

**Dependencies:** 17 and the recorded outcome of 16.

**Files likely touched:** `docs/EXTENSIONS.md` (new), `docs/CLIENT_API.md`, `docs/RUNTIME_API.md`, `docs/PROTOCOL.md`, `examples/README.md`.

**Estimated scope:** Medium, 5 documentation files.

### Task 19: Run final gates and report supported scope

**Description:** Run the existing repository checks and the bounded acceptance walkthrough after implementation. Report what is implemented, what was verified offline, and any upstream dependency still outstanding.

**Acceptance criteria:**
- [ ] Ruff/Pyrefly/build/diff checks pass for the final change, with unrelated baseline failures identified separately and unrelated user edits preserved.
- [ ] Demonstrate user-only status, explicit agent turn, independent input/output rendering, an interactive command callback, missing-renderer fallback, and passive replay over both transports.
- [ ] Report D3 and any deferred operation explicitly; do not call the full history-editing surface complete when the public upstream prerequisite is absent.

**Verification:** Run the commands below once for the final implementation; repeat only for changed code or unresolved failures. Record the Task 15/17 walkthrough evidence in `docs/VERIFICATION.md`.

**Dependencies:** 18; Task 16 either implemented under its prerequisite or explicitly recorded as deferred/unsupported.

**Files likely touched:** `docs/VERIFICATION.md`, `tasks/todo.md`, `PLAN.md` (completion/deferred status only).

**Estimated scope:** Small, 3 documentation files.

### Checkpoint F: Complete

All mandatory acceptance criteria pass, the external-plugin walkthrough works, and the user has reviewed the result. Conditional work remains visibly conditional. No live-provider success is inferred from FunctionModel/offline checks.

## 6. Verification commands and scenarios

Use existing non-mutating checks; `just lint` reformats/fixes files and is not the appropriate read-only gate when preserving unrelated edits.

```sh
uv run ruff check src/harness
uv run ruff format --check <changed-python-files>
uv run pyrefly check src/harness --no-progress-bar
uv build
git diff --check
```

Include the new example's Python files in the focused lint/type checks once present. No new tests, test dependencies, CI jobs, compatibility readers, legacy aliases, or persistence migrations are part of this plan.

Manual/offline acceptance matrix:

| Scenario | Required observation |
| --- | --- |
| Direct `/example:status` | Inline command result; no tool event, model call, model-history addition, title change, or model-usage increment. |
| Explicit `/example:ask` | One normal agent turn; only intentional prompt enters history; normal usage/cancellation applies. |
| Native agent tool | Independent tool widget, input/output snapshots, unchanged model-facing result. |
| Two bindings / prefix / deferred loading | Exact ownership; independent command namespaces; no reliance on display titles or run-local IDs. |
| Disable or edit binding | Picker refresh; host rejects stale invocation/callback. |
| Concurrent/duplicate/cancelled invocation | Defined busy/dedup/cancel behavior; no hidden retry or resource use after close. |
| Early/late supplemental metadata | Correct merge; bounded pending state; no card resurrection after pruning. |
| Session load | Passive final-view reconstruction; missing metadata falls back; no actions repeat. |
| Missing/broken UI package | Sanitized fallback; runtime/client core still import and work without Textual. |
| Remote peer / standard ACP peer | Same negotiated behavior over both transports; standard-only peers retain generic functionality. |
| Unsupported history mutation | Not advertised; clear rejection before handler side effects. |
| Widget retention / failure | Native teardown, bounded references, and fallback at supported loader/factory/apply boundaries. |

## 7. Risks and boundaries

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Rebuilding upstream execution to add UI | High | Keep dispatch application-side and the presentation observer passive; prohibit private adapter state/methods. |
| Promising unsupported live history changes | High | D3 and conditional Task 16; distinguish a computed history from installing it into a resident session. |
| Commands accidentally becoming model messages | High | Separate negotiated dispatch and command block; only explicit conversation actions enter the normal prompt path. |
| Wrong tool/view association under concurrency | High | Exact identity and bounded state; exercise prefixes, deferred tools, parallel calls, and approval continuations early. |
| Descriptor/handler state leaking across bindings | High | Validated context at invocation, binding-scoped namespace, active revision checks, and no fake native lifecycle. |
| Plugin UI destabilizes the TUI | Medium | Standard Textual ownership, supported boundary fallback, documented author responsibility; no sandbox guarantee. |
| Persisting extra UI state creates a second lifecycle | Medium | Reuse native return metadata for final tool views; keep command output current-UI-only and progress live-only. |
| Hidden compatibility/duplicate discovery contracts | Medium | One descriptor-provider convention and one widget-provider convention; update callers rather than dual-read. |
| Current user edits drift during implementation | Medium | Re-read status/diff and affected files before each slice; keep changes scoped and avoid repository-wide formatting. |

## 8. Review points before implementation

1. Review D1/D2: these are necessary generic Harness integration, not upstream features. Stop and revisit if implementation grows into per-plugin adapters or a second runtime.
2. Confirm the documented supported subset is acceptable while D3 is unavailable. Requiring immediate clear/compact/edit changes the dependency plan; it does not authorize private-state workarounds.
3. Checkpoint reviews assess concrete slices and substantial gaps. Routine implementation choices already resolved here do not require repeated permission prompts.

Implementation validation: commands, command binding/composition, negotiated invoke/cancel, explicit `StartTurn`, the passive native presentation observer, client merge, passive replay, local view adapters and example plugin were implemented and exercised offline over stdio as recorded in [docs/VERIFICATION.md](docs/VERIFICATION.md). D3 remains an upstream gap: writable history operations are not advertised. The WebSocket hosting walkthrough for this feature was not run.
