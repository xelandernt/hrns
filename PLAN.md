# Implementation Plan: Responsibility-Focused Runtime and TUI Refactor

## Overview

The review identified five files that carry too many independent responsibilities
(`client/tui/screens.py`, `client/tui/widgets.py`, `runtime/host.py`, `client/client.py`, and
`runtime/policy.py`). The correctness findings R1–R10 and the cleanup items D1–D4/D6 are
already addressed. This plan covers the remaining structural work: split those modules along
cohesive ownership boundaries, remove the resulting dead scaffolding, and audit the TUI's
untracked background tasks.

The intent is **behavior-preserving refactoring**. Moves happen before redesigns. No new
features, no new tests (repository policy), and no blanket typing rewrite. Code moved during
this refactor should not add `Any`, `cast()`, or type-ignore comments; where a moved block has
an avoidable escape hatch, remove it in the same task.

## Preconditions

- [ ] The current R1–R10/D1–D4/D6 working tree is reviewed, committed, and green in CI.
- [ ] `just check` passes on the starting commit.
- [ ] The refactor starts from a clean working tree; no unrelated edits are mixed into a task.
- [ ] No `tasks/plan.md` or `tasks/todo.md` exists with unfinished work for this repository.
      Task tracking for this plan lives in this file, as requested.

## Baseline Snapshot

Measured before this plan:

| File | Lines | Primary responsibilities today |
| --- | ---: | --- |
| `src/harness/client/tui/screens.py` | 3,690 | Connection picker, chat screen, event consumption, prompt/queue, sessions, models, capabilities, plugin/MCP workflows |
| `src/harness/client/tui/widgets.py` | 3,215 | Formatting, messages, composer, transcript, tool cards, status, approvals, questions, side panels |
| `src/harness/runtime/host.py` | 1,765 | ACP adapter, host policy/setup, composition, configuration transactions, commands, session metadata, CLI/bootstrap |
| `src/harness/client/client.py` | 1,416 | Connection lifecycle, session API, extension API, protocol callbacks, event normalization |
| `src/harness/runtime/policy.py` | 999 | Binding state, offering catalog, change validation, graph validation, configuration application |

Static baseline: Ruff clean, Pyrefly 0 errors with 6 suppressed diagnostics, `just check`
passes, `examples/custom_capability.py` runs. There is no automated test suite.

## Goals

- No source module in the affected areas exceeds roughly 800 lines; `host.py` and
  `client.py` should end below 500 lines.
- One module owns one named responsibility from the review table.
- `client/tui/widgets.py` is removed after its widgets move to dedicated modules.
- `screens.py` becomes one small screen core, with non-UI workflows delegated to explicit
  collaborators instead of being accumulated as private methods.
- TUI background work uses tracked Textual workers or an explicitly owned task helper, with
  exceptions observed.
- All internal imports are updated; no compatibility shims remain.
- `just check` stays green after every task.
- No new `Any`, `cast()`, or type-ignore comments are introduced. Pyrefly suppressed
  diagnostics do not increase; they should decrease where moved code makes it cheap.

## Non-Goals

- Changing ACP, catalog, storage, profile, search, or configuration behavior.
- Adding a test suite or changing the no-test repository policy.
- Rewriting all `Any` annotations; D4's blanket rewrite remains out of scope.
- Redesigning the TUI visually or changing keyboard bindings.
- Changing the public package layout beyond what the new module boundaries require.
- Implementing the optional PostgreSQL live verification or provider integrations.

## Architecture Decisions

1. **Move shared primitives first.** Textual widgets import each other heavily. A small
   `tui/formatting.py` and `tui/messages.py` break the initial cycles before widget classes
   move.
2. **Keep Textual classes in their own modules.** Textual resolves handlers and composition
   from the class body. Handlers stay on screen/widget classes; extracted collaborators are
   plain objects called by those handlers.
3. **Prefer delegation over mixins for multi-concern screens.** A `ChatScreen` keeps its
   lifecycle/layout/handler methods and delegates session, model, capability, and prompt
   workflows to constructor-injected collaborators. This makes state ownership explicit and
   avoids a mixin that silently depends on a dozen screen attributes.
4. **Extract bottom-up.** Widgets before screens, host support before host services, client
   payload helpers before connection/session collaborators.
5. **Do not create a compatibility layer.** The repository explicitly forbids backward
   compatibility unless requested. Move imports directly; delete the old module when empty.
6. **No behavior changes in move-only tasks.** A task that only relocates code must preserve
   method names, signatures, and call order. Behavior changes require a separate task with
   its own acceptance criteria.
7. **Verification without a test suite.** Each task runs `just check`, targeted import smoke
   checks, and a temporary headless Textual probe or existing example. Temporary probes are
   not committed.
8. **One task per reviewable change.** No task should touch more than five files or mix two
   independent subsystems.

## Dependency Graph

```text
Preflight / baseline
        │
        ▼
TUI shared formatting + messages + blocks
        │
        ├── transcript widgets
        ├── composer widgets
        ├── status / approval widgets
        └── side panels ───────────────┐
                                        ▼
                              remove widgets.py
                                        │
                                        ▼
                         connection picker + screen base
                                        │
                                        ▼
                         ChatScreen workflow collaborators
                                        │
                                        ▼
                         tracked-worker audit + chat core cleanup
                                        │
        ┌───────────────────────────────┴───────────────────────────────┐
        ▼                                                               ▼
Runtime host support types                                      Client SessionProxy + payloads
        │                                                               │
        ├── configuration service                                       ├── extension API
        ├── command service                                             ├── connection lifecycle
        └── session/composition service                                 └── client core cleanup
        │
        ▼
Runtime CLI/bootstrap extraction
        │
        ▼
Policy package split (optional lower-priority phase)
        │
        ▼
Docs, dead-code sweep, final verification
```

## Target Layout

```text
src/harness/client/tui/
  formatting.py          # text truncation, span styling, small pure helpers
  messages.py            # cross-widget Textual Message classes
  blocks.py              # MessageBlock, ReasoningBlock, NoticeBlock
  transcript.py          # TranscriptPane, CommandResultBlock, ToolCard, tool views
  composer.py            # Composer, CommandSuggestions, AttachmentBar, PromptQueue
  status.py              # StatusBar and status formatting
  approval.py            # ApprovalBar, AskUserBar and their messages/helpers
  panels/
    __init__.py
    session.py           # SessionPanel
    capability.py        # CapabilityPanel
    mcp.py               # McpPanel
    activity.py          # ActivityPanel
    todo.py              # TodoPanel
    side.py              # SidePanel assembly
  screen_base.py         # _HarnessScreen typed app access
  connection_screen.py   # ConnectScreen + PickEntry
  chat/
    __init__.py
    screen.py            # ChatScreen core: mount, layout, event loop, Textual handlers
    session.py           # session load/new/resume/delete/close/list workflow
    model.py             # model/effort/config-option workflow
    capabilities.py      # plugin/MCP/capability workflow
    prompts.py           # prompt submission, queue, drafts, attachments
    commands.py          # local and plugin command routing
  app.py
  dialogs.py
  local.py
  state.py
  tool_views.py
```

```text
src/harness/runtime/
  host.py                # thin HarnessACPAgent adapter
  host_support.py        # HostSetup/HostBundle/HostPolicy/SessionDeps/composed agent
  configuration_service.py
  command_service.py
  session_service.py
  cli.py                 # argparse entrypoint/bootstrap
  policy/
    __init__.py
    binding.py           # BindingOffering and configuration value helpers
    catalog.py           # PolicyCatalog and change application
    validation.py        # graph/change validation and descriptor helpers
```

```text
src/harness/client/
  client.py              # connection/session facade and ACP callback methods
  connection.py          # connect/disconnect/loss lifecycle collaborator
  session.py             # SessionProxy
  updates.py             # ACP update normalization and tool-event mapping
  extensions_api.py      # _harness extension methods as an explicit collaborator
```

## Task List

### Phase 0 — Preflight

#### Task 1: Capture the baseline and prove the starting point

**Description:** Confirm the current review fixes are committed or otherwise isolated, run
the full verification suite, and record current line counts and Pyrefly suppression counts in
this plan.

**Acceptance criteria:**
- [ ] Working tree is clean before refactor edits begin.
- [ ] `just check` passes.
- [ ] `examples/custom_capability.py` produces the expected enabled/disabled output.
- [ ] `uv run harness-host --help` and `uv run harness-local --catalog` start successfully.
- [ ] Baseline line counts and suppression counts are recorded above or in a short note.

**Verification:**
- [ ] `just check`
- [ ] `uv run python examples/custom_capability.py`
- [ ] `uv run harness-host --help`
- [ ] `uv run harness-local --catalog`

**Dependencies:** None.  
**Files likely touched:** `PLAN.md` only.  
**Estimated scope:** XS.

### Checkpoint: Preflight

- [ ] Refactor starts from a clean, green commit.
- [ ] Baseline is recorded.
- [ ] Human confirms scope before files move.

### Phase 1 — TUI Widgets

#### Task 2: Extract formatting, messages, and block widgets

**Description:** Create `tui/formatting.py` for pure text/style helpers, `tui/messages.py`
for Textual message classes shared across widget modules, and `tui/blocks.py` for
`MessageBlock`, `ReasoningBlock`, and `NoticeBlock`. Update `widgets.py` to import them.

**Acceptance criteria:**
- [ ] `bounded_user_text`, `truncate_text`, and `_restyle_spans` live in `formatting.py`.
- [ ] Cross-widget `Message` subclasses live in `messages.py`.
- [ ] `MessageBlock`, `ReasoningBlock`, and `NoticeBlock` live in `blocks.py`.
- [ ] `widgets.py` no longer defines these symbols.
- [ ] No new `Any`, `cast()`, or type-ignore comments.
- [ ] Public imports used by `screens.py` still resolve during the transition.

**Verification:**
- [ ] `just check`
- [ ] `.venv/bin/python -c "import harness.client.tui.app"`
- [ ] Temporary headless probe mounts the three block widgets and appends text to a
      `MessageBlock`; probe is not committed.

**Dependencies:** Task 1.  
**Files likely touched:** `src/harness/client/tui/widgets.py`,
`src/harness/client/tui/formatting.py`, `src/harness/client/tui/messages.py`,
`src/harness/client/tui/blocks.py`.  
**Estimated scope:** M (4 files).

#### Task 3: Extract transcript rendering

**Description:** Move `TranscriptPane`, `CommandResultBlock`, `ToolCard`, and the tool
presentation/state helpers into `tui/transcript.py`. Keep `ToolCard` behavior and the
transcript's scroll/retention semantics unchanged.

**Acceptance criteria:**
- [ ] Transcript and tool-card classes are defined only in `transcript.py`.
- [ ] `widgets.py` imports and re-exports only what the transition still needs; no duplicate
      class definitions remain.
- [ ] Tool presentation merging and retained-tool-id behavior are unchanged.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Import smoke: `import harness.client.tui.transcript`
- [ ] Temporary headless probe appends a user message, agent text, a tool start/update, and a
      command result; checks the transcript contains each block.

**Dependencies:** Task 2.  
**Files likely touched:** `src/harness/client/tui/widgets.py`,
`src/harness/client/tui/transcript.py`, possibly `src/harness/client/tui/tool_views.py` for
import repair.  
**Estimated scope:** M (2–3 files).

#### Task 4: Extract composer, suggestions, attachments, and prompt queue

**Description:** Move `Composer`, `CommandSuggestions`, `AttachmentBar`, `PromptQueue`, and
their message classes into `tui/composer.py`. Preserve mention highlighting and key handling.

**Acceptance criteria:**
- [ ] Composer, suggestions, attachments, and queue classes live in `composer.py`.
- [ ] Mention catalog rendering and shift-enter/submit behavior are unchanged.
- [ ] Message classes are imported from `messages.py` where shared.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary headless probe types text, submits, stages/removes an attachment, and checks
      queue actions emit the expected messages.

**Dependencies:** Task 2.  
**Files likely touched:** `src/harness/client/tui/widgets.py`,
`src/harness/client/tui/composer.py`, `src/harness/client/tui/messages.py`.  
**Estimated scope:** M (3 files).

#### Task 5: Extract status, approval, and question bars

**Description:** Move `StatusBar` and its formatting helpers to `tui/status.py`; move
`ApprovalBar`, `AskUserBar`, and their messages/helpers to `tui/approval.py`. Keep timing,
spinner, and permission-option mapping behavior intact.

**Acceptance criteria:**
- [ ] `StatusBar` is the only status widget in `status.py`.
- [ ] Approval and ask-user widgets/messages live in `approval.py`.
- [ ] Permission decisions and question answers still emit the same message classes.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary headless probe shows status text, resolves an approval choice, and answers a
      multi-question request.

**Dependencies:** Task 2.  
**Files likely touched:** `src/harness/client/tui/widgets.py`,
`src/harness/client/tui/status.py`, `src/harness/client/tui/approval.py`.  
**Estimated scope:** M (3 files).

#### Task 6: Extract side panels

**Description:** Split the panel widgets into `tui/panels/` with one module per panel family
and a `side.py` assembly module. Move panel-specific messages with their panels.

**Acceptance criteria:**
- [ ] `SessionPanel`, `CapabilityPanel`, `McpPanel`, `ActivityPanel`, `TodoPanel`, and
      `SidePanel` are defined in their dedicated modules.
- [ ] `panels/__init__.py` exports only the assembly surface used by `ChatScreen`.
- [ ] Panel navigation, close requests, and refresh/toggle/edit messages behave as before.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary headless probe mounts `SidePanel`, switches tabs, selects a session, and
      toggles a capability binding.

**Dependencies:** Tasks 2–5.  
**Files likely touched:** `src/harness/client/tui/widgets.py`,
`src/harness/client/tui/panels/__init__.py`, `.../session.py`, `.../capability.py`,
`.../mcp.py`, `.../activity.py`, `.../todo.py`, `.../side.py`.  
**Estimated scope:** L split across this task and the next two; if it exceeds five files in one
review, land the session/activity/todo panels first and capability/MCP/side assembly second.

#### Task 7: Finish panel extraction and delete `widgets.py`

**Description:** Move the remaining capability/MCP/side-panel code, update all imports in
`screens.py`, and delete `client/tui/widgets.py`. No compatibility module remains.

**Acceptance criteria:**
- [ ] `widgets.py` is deleted.
- [ ] `screens.py` imports widgets from their new modules.
- [ ] No repository import references `harness.client.tui.widgets`.
- [ ] The full TUI can be imported and mounted headlessly.

**Verification:**
- [ ] `just check`
- [ ] `rg "tui\\.widgets|from \\.widgets" src` returns no live imports.
- [ ] Temporary headless probe mounts the full `ChatScreen` and `ConnectScreen` trees.

**Dependencies:** Task 6.  
**Files likely touched:** `src/harness/client/tui/widgets.py` (delete),
`src/harness/client/tui/screens.py`, panel modules.  
**Estimated scope:** M (3–5 files).

### Checkpoint: TUI Widgets

- [ ] `widgets.py` is gone; panel/widget modules each own one concern.
- [ ] `just check` passes.
- [ ] Headless widget probes pass.
- [ ] No new typing escape hatches.
- [ ] Stop and review the diff before touching screens.

### Phase 2 — TUI Screens and Workflows

#### Task 8: Extract the shared screen base and connection picker

**Description:** Move `_HarnessScreen` to `screen_base.py`, move `PickEntry`, `_short_path`,
and `ConnectScreen` to `connection_screen.py`, and update `app.py` imports.

**Acceptance criteria:**
- [ ] `screen_base.py` owns only the typed `app` access used for type checking.
- [ ] `connection_screen.py` owns the picker and its local display helpers.
- [ ] `app.py` imports the concrete screens from their new modules.
- [ ] Connection picker rendering, filtering, detail view, and connect workers are unchanged.

**Verification:**
- [ ] `just check`
- [ ] Temporary headless probe mounts `ConnectScreen`, filters entries, and selects a target.

**Dependencies:** Task 7.  
**Files likely touched:** `src/harness/client/tui/screens.py`,
`src/harness/client/tui/screen_base.py`,
`src/harness/client/tui/connection_screen.py`, `src/harness/client/tui/app.py`.  
**Estimated scope:** M (4 files).

#### Task 9: Extract the chat session workflow

**Description:** Introduce `tui/chat/session.py` with a `SessionWorkflow` collaborator that
owns session load/resume/new/delete/close and session-list pagination. `ChatScreen` handlers
delegate to it and retain Textual event signatures.

**Acceptance criteria:**
- [ ] Session lifecycle logic lives in the collaborator, not as private `ChatScreen` methods.
- [ ] The collaborator receives the screen/client/session dependencies explicitly.
- [ ] Session panel events still route to the same operations.
- [ ] No behavior change in session switching, replay, or deletion.

**Verification:**
- [ ] `just check`
- [ ] Temporary headless probe loads a session, creates a new one, and requests more pages.
- [ ] Manual connect-and-session smoke against a local host.

**Dependencies:** Task 8.  
**Files likely touched:** `src/harness/client/tui/screens.py`,
`src/harness/client/tui/chat/session.py`, `src/harness/client/tui/chat/__init__.py`.  
**Estimated scope:** M (2–3 files).

#### Task 10: Extract the model and effort workflow

**Description:** Move model selection, local model settings, effort selection, and standard
config-option handling into `tui/chat/model.py` as a collaborator.

**Acceptance criteria:**
- [ ] Model/effort dialogs and persistence paths live in the collaborator.
- [ ] Standard option stepping/value changes still update the session and refresh chrome.
- [ ] Local settings persistence behavior is unchanged.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary headless probe opens model and effort dialogs and applies a selection.
- [ ] `examples/custom_capability.py` still runs.

**Dependencies:** Task 9.  
**Files likely touched:** `src/harness/client/tui/screens.py`,
`src/harness/client/tui/chat/model.py`.  
**Estimated scope:** M (2 files).

#### Task 11: Extract the capability, plugin, and MCP workflow

**Description:** Move capability refresh/edit/add, binding toggles, plugin selection/forms,
MCP refresh/probe, and unavailable/capability presentation handling into
`tui/chat/capabilities.py`.

**Acceptance criteria:**
- [ ] Capability/plugin/MCP operations and their error notices live in the collaborator.
- [ ] Panel messages still trigger the same operations and refresh behavior.
- [ ] Configuration revisions are not bypassed or mutated differently.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary headless probe toggles a binding and refreshes the capability panel.
- [ ] Manual plugin flow against a local host with the example plugin.

**Dependencies:** Task 10.  
**Files likely touched:** `src/harness/client/tui/screens.py`,
`src/harness/client/tui/chat/capabilities.py`.  
**Estimated scope:** M (2 files).

#### Task 12: Extract prompt, command, and composer workflows

**Description:** Move prompt submission, steering, queue handling, draft save/restore,
attachments, suggestion acceptance, local command dispatch, and plugin command invocation
into `tui/chat/prompts.py` and `tui/chat/commands.py`.

**Acceptance criteria:**
- [ ] Prompt/queue/attachment behavior is delegated and still emits the same events.
- [ ] Local command routing and plugin command argument parsing are unchanged.
- [ ] Composer suggestions still accept and complete correctly.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary headless probe submits a prompt, queues a second, steers, and invokes a local
      command.
- [ ] Manual prompt/queue/attachment smoke.

**Dependencies:** Task 11.  
**Files likely touched:** `src/harness/client/tui/screens.py`,
`src/harness/client/tui/chat/prompts.py`, `src/harness/client/tui/chat/commands.py`.  
**Estimated scope:** M (3 files).

#### Task 13: Audit and track TUI background tasks

**Description:** Replace untracked `asyncio.create_task()` calls in the TUI with Textual
workers (`run_worker`) or a single documented task helper that stores tasks, observes
exceptions, and is cancelled on unmount. Apply to renaming, configuration, model selection,
command dispatch, and notice callbacks.

**Acceptance criteria:**
- [ ] No TUI screen/widget starts a fire-and-forget task whose exception is unobserved.
- [ ] Tasks tied to a mounted widget use Textual's worker lifecycle or are cancelled on
      unmount.
- [ ] Concurrent groups remain exclusive where the old `@work(group=..., exclusive=True)`
      behavior required it.
- [ ] A failed background action surfaces a notice instead of an unhandled-task warning.
- [ ] Remaining `asyncio.create_task` calls have a documented owner and cleanup path.

**Verification:**
- [ ] `just check`
- [ ] `rg "asyncio\\.create_task" src/harness/client/tui` reviewed; each remaining call has a
      comment or helper with explicit ownership.
- [ ] Temporary headless probe forces a background failure and confirms an error notice.

**Dependencies:** Tasks 9–12.  
**Files likely touched:** `src/harness/client/tui/chat/*.py`,
`src/harness/client/tui/connection_screen.py`, shared TUI helper module.  
**Estimated scope:** M (3–5 files).

#### Task 14: Reduce `screens.py` to the chat screen core

**Description:** Rename the remaining screen module to `tui/chat/screen.py` (or keep a small
`screens.py` facade only if it owns the `ChatScreen` class) and remove all now-unused imports
and methods.

**Acceptance criteria:**
- [ ] `ChatScreen` core contains only mount/layout, event consumption, Textual handlers, and
      delegation to the collaborators.
- [ ] `app.py` imports from the final location.
- [ ] `screens.py` is either a small single-class module or is gone.
- [ ] The file is below approximately 900 lines.

**Verification:**
- [ ] `just check`
- [ ] `rg "from \\.screens|tui\\.screens" src` returns only the intended import.
- [ ] Full headless TUI mount probe.

**Dependencies:** Tasks 9–13.  
**Files likely touched:** `src/harness/client/tui/screens.py`,
`src/harness/client/tui/chat/screen.py`, `src/harness/client/tui/chat/__init__.py`,
`src/harness/client/tui/app.py`.  
**Estimated scope:** M (3–4 files).

### Checkpoint: TUI Screens

- [ ] Connection picker and chat screen are in separate modules.
- [ ] Session/model/capability/prompt workflows have explicit owners.
- [ ] No untracked background tasks remain without a documented lifetime.
- [ ] `just check` passes; headless probes pass; manual TUI smoke passes.
- [ ] Stop and review before moving to runtime code.

### Phase 3 — Runtime Host

#### Task 15: Extract host support types and the agent bridge

**Description:** Move `HostSetup`, `HostBundle`, `HostPolicy`, `SessionDeps`,
`_UsageReporting`, `_ComposedAgent`, and the small path/prompt helpers into
`runtime/host_support.py`. Update `runtime/local.py` and `server/app.py` imports.

**Acceptance criteria:**
- [ ] Support types and the composed-agent dispatcher live in `host_support.py`.
- [ ] `host.py` imports them; no duplicate definitions remain.
- [ ] `server/app.py` constructs `HostBundle`/`HostPolicy` from the new module.
- [ ] `runtime/local.py` uses the new `HostBundle`/`build_store` location.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] `uv run harness-host --help`
- [ ] `uv run harness-local --catalog`
- [ ] `uv run harness-host` discovers the example definition.

**Dependencies:** TUI checkpoint (or can run in parallel with Phase 2 by a second worker).  
**Files likely touched:** `src/harness/runtime/host.py`,
`src/harness/runtime/host_support.py`, `src/harness/runtime/local.py`,
`src/harness/server/app.py`.  
**Estimated scope:** M (4 files).

#### Task 16: Extract configuration transactions

**Description:** Create `runtime/configuration_service.py` with a collaborator that owns
request-id caching, expected-revision checks, change application, composition rebuild, and
configuration-changed notification. `HarnessACPAgent` keeps the extension entrypoint and
delegates.

**Acceptance criteria:**
- [ ] The cached-result check remains inside the turn lock (R3).
- [ ] Payload-mismatch rejection remains in the same critical section.
- [ ] The service receives store/policy/definition/composition dependencies explicitly.
- [ ] No behavior change in no-op, revision conflict, or apply paths.

**Verification:**
- [ ] `just check`
- [ ] Offline host probe sends two identical concurrent updates and receives two identical
      snapshots, not a revision conflict.
- [ ] Manual config change followed by a prompt uses the new composition.

**Dependencies:** Task 15.  
**Files likely touched:** `src/harness/runtime/host.py`,
`src/harness/runtime/configuration_service.py`.  
**Estimated scope:** M (2 files).

#### Task 17: Extract command invocation

**Description:** Move command request hashing/caching, command locks, execution, and
command-result/presentation notifications into `runtime/command_service.py`.

**Acceptance criteria:**
- [ ] Duplicate request-id behavior and payload mismatch rejection are preserved.
- [ ] Command execution still runs under the session turn lock.
- [ ] Command cancellation still finds the same in-flight task.
- [ ] No behavior change in inline/start-turn results.

**Verification:**
- [ ] `just check`
- [ ] Temporary host probe invokes the example command twice and checks replay.
- [ ] Manual plugin command smoke.

**Dependencies:** Task 16.  
**Files likely touched:** `src/harness/runtime/host.py`,
`src/harness/runtime/command_service.py`.  
**Estimated scope:** M (2 files).

#### Task 18: Extract session/composition lifecycle

**Description:** Move composition creation/replacement, resource construction, session
metadata/list/delete/load helpers, option snapshots, and presentation/plan/usage notification
helpers into `runtime/session_service.py` (split a second module only if the service exceeds
roughly 600 lines).

**Acceptance criteria:**
- [ ] Session composition and metadata logic live outside the ACP adapter class.
- [ ] `HarnessACPAgent` methods remain thin ACP entrypoints.
- [ ] Session scoping, principal/deployment checks, and store calls are unchanged.
- [ ] No behavior change in load/new/delete/list or plan/usage notifications.

**Verification:**
- [ ] `just check`
- [ ] Temporary host probe creates, lists, loads, and deletes a session.
- [ ] Manual stdio host session walkthrough.

**Dependencies:** Task 17.  
**Files likely touched:** `src/harness/runtime/host.py`,
`src/harness/runtime/session_service.py`, possibly
`src/harness/runtime/presentation_service.py`.  
**Estimated scope:** M/L (2–3 files).

#### Task 19: Extract CLI and bootstrap from `host.py`

**Description:** Move argparse setup, environment parsing, definition loading, router
construction, stdio main, and `_AgentClientChannel` into `runtime/cli.py`. Move
`build_store`/`build_local_bundle`/`build_agent` into `host_support.py` if not already there.
Update `pyproject.toml` entry points and docs.

**Acceptance criteria:**
- [ ] `harness-host` still starts and points at the new CLI module.
- [ ] `harness-local` still imports the host bundle/bootstrap from the correct module.
- [ ] `host.py` no longer contains CLI parsing or stdio main.
- [ ] `pyproject.toml` and docs reference the final entry point.
- [ ] No behavior change in flags, environment handling, or definition loading.

**Verification:**
- [ ] `just check`
- [ ] `uv run harness-host --help`
- [ ] `uv run harness-local --catalog`
- [ ] Both commands work from a clean checkout through the installed entry points.

**Dependencies:** Task 18.  
**Files likely touched:** `src/harness/runtime/host.py`,
`src/harness/runtime/host_support.py`, `src/harness/runtime/cli.py`,
`pyproject.toml`, `docs/LOCAL.md` or `README.md`.  
**Estimated scope:** M (5 files).

#### Task 20: Finish host cleanup and checkpoint

**Description:** Remove unused host imports, collapse thin pass-through methods that no longer
add value, and verify `host.py` is an adapter rather than a second application.

**Acceptance criteria:**
- [ ] `host.py` is below roughly 500 lines.
- [ ] No duplicated ownership remains between `host.py` and the new services.
- [ ] All host module imports are used.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] `rg "class HarnessACPAgent" -A 5 src/harness/runtime/host.py`
- [ ] Full host smoke and example run.

**Dependencies:** Task 19.  
**Files likely touched:** `src/harness/runtime/host.py`, service modules for import cleanup.  
**Estimated scope:** S/M (2–4 files).

### Checkpoint: Runtime Host

- [ ] `host.py` is a thin ACP adapter.
- [ ] Configuration, commands, sessions, and CLI have explicit owners.
- [ ] Host entry points and examples work.
- [ ] `just check` passes.
- [ ] Stop and review before moving to the client core.

### Phase 4 — Client Core

#### Task 21: Extract `SessionProxy`

**Description:** Move `SessionProxy` to `client/session.py`, update package/client/TUI imports,
and leave the client core focused on connection and ACP operations.

**Acceptance criteria:**
- [ ] `SessionProxy` is defined only in `client/session.py`.
- [ ] `client/__init__.py` exports it from the new module.
- [ ] Headless and TUI imports still resolve through the package.
- [ ] No behavior change in cached config/mode state or extension calls.

**Verification:**
- [ ] `just check`
- [ ] `import harness.client` and `from harness.client import SessionProxy`
- [ ] Headless client smoke against an example host.

**Dependencies:** Host checkpoint (or parallel with independent client work).  
**Files likely touched:** `src/harness/client/client.py`,
`src/harness/client/session.py`, `src/harness/client/__init__.py`.  
**Estimated scope:** M (3 files).

#### Task 22: Extract ACP update normalization

**Description:** Move `_dispatch_update`, `_tool_event`, text/content helpers, permission
normalization, and elicitation payload helpers into `client/updates.py` and
`client/payloads.py`. Keep the ACP callback methods on `HarnessAcpClient` and delegate.

**Acceptance criteria:**
- [ ] Update-to-event mapping lives outside the client class.
- [ ] The same event classes and metadata are emitted for text, thought, tools, plans,
      commands, modes, config, session info, and usage updates.
- [ ] Unknown updates still degrade to a readable activity event.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary probe feeds representative update objects through `session_update` and checks
      the emitted events.
- [ ] Manual streaming prompt shows text, tool, plan, and usage updates.

**Dependencies:** Task 21.  
**Files likely touched:** `src/harness/client/client.py`,
`src/harness/client/updates.py`, `src/harness/client/payloads.py`.  
**Estimated scope:** M (3 files).

#### Task 23: Extract the harness extension API

**Description:** Create `client/extensions_api.py` with a collaborator that owns the
`harness_*` calls and their payload validation. `HarnessAcpClient` methods delegate; the
collaborator receives the extension request callable and session-access dependencies
explicitly.

**Acceptance criteria:**
- [ ] Extension request methods live in the collaborator, not the connection class.
- [ ] `SessionProxy` and TUI imports still call the same facade methods.
- [ ] Extension-unavailable and invalid-response errors are unchanged.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary probe invokes capability/plugin/configuration/command/presentation endpoints
      through the facade.
- [ ] Manual plugin/configuration TUI smoke.

**Dependencies:** Task 22.  
**Files likely touched:** `src/harness/client/client.py`,
`src/harness/client/extensions_api.py`.  
**Estimated scope:** M (2 files).

#### Task 24: Extract connection lifecycle and finish client cleanup

**Description:** Move connect/disconnect/reconnect, transport loss handling, stderr tail, and
connection-state publication into `client/connection.py` if the remaining core is still above
the target size. Remove dead helpers and update imports/exports.

**Acceptance criteria:**
- [ ] Connection lifecycle has one explicit owner.
- [ ] Reconnect-after-loss behavior and cleanup suppression are preserved.
- [ ] `client.py` is below roughly 500 lines.
- [ ] No dead private helpers remain.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] Temporary probe opens, drops, and reconnects a transport.
- [ ] Manual TUI reconnect smoke.
- [ ] `rg "_dispatch_update|_tool_event|SessionProxy" src/harness/client` shows only intended
      delegation.

**Dependencies:** Task 23.  
**Files likely touched:** `src/harness/client/client.py`,
`src/harness/client/connection.py`, `src/harness/client/__init__.py`.  
**Estimated scope:** M (3 files).

### Checkpoint: Client Core

- [ ] `client.py` is below roughly 500 lines.
- [ ] Session proxy, update mapping, extension API, and connection lifecycle are separate.
- [ ] Headless and TUI clients still connect, prompt, and reconnect.
- [ ] `just check` passes.
- [ ] Stop and review before policy work.

### Phase 5 — Policy (Lower Priority)

#### Task 25: Extract binding state and configuration value helpers

**Description:** Convert `runtime/policy.py` into a package or split off
`runtime/policy/binding.py` for `BindingOffering`, `BindingState` conversion,
`jsonable_config`, payload hashing, path helpers, and `is_noop_change`. Keep the existing
`from .policy import ...` import path working through `__init__.py`.

**Acceptance criteria:**
- [ ] Binding/value helpers have a dedicated module.
- [ ] `PolicyCatalog` behavior is untouched in this task.
- [ ] All repository imports resolve without compatibility shims beyond the package
      `__init__`.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] `examples/custom_capability.py`
- [ ] Temporary probe exports/restores a binding payload and toggles a binding.

**Dependencies:** Client checkpoint.  
**Files likely touched:** `src/harness/runtime/policy.py` (move),
`src/harness/runtime/policy/__init__.py`,
`src/harness/runtime/policy/binding.py`.  
**Estimated scope:** M (3 files).

#### Task 26: Extract validation and finish the policy package

**Description:** Move change/graph validation and descriptor helpers into
`runtime/policy/validation.py`; leave `PolicyCatalog` as the orchestration object in
`runtime/policy/catalog.py`. Target modules under roughly 500 lines.

**Acceptance criteria:**
- [ ] Validation logic has one explicit owner.
- [ ] `PolicyCatalog.apply_changes`, restore, snapshot, descriptor, and composition behavior
      are unchanged.
- [ ] Cycle detection, dependency checks, lifecycle checks, and policy precedence still pass
      the existing example/plugin flows.
- [ ] No new escape hatches.

**Verification:**
- [ ] `just check`
- [ ] `examples/custom_capability.py`
- [ ] Temporary probe applies enable/configure/add changes and rejects invalid graph states.
- [ ] Manual configuration TUI smoke.

**Dependencies:** Task 25.  
**Files likely touched:** `src/harness/runtime/policy/catalog.py`,
`src/harness/runtime/policy/validation.py`, `src/harness/runtime/policy/__init__.py`.  
**Estimated scope:** M (3 files).

### Checkpoint: Policy

- [ ] Binding, validation, and catalog responsibilities are separate.
- [ ] Existing configuration flows and examples pass.
- [ ] `just check` passes.
- [ ] If this phase becomes disproportionate, record it as a follow-up and keep the package
      extraction that already landed.

### Phase 6 — Final Cleanup and Verification

#### Task 27: Remove transition scaffolding and update documentation

**Description:** Delete any temporary re-exports or import shims, update the architecture
module table and any references to moved entry points, and perform a dead-reference sweep.

**Acceptance criteria:**
- [ ] No source module is kept solely as a compatibility shim.
- [ ] No import points at a deleted module.
- [ ] `docs/ARCHITECTURE.md` module ownership matches the final layout.
- [ ] README/CLI docs reference the final entry points.
- [ ] No new `Any`, `cast()`, or type-ignore comments; suppression count is unchanged or
      lower.

**Verification:**
- [ ] `just check`
- [ ] Repository-wide import/reference search for old modules.
- [ ] `git diff --check`

**Dependencies:** Tasks 1–26.  
**Files likely touched:** docs, import-only cleanup files.  
**Estimated scope:** S/M (3–5 files).

#### Task 28: Final behavior-preservation verification

**Description:** Run the complete available verification and manual smoke matrix against the
finished layout. Record results and any accepted residual risks in this plan.

**Acceptance criteria:**
- [ ] `just check` passes.
- [ ] `examples/custom_capability.py` passes.
- [ ] `harness-host --help`, `harness-local --catalog`, and `hrns --help` pass.
- [ ] Manual TUI checklist passes: connect picker, chat prompt, queue/steer, session
      load/new/delete, model/effort, capability toggle, plugin add, MCP refresh, approval,
      question, command invocation.
- [ ] Manual host checklist passes: stdio session lifecycle, config update/replay, command
      invocation/replay, session list/delete, persistence reload.
- [ ] Line-count targets are met or documented exceptions are recorded.
- [ ] Pyrefly suppression count is ≤ baseline.
- [ ] No live imports reference deleted modules.
- [ ] Final results are recorded below.

**Verification:**
- [ ] Commands and manual checklist above.
- [ ] `git diff --stat` reviewed for accidental scope.
- [ ] `git diff --check`.

**Dependencies:** Task 27.  
**Files likely touched:** `PLAN.md` (results), no source changes expected.  
**Estimated scope:** S.

### Checkpoint: Complete

- [ ] All tasks checked or explicitly deferred with rationale.
- [ ] Static checks and builds pass.
- [ ] Manual smoke matrix passes.
- [ ] Target module ownership is documented.
- [ ] Baseline typing escape hatches did not increase.
- [ ] Human review before merge.

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Textual import cycles during widget moves | High | Extract shared formatting/messages first; run import smoke after every task. |
| Textual handlers stop being discovered after a split | High | Keep handlers on screen/widget classes; move only logic into plain collaborators. |
| Hidden state in mixins or controllers | High | Prefer constructor-injected collaborators and explicit method parameters over mixins with implicit screen attributes. |
| Behavior drift in event/update mapping | High | Make Task 22 a move-only change; compare emitted event types and payloads with a temporary probe. |
| Untracked tasks leak or swallow exceptions | Medium | Task 13 audits every `create_task`; use `run_worker` or a tracked helper with unmount cancellation. |
| No automated test suite | High | Use `just check`, import smoke, temporary headless probes, existing examples, and manual checklists; keep tasks small and reversible. |
| Large diff obscures a regression | Medium | One task per module/concern; checkpoint after each phase; no unrelated edits. |
| CLI entry-point move breaks installed scripts | Medium | Update `pyproject.toml` and test both installed entry points in Task 19. |
| Client reconnect logic regresses | High | Defer Task 24 until after lower-risk extraction; preserve cleanup suppression and test loss/reconnect explicitly. |
| Policy split expands scope | Medium | Treat Phase 5 as lower priority; land package extraction first and defer validation extraction if it needs a redesign. |
| Temporary re-exports linger | Low | Final Task 27 deletes all shims and searches for old import paths. |

## Open Questions

1. **ChatScreen collaborators vs. mixins:** This plan recommends delegation collaborators.
   Confirm before Task 9, because changing approach later invalidates several tasks.
2. **Policy phase:** Should Phase 5 be included now, or deferred until after the TUI/host/client
   changes are reviewed? The review calls it lower-risk than host/client but still material.
3. **`screens.py` rename:** This plan prefers `tui/chat/screen.py`. If a single `screens.py`
   facade is preferred, Task 14 can retain it with only `ChatScreen`.
4. **`host.py` public imports:** `server/app.py` and `runtime/local.py` import host types
   today. This plan updates them to `host_support.py`; confirm no external integration relies
   on importing those from `harness.runtime.host`.
5. **Tests:** The repository policy forbids adding tests unless explicitly requested. This
   plan uses temporary probes and manual smoke checks only. If the policy changes, add focused
   characterization tests before the first widget move.

## Completion Record

Fill this in during execution:

- Baseline line counts: `screens.py 3690`, `widgets.py 3215`, `host.py 1765`,
  `client.py 1416`, `policy.py 999`
- Final line counts: `____`
- Baseline Pyrefly suppressed diagnostics: `6`
- Final Pyrefly suppressed diagnostics: `____`
- `just check`: `passing at baseline (d235e88)`
- TUI smoke: `____`
- Host/client smoke: `baseline: examples/custom_capability.py, harness-host --help, harness-local --catalog, hrns --help all pass`
- Deferred items and rationale: `____`
