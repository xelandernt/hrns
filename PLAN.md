# Implementation plan: Shared native agent sessions with Loro

Status: planned; implementation has not started. Scope established through the
interview and idea-refinement discussion on 2026-11-12.

This is the current plan and task-list target, as requested. All Loro tasks and
checkpoints are recorded here, rather than in a second checklist. The previous
ACP/AG-UI plan and its implementation record remain below, unchanged; its open
human-review item remains in `tasks/todo.md`. That item is not implicitly approved
or superseded by this work.

## Outcome

Multiple TypeScript clients synchronize their Loro document replicas with a
Python server and participate in one native Pydantic AI agent's ongoing work.
Every session uses Loro internally, including sessions accessed only through ACP.
Python calls, one attached ACP client, and many Loro clients share one executor
and one durable session record. Exposing ACP and Loro endpoints is independent;
there is no internal ACP connection between Loro and the native agent.

Clients collaboratively edit pending input and explicitly mark it ready. The
agent always finishes its current model request and associated tool execution;
before the next model request, the server captures all eligible input together,
with authors and server-assigned consumption order. A captured version is what
the agent receives. Corrections to consumed input are new messages. Participants
can answer agent questions and approvals through either interface.

## Scope and decisions

- One process/worker owns live sessions and execution. Multiple client replicas do
  not create multiple agents or model runs.
- Native agents only. No external ACP agent bridge, daemon registry, Loro Streams,
  Flock, or general-purpose `loro-repo` replacement.
- One Loro document per session, with separate structures for pending input,
  consumed versions, native model history, display content, and interactions.
  Native history retains tool calls/results and model metadata; a display
  transcript cannot replace it. No event-sourcing framework or comprehensive
  journal of every internal callback is required.
- Loro is a server dependency. Mounting its synchronization endpoint is optional;
  client-only installs must not import the server SDK. AG-UI remains optional and
  read-only, observing the same executor.
- Admitted writers are trusted to edit the entire document. Ownership of output,
  execution records, and consumed versions is an application convention, not a
  field-level security guarantee. Arbitrary historical edits do not themselves
  request execution or rerun completed work.
- Admission and per-session authorization stay outside mutable document fields.
  Retain application-owned principal types and registration partitions. Loro peer
  IDs and client-supplied author labels are not authenticated identities.
- Provide a typed Python integration plus a mountable WebSocket synchronization
  endpoint. Publish the document and wire contracts for the TypeScript Loro SDK;
  a UI application and a published TypeScript client package are not required.
- Recommended persistence: a document-store protocol, an in-memory implementation
  for process-local usage, and an opt-in SQLite implementation for durable usage.
  A single-process deployment does not need a network database. Store metadata
  supports listing without opening every document.
- Persist document data before acknowledging durability. Preserve pending imports
  as well as integrated state. Transport delivery, durable storage, and actual
  model consumption are distinct acknowledgments.
- Restore conversations and pending input after restart. Mark interrupted work
  explicitly and pause execution until an explicit resume; do not automatically
  rerun uncertain tool side effects or claim exactly-once model/tool execution.
- No backwards-compatibility requirement: replace the JSON `StoredSession`
  persistence path and update repository callers rather than maintain two sources
  of truth or compatibility aliases. No migration of existing stored sessions is
  included.
- No new tests. Verification uses focused manual/offline exercises and existing
  lint, type, build, and documentation commands. Avoid `Any`, casts, and ignore
  comments. Planning does not authorize implementation in this turn.

Planned architecture decisions are also recorded, explicitly as proposals, in
`ARCHITECTURE.md`. Update that file with the implemented result as slices land.

## Existing integration points

| Location | Current responsibility | Planned change |
| --- | --- | --- |
| `server/acp/host.py` | Registrations, one executor, FIFO, turn handles | Share session ownership across interfaces; document-backed scheduling |
| `server/acp/runtime.py` | Native execution, ACP rendering, interaction callbacks | Separate native execution from transport delivery; add input consumption boundary |
| `server/acp/session.py` | Model history, transcript, settings, attachment documents | Distinguish durable session state from connection-owned editor state |
| `server/acp/store.py` | `StoredSession` persistence and principal-aware access | Replace persistence payload with Loro checkpoints, retain authorization semantics |
| `server/acp/interactions.py` | Wait for one attached ACP client | Session-owned interaction records resolved by ACP or Loro |
| `server/acp/adapter.py` | ACP requests, attachment, replay | Translate ACP into the same document-backed session operations |
| `server/ag_ui.py`, `server/host.py` | Native event conversion and read-only mounts | Preserve one native stream and observer lifecycle alongside Loro |
| `server/fastapi.py` | ACP authentication and transport | Reuse connection authentication conventions for independent Loro routes |

Paths in this table are relative to `hrns_slim/hrns/`.

## Behavioral contract

### Input and execution

1. A stable input ID identifies a contribution. Drafts do not execute. Ready input
   is eligible for the next consumption boundary, not an instruction to interrupt.
2. At that boundary, synchronously select all eligible input currently integrated
   on the server, capture its content/version and author, and assign monotonic
   consumption sequence numbers. Persist that decision before model dispatch.
   Cross-client send time and CRDT list order alone do not define execution order.
3. Changes arriving after capture cannot modify the captured content. Late edits
   remain visible but need a new ready contribution to affect future context.
   A client's local offline edit cannot retroactively change an earlier request.
4. Preserve separate states for draft/ready, captured, delivered to native history,
   and interrupted/failed. Do not equate enqueue acceptance with model consumption.
   Stable receipts prevent normal reconnect/replay from dispatching input twice.
5. An idle session starts from ready input. Input arriving during the apparent
   final response must cause another request or a subsequent execution after that
   response finishes. A serialized finish/start handoff prevents stranded input
   and concurrent executors. A paused session stays paused until explicit resume.
6. Questions and approvals resolve by the first valid answer accepted by the
   server for a pending interaction ID. Later answers cannot repeat a tool.
   A CRDT last-writer-wins field alone does not implement this rule.
7. An explicit cancel remains available; ordinary input never cancels work.
   Disconnect removes only connection-owned resources. Close/shutdown owns task
   cancellation, final persistence, and waiter/subscriber cleanup.

### Python and ACP contracts to finalize in T1

Keep explicit concepts for a contribution's acceptance/consumption receipt and an
execution's result handle. Do not pretend every mid-run contribution starts a
separate turn. Define how `Session.submit`, `TurnHandle`, pending-input inspection,
resume/clear, and ACP prompt responses map to these concepts, then update callers
directly. All transports must use the same acceptance and consumption operations;
do not retain an independent ACP FIFO with conflicting ordering rules.

Retain at most one ACP attachment, including generation checks for its MCP,
filesystem, and terminal resources. ACP is an interface to the native session,
not the owner of shared execution. Loro clients do not implicitly supply client
tools. ACP responses complete at the documented execution boundary even if Loro
participants contribute additional input during that execution.

## Feasibility evidence and first gate

The installed Pydantic AI source exposes public `AgentRun.enqueue` and
`RunContext.enqueue`, a `before_model_request` capability hook, and
`EnqueuedMessagesEvent`. Current upstream documentation describes `asap` input
delivery at the next request, or continuation if the run would otherwise finish.
This is a strong candidate, not a completed compatibility verification.

The current runtime uses `run_stream_events`, while external `AgentRun.enqueue`
is documented with `agent.iter()`. T1 must establish a supported streaming path
and verify availability at the selected minimum dependency version. Do not import
private drain/queue internals. Also prove that pending text can remain editable
until capture: eagerly copying it into an upstream queue at network arrival would
freeze it too early. Prefer a request-boundary capability or a supported public
enqueue/dequeue strategy after examining its exact timing and history semantics.

Sources to consult and pin during implementation:

- [Pydantic AI message injection](https://pydantic.dev/docs/ai/core-concepts/message-history/#injecting-messages-mid-run).
- [Pydantic AI AgentRun API](https://pydantic.dev/docs/ai/api/pydantic-ai/run/).
- [Pydantic AI capabilities](https://pydantic.dev/docs/ai/capabilities/overview/).
- [Official Python Loro bindings](https://github.com/loro-dev/loro-py).
- [Loro synchronization](https://www.loro.dev/docs/tutorial/sync) and
  [API reference](https://www.loro.dev/docs/api/js).
- `REPORT.md`, especially sections 6–9 and 14, as prior investigation rather than
  a guarantee for different SDK versions.

## Dependency order

T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12 → T13 → T14 → T15 → T16.

This conservative implementation order keeps runtime and document changes
sequential. T10's wire contract is fixed in T1, and T11 builds on its transport
engine. No delegated agent work is required. Independent documentation or SDK
exercises could be done separately after contracts stabilize, but shared runtime,
schema, and persistence edits should stay sequential.

## Tasks

### T1 — Prove native input delivery

**Description:** Resolve the highest-risk integration before restructuring state.
Manually exercise public Pydantic AI injection and Python/TypeScript Loro exchange;
write down exact input, handle, storage, schema, and transport contracts.

**Acceptance criteria:**
- [x] A controlled native run receives two authors' input after an in-flight tool
  finishes, retains it in native history, and continues after final-response input.
- [x] Editable input is captured at the documented boundary; supported public APIs,
  minimum SDK versions, Unicode offsets, pending-import persistence, and Python/JS
  binary exchange are verified without private upstream imports.
- [x] This plan and `ARCHITECTURE.md` record concrete handle semantics, checkpoint
  types, route/frame shapes, and dependency versions; a material gap is reported
  before dependent implementation rather than hidden behind a speculative API.

**Verification:** Manual/offline exercises against controlled model output and
two SDK replicas; record results and versions in this plan. No test files.
**Dependencies:** None.
**Files likely touched:** `PLAN.md`, `ARCHITECTURE.md`.
**Estimated scope:** Small, 2 documentation files; disposable feasibility work.

**T1 evidence (2026-11-12).** Versions exercised: `pydantic-ai-slim` 2.57.0,
`loro` 1.17.1 (CPython 3.14.5, macOS arm64), `loro-crdt` 1.17.0 (Node 26.7.0).
Disposable scripts were kept outside the repository.

Capture point and handle semantics resolved:

- A per-run `AbstractCapability.before_model_request` hook is the capture
  boundary. It reads eligible contributions from the session document at the
  instant of the model request, then sets `request_context.messages` to a new
  list ending in the captured `ModelRequest`s and extends `RunContext.messages`
  with the same objects. That assignment form is not deprecated; the upstream
  pending-message drain capability uses the same pair of writes. No private
  drain or queue imports are used.
- A run started from idle pre-captures ready input into `message_history` before
  dispatch, so the first request sees it and history records it. The hook still
  runs at every request, including requests after a tool result. A run started
  from a deferred-tool continuation passes `run_input=None`; history ends in a
  tool-return request, so the model is still called.
- `agent.run_stream_events(None, message_history=...)` supports the hook. A
  controlled streaming run with a tool call produced two capture boundaries:
  the injected messages reached the model, appeared in
  `result.all_messages()`, and retained per-message `metadata` (author and
  stable contribution id). `FunctionModel` needs `stream_function` for the
  streamed path; this is a test-double property only.
- Input arriving after an apparent final response is not injected by the ended
  run. The scheduler starts a subsequent execution whose pre-capture includes
  it. That is the documented continuation behavior; no upstream
  `priority="asap"` queue is used, because enqueuing at network arrival would
  freeze pending text before the boundary and choose a different capture rule.
- `TurnHandle` is tied to a contribution's consumption record, not to one
  "turn". One execution can complete several handles when it captures several
  contributions. A handle that is never consumed resolves cancelled; waiting on
  a handle never owns execution. ACP `session/prompt` waits on its handle, so
  its response completes when the execution that consumed it reaches an
  outcome, even if other participants contributed during that execution.

Loro contracts resolved:

- Python and JavaScript exchange snapshots and updates in both directions;
  scalar `status` and `LoroText` content with non-BMP emoji converged
  (`he😀llo` → `🦀:he😀llo`). `doc.import_()` is the Python import; imported
  updates do not fire `subscribe_local_update`, so broadcasting local updates
  cannot echo an import. A callback must return `True` to stay subscribed.
- Python `LoroText` indices are Unicode code points (`len_unicode`), JavaScript
  positions are UTF-16 (`len_utf16`). Server-produced streamed text is
  append-only via `push_str`; clients own pending-input text. Server-created
  input inserts its whole text once into an empty container. Documented offsets
  are code points.
- Checkpoint contract: `snapshot = doc.export(ExportMode.Snapshot())`,
  `pending = doc.export_pending_updates()`, `version = doc.oplog_vv.encode()`,
  `frontiers = doc.oplog_frontiers.encode()`. Restore imports the snapshot and
  then the pending bytes. `ImportStatus.pending` identifies retained pending
  imports. Version vectors implement `encode()`/`VersionVector.decode()` and can
  drive `ExportMode.Updates(vv)` for either direction.
- Minimum pinned versions: `loro>=1.17.1,<2` (server extra) and
  `loro-crdt@1.17.0` for the documented TypeScript example.

Wire contract fixed here for T10/T11:

| Frame | Tag | Payload |
| --- | --- | --- |
| `version` | `0x01` | UTF-8 JSON `{"schemaVersion":1,"vv":"<base64>","sessionId":"…"}` |
| `update` | `0x02` | raw Loro update bytes, one coalesced update per frame, ≤ 4 MiB |
| `snapshot` | `0x03` | `u32 chunkIndex`, `u32 chunkCount`, raw snapshot bytes, ≤ 256 KiB per chunk |
| `ack` | `0x04` | UTF-8 JSON `{"durable":true,"seq":N}` after the checkpoint is saved |
| `error` | `0x05` | UTF-8 JSON `{"code":"…","message":"…"}` |

Handshake: the server sends `version` on accept; the client replies with
`version`. Each side then sends `snapshot` chunks (first join) or `update`
frames computed from the peer's version vector. Live changes are `update`
frames carrying only local updates. Imported updates are persisted before an
`ack` is sent. Frame and peer-queue limits are 4 MiB and 128 frames; overflow
closes that peer (code 1013) and relies on reconnect resynchronization. Schema
version other than 1 closes with an `error` frame and code 1008.

### T2 — Establish a transport-independent session boundary

**Description:** Move native ownership behind a server session interface while
keeping ACP behavior working through its adapter. Avoid a wholesale directory
reorganization: extract only the boundaries needed by the next slices.

**Acceptance criteria:**
- [ ] A Python-created session runs without an ACP client; the same session can
  attach/replay through ACP without constructing a second agent.
- [ ] Core event and pending-interaction interfaces do not require an outbound ACP
  client; connection resources remain attachment-owned.
- [ ] Registration/output/dependency/principal typing remains intact; existing
  entry points remain usable after this slice.

**Verification:** Focused Python/ACP create-run-attach-disconnect exercise; `just lint`.
**Dependencies:** T1.
**Files likely touched:** `server/acp/host.py`, `server/acp/runtime.py`,
`server/acp/session.py`, `server/acp/adapter.py`, `server/session.py` (new).
**Estimated scope:** Medium, up to 5 files. Paths hereafter use the same server prefix.

### Checkpoint A — Execution boundary

- [ ] T1–T2 acceptance criteria and focused verification pass.
- [ ] Input injection uses supported APIs with a documented capture point.
- [ ] Review the resolved contracts before schema and persistence implementation.

### T3 — Represent one session in Loro

**Description:** Add the Python SDK and a typed session-document domain layer for
creation, reading, subscriptions, and validated local writes.

**Acceptance criteria:**
- [ ] A new document represents pending inputs, consumption records, native
  history, display content, settings/todos/skills, lifecycle, and interactions.
- [ ] Stable IDs, schema version, container identities, and author representation
  are documented; streaming/coedited text uses text containers, opaque native
  records use lossless typed serialization, and metadata uses scalar fields.
- [ ] Concurrent creation and text edits converge across Python/TypeScript;
  secrets and connection-owned resources are not serialized into the document.

**Verification:** Manual concurrent SDK edits including emoji; `just lint`.
**Dependencies:** T2.
**Files likely touched:** `server/loro/document.py`, `server/loro/models.py`,
`server/loro/__init__.py` (new), `hrns_slim/pyproject.toml`, `uv.lock`.
**Estimated scope:** Medium, 5 files.

### T4 — Replace session persistence with document checkpoints

**Description:** Make document checkpoints the canonical store payload while
retaining principal-aware creation/access/list/delete and registration partitioning.
Provide a document-backed in-memory implementation for existing lightweight use.

**Acceptance criteria:**
- [ ] Store/load round-trips integrated state plus pending imports, schema version,
  and metadata through one checkpoint contract; JSON `StoredSession` is replaced
  as the storage payload. Any transitional wire-facing view is derived from the
  document and cannot become a second persistence path.
- [ ] Session authorization still runs before resident lookup and restoration;
  trusted runtime saves remain independent of client connections.
- [ ] There is one runtime source of truth; native-history caches are derived
  from the document rather than independently writable persisted histories.

**Verification:** Manual restore, principal-denial, and per-agent partition checks;
`just lint`.
**Dependencies:** T3.
**Files likely touched:** `server/acp/store.py`, `server/acp/host.py`,
`server/acp/runtime.py`, `server/loro/store.py`, `examples/fastapi_app.py`.
**Estimated scope:** Medium, 5 files.

### Checkpoint B — Canonical state

- [ ] T3–T4 acceptance criteria pass; `just check` passes.
- [ ] ACP-only and Python-only sessions use Loro internally.
- [ ] No separate mutable JSON persistence path or compatibility shim remains.

### T5 — Persist sessions in SQLite

**Description:** Implement opt-in durable document storage with lightweight session
metadata, atomic checkpoints, and explicit recovery behavior.

**Acceptance criteria:**
- [ ] SQLite atomically stores a snapshot and pending imports, with agent/session
  partitioning and metadata-backed listing; deletion handles live and stored state.
- [ ] A fresh process restores native context, pending inputs, and published partial
  display; interrupted execution is marked and paused rather than replayed.
- [ ] Persistence failures prevent durability acknowledgment or new model dispatch;
  graceful shutdown flushes state without losing pending operations.

**Verification:** Manual close/reopen and interrupted-checkpoint scenarios on a
temporary database; `just lint`.
**Dependencies:** T4.
**Files likely touched:** `server/loro/sqlite.py`, `server/loro/store.py`,
`server/loro/__init__.py`.
**Estimated scope:** Medium, 3 files.

### T6 — Publish one native run into the document

**Description:** Make a Python-submitted prompt produce document-backed live output,
native context, tool records, and lifecycle without requiring a sync endpoint.

**Acceptance criteria:**
- [ ] Text/reasoning and tool activity update stable records from one native event
  stream; batching bounds write frequency and flushes at lifecycle boundaries.
- [ ] Completed native messages round-trip losslessly; partial display survives
  interruption without being presented as completed model history.
- [ ] Settings, todos, invoked skills, and usage remain session-scoped; document
  subscriptions cannot block execution or cause an output-to-input feedback loop.

**Verification:** Manual streamed text/tool run, typed output, deferred run, and
cancellation; inspect reopened document; `just lint`.
**Dependencies:** T5.
**Files likely touched:** `server/acp/runtime.py`, `server/acp/session.py`,
`server/loro/document.py`, `server/loro/publisher.py`, `server/session.py`.
**Estimated scope:** Medium, 5 files.

### Checkpoint C — Durable single-client flow

- [ ] T5–T6 acceptance criteria pass; `just check` passes.
- [ ] One native execution publishes live content and restores full conversation context.
- [ ] Interrupted work is distinguishable from completed work without automatic replay.

### T7 — Consume collaborative input at model boundaries

**Description:** Connect eligible document input to the public native mechanism
proved in T1, preserving editable drafts until the capture boundary.

**Acceptance criteria:**
- [ ] Each boundary captures all integrated ready messages with stable IDs,
  authors, frozen content/version, and server-assigned order before dispatch.
- [ ] Native delivery is reconciled to consumption receipts; duplicate imports,
  unrelated document edits, and late pending-text edits cannot redeliver input.
- [ ] New input never cancels a running model/tool step; corrections to consumed
  input are separate contributions and tool-call/result ordering remains valid.

**Verification:** Manual two-replica contributions during a controlled tool call,
edit-before/edit-after capture, duplicate delivery, and model-failure scenarios;
`just lint`.
**Dependencies:** T6.
**Files likely touched:** `server/loro/input.py`, `server/loro/document.py`,
`server/acp/runtime.py`, `server/session.py`.
**Estimated scope:** Medium, 4 files.

### T8 — Schedule idle and finishing sessions

**Description:** Use one scheduler for document-backed input and explicit execution
handles, including the race between final output and newly ready input.

**Acceptance criteria:**
- [ ] Ready input starts an idle session; input arriving at run completion is
  consumed by a continuation or the next execution without stranded work.
- [ ] Single-owner scheduling survives detach/reconnect; pause/cancel/resume/clear
  use the same pending records and every handle reaches a documented outcome.
- [ ] Restored sessions remain paused; resume reconciles captured-but-interrupted
  input explicitly without silently repeating uncertain tool execution.

**Verification:** Manual idle/start/finish races, deferred wait, cancellation,
shutdown, and restored resume; `just lint`.
**Dependencies:** T7.
**Files likely touched:** `server/acp/host.py`, `server/session.py`,
`server/loro/input.py`, `server/acp/runtime.py`.
**Estimated scope:** Medium, 4 files.

### Checkpoint D — Ongoing participation

- [ ] T7–T8 acceptance criteria pass; `just check` passes.
- [ ] Two document replicas influence ongoing work without cancelling a step.
- [ ] No input is stranded at completion or duplicated by reconnect; consumed
  versions remain the versions used by the model.

### T9 — Resolve session-owned interactions

**Description:** Store pending questions and approvals as session domain records,
with one server acceptance path independent of transport.

**Acceptance criteria:**
- [ ] Pending requests include stable IDs and validation data; an eligible Loro
  answer resolves the request without an ACP attachment.
- [ ] The server records the first accepted valid answer; duplicate, conflicting,
  stale, or cancelled answers do not resolve another request or repeat a tool.
- [ ] Disconnect retains pending interactions; cancel invalidates them; restart
  exposes interrupted decisions without claiming a live continuation exists.

**Verification:** Manual conflicting approval/question answers, unavailable client,
detach, cancel, and restoration scenarios; `just lint`.
**Dependencies:** T8.
**Files likely touched:** `server/acp/interactions.py`, `server/acp/runtime.py`,
`server/loro/interactions.py`, `server/loro/document.py`.
**Estimated scope:** Medium, 4 files.

### T10 — Synchronize replicas through a bounded transport engine

**Description:** Implement transport-neutral peer synchronization using the frame
contract fixed in T1. This slice supports connected peers without HTTP routing.

**Acceptance criteria:**
- [ ] Initial join atomically captures synchronization state and subscribes to
  later changes; version-vector exchange reconciles local offline updates and
  server changes in both directions without an echo loop.
- [ ] Updates are durably persisted before a durability acknowledgment; pending
  imports are retained and trigger dependency catch-up rather than execution.
- [ ] Frame/update limits and bounded peer queues protect execution from slow
  peers; reconnect resynchronizes instead of silently losing deltas. Snapshot
  bootstrap has an explicit size/chunk policy and peer-ID collision policy.

**Verification:** Manual two-peer exchange, out-of-order imports, duplicate updates,
reconnect with unsent edits, and slow-peer overflow; `just lint`.
**Dependencies:** T9.
**Files likely touched:** `server/loro/protocol.py`, `server/loro/sync.py`,
`server/loro/store.py`, `server/loro/document.py`.
**Estimated scope:** Medium, 4 files.

### Checkpoint E — Shared state and decisions

- [ ] T9–T10 acceptance criteria pass; `just check` passes.
- [ ] Conflicting interaction answers produce one accepted result.
- [ ] Python/TypeScript replicas converge after reconnect without blocking execution.

### T11 — Mount authenticated Loro sessions

**Description:** Expose `mount_loro(app, prefix=..., authenticate=...)` with a
per-session WebSocket and a minimal session creation/discovery surface so a
TypeScript-only client can start or join without first connecting over ACP.

**Acceptance criteria:**
- [ ] The documented lifecycle routes and WebSocket under the chosen agent/session
  namespace reuse canonical registrations and synchronized session restoration.
- [ ] Authentication uses `HTTPConnection`; session access operations distinguish
  read/sync/write/create/list as needed and are checked before exposing content.
- [ ] Joining and importing through the route uses T10; disconnect does not cancel
  accepted work, and shutdown closes peers and flushes persistence.

**Verification:** Manual local WebSocket/HTTP create, join, list, denied access,
unknown-session, reconnect, and shutdown with two SDK clients; `just lint`.
**Dependencies:** T10.
**Files likely touched:** `server/loro/routes.py`, `server/acp/host.py`,
`server/acp/store.py`, `server/fastapi.py`.
**Estimated scope:** Medium, 4 files.

### T12 — Participate in one session through ACP and Loro

**Description:** Complete ACP translation into shared input and interaction
operations, using the handle/response semantics resolved in T1.

**Acceptance criteria:**
- [ ] An ACP prompt enters the same document-backed path as Loro/Python input;
  ACP and Loro clients observe one output and consistent execution outcomes.
- [ ] ACP questions/approvals share T9 request IDs and acceptance, including a
  Loro answer winning while an ACP request is outstanding and vice versa.
- [ ] Replay and origin suppression show each message once; one-attachment and
  generation-bound client-tool rules remain intact without gating Loro interaction
  on ACP capabilities or disconnecting native work when ACP leaves.

**Verification:** Manual combined ACP plus two Loro clients, raced interaction
answers, mid-run join, and lost client-tool result; `just lint`.
**Dependencies:** T11.
**Files likely touched:** `server/acp/adapter.py`, `server/acp/runtime.py`,
`server/acp/interactions.py`, `server/acp/host.py`, `server/acp/session.py`.
**Estimated scope:** Medium, 5 files.

### Checkpoint F — Both public interfaces

- [ ] T11–T12 acceptance criteria pass; `just check` passes.
- [ ] TypeScript-only clients can create/join without an ACP bootstrap connection.
- [ ] One ACP client and multiple Loro clients participate in one native executor.

### T13 — Preserve AG-UI and session lifecycle

**Description:** Keep existing read-only observers and settings consistent with
live contributions, while completing cancellation, deletion, and recovery cleanup.

**Acceptance criteria:**
- [ ] AG-UI converts the same single native stream; injected messages appear once,
  with correct identities and no false completion during continuation/approval.
- [ ] Snapshot/replay agrees with persisted native context and partial display;
  settings and provider changes retain their documented scope and timing.
- [ ] Close/delete/shutdown resolves handles and subscriptions; restore marks
  interrupted runs and obsolete interactions without resurrecting client tools.

**Verification:** Manual combined AG-UI/ACP/Loro observation during continuation,
cancel, settings changes, close/delete, and reopen; `just lint`.
**Dependencies:** T12.
**Files likely touched:** `server/ag_ui.py`, `server/host.py`,
`server/acp/host.py`, `server/acp/runtime.py`, `server/loro/sync.py`.
**Estimated scope:** Medium, 5 files.

### T14 — Expose the integration through shipped entry points

**Description:** Finalize public exports, stdio/CLI runtime wiring, and a concrete
FastAPI example with native server tools and opt-in durable storage.

**Acceptance criteria:**
- [ ] Public Python exports and existing launchers use the canonical Loro session
  core; server-only imports do not leak into client-only installations.
- [ ] The example mounts ACP and Loro independently and uses server-side tools so
  Loro-only participation does not depend on an ACP client's filesystem.
- [ ] Full/slim package dependency resolution and the optional AG-UI extra work;
  platform/Python requirements of the pinned Loro wheel are documented.

**Verification:** CLI help, stdio startup, example import/startup, install-matrix
inspection, and `just check`.
**Dependencies:** T13.
**Files likely touched:** `server/__init__.py`, `server/acp/server.py`,
`server/runtime.py`, `examples/fastapi_app.py`, `hrns_slim/pyproject.toml`.
**Estimated scope:** Medium, 5 files.

### Checkpoint G — Shipped integration

- [ ] T13–T14 acceptance criteria pass; packaging/build checks pass.
- [ ] Existing ACP and read-only AG-UI flows remain usable with the shared core.
- [ ] Restart limits and trusted-writer behavior match the documented contract.

### T15 — Document the session and wire contracts

**Description:** Publish implementable Python/TypeScript integration instructions
and update architecture to describe the implemented state rather than the proposal.

**Acceptance criteria:**
- [ ] Documentation specifies root/container types, IDs, readiness, capture,
  author/order semantics, interaction resolution, frame encodings, handshake,
  acknowledgments, errors, limits, reconnect, and schema-version rejection.
- [ ] A minimal TypeScript Loro SDK example creates/joins, edits pending input,
  marks it ready, receives output, and answers a request; no full UI is required.
- [ ] Python persistence/mount examples and `ARCHITECTURE.md` explain one-server
  ownership, trusted writers, native history vs display, and restart semantics.

**Verification:** Follow both language examples against the server and run
`just docs-build`; check cross-links and public signatures.
**Dependencies:** T14.
**Files likely touched:** `docs/loro.md`, `docs/mount-acp.md`,
`docs/ag_ui.md`, `ARCHITECTURE.md`, `zensical.toml`.
**Estimated scope:** Medium, 5 files.

### T16 — Validate the complete collaborative flow

**Description:** Perform the final combined exercise, update package READMEs and
record implementation evidence and remaining limitations in this plan.

**Acceptance criteria:**
- [ ] Two TypeScript replicas and an ACP client share a session: concurrent edits,
  ongoing corrections, questions/approvals, final-response input, offline reconnect,
  cancellation, and process restart satisfy this plan's behavioral contract.
- [ ] Native tool context, todos/skills/settings, response identities, and partial
  display survive the appropriate boundaries; slow peers do not delay execution.
- [ ] Final lint/type/build/docs gates pass; no tests, compatibility layer,
  external-agent bridge, client UI, or distributed runtime was added.

**Verification:** Focused combined manual/offline exercise, `just check`, and
`just docs-build`. Record SDK versions, scenarios, and observed limitations here;
do not add a new automated test suite or repeat successful gates without cause.
**Dependencies:** T15.
**Files likely touched:** `PLAN.md`, `README.md`, `hrns_slim/README.md`,
`examples/README.md`, `ARCHITECTURE.md`.
**Estimated scope:** Medium, 5 documentation files.

### Checkpoint H — Complete

- [ ] T15–T16 acceptance criteria pass; final evidence and limitations are recorded.
- [ ] All confirmed requirements are implemented and ready for human review.
- [ ] Human review of the new session, persistence, and synchronization contracts.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Upstream injection freezes input too early or omits native history | High | T1 verifies capture timing and persistent history through public APIs before refactoring |
| Crash between capture, model dispatch, and delivery recording | High | Persist explicit stages; pause uncertain work on restart; do not promise exactly-once side effects |
| Whole-document writes alter execution records or spoof author labels | Accepted constraint | Treat writers as trusted, derive authority from admission, document that field ownership is conventional |
| Full snapshots omit unintegrated imports | High | Atomically preserve snapshot plus pending bytes; reconcile dependencies on reconnect |
| Streaming creates excessive operations or disk writes | Medium | Batch output, measure document growth, flush at boundaries; defer destructive compaction |
| Multiple sources trigger duplicate dispatch or answers | High | Stable contribution/request IDs, serialized acceptance, durable receipts, one scheduler |
| ACP result semantics conflict with mid-run contributions | High | Resolve handle/execution semantics in T1; update callers directly instead of adding a second queue |
| Runtime refactor duplicates event conversion | Medium | One native stream with composed publishers; AG-UI identity and deferred-run verification |
| Client SDK/runtime versions diverge | Medium | Pin verified versions, reject unsupported schemas, publish a minimal wire contract |

## Remaining decisions and defaults

- T1 fixes exact public types and route/frame names; examples such as
  `mount_loro` describe intent until signatures are verified.
- Default persistence is process-local unless an application supplies SQLite or
  another document-store implementation. Durable acknowledgments only claim the
  configured backend's guarantees. If applications need another backend, implement
  the same protocol rather than another session representation.
- Restored interrupted work remains paused. Ordinary ready input on a healthy idle
  live session starts automatically. An explicit resume must define disposition of
  uncertain captured input without repeating side effects silently.
- Presence/cursors, attachments/blob storage, historical forks, automatic compaction,
  and TypeScript SDK packaging are deferred; basic synchronization needs none of them.
- No feature implementation starts as part of writing this plan.

## Implementation record

Pending. Check off a task only after its acceptance criteria and verification are
satisfied. Record T1 evidence first. The historical record below belongs to the
previous ACP/AG-UI implementation.

---

# Previous plan: ACP server API and read-only AG-UI observation

Status: implemented; offline validation completed. Human review pending.
Task checklist: [tasks/todo.md](tasks/todo.md).

## Confirmed outcome

Introduce `AcpServer` with factory-only agent registration and programmatic session execution. `HrnsServer` extends it with read-only AG-UI observation. Sessions run independently of client connections. One ACP client may attach to a session to interact with it; any number of AG-UI subscribers may observe it.

Mounting is explicit and independent:

```python
server.mount_acp(app, prefix="/acp")
server.mount_ag_ui(app, prefix="/ag-ui")
```

`AcpServer` provides `mount_acp`; `HrnsServer` adds `mount_ag_ui`. Both mounts reference the same agent registrations and sessions. Mounting observation routes never starts an agent run.

## Scope and constraints

- Replace the hosting API, extract session execution, update existing entry points, and add read-only observation routes.
- Register factories only, called once per live conversation; reconnecting and observing reuse that conversation's agent.
- Server execution survives ACP and AG-UI disconnects. Process restart recovery restores saved conversation state, not running tasks, pending interactions, or queued work.
- All admitted users have equal server permissions/scope. Retain authentication, but do not introduce user-specific session partitions or roles.
- Follow-ups execute after the active turn; no injection into an active model request.
- One ACP attachment per session. No simultaneous ACP controllers, takeover protocol, or ACP observers.
- Python owns queue inspection/resume/clear. No custom ACP queue-management methods or reconnect queue UI.
- AG-UI routes accept no prompts, client tools, approval responses, history replacements, or state mutations. No frontend application is included.
- Use Pydantic AI's public AG-UI converter and encoder. Do not reimplement its text/tool/reasoning event translation.
- No compatibility aliases, legacy API wrappers, or dual implementations. Update repository callers directly.
- Do not write new tests. Use existing lint/build gates and focused offline/manual checks. Avoid `Any`, casts, and type-ignore comments.
- Put implemented architecture and decisions in `ARCHITECTURE.md` when the relevant implementation lands; do not describe this proposal as existing behavior there.

## Current implementation and required changes

| Current location | Behavior | Required change |
| --- | --- | --- |
| `hrns_slim/hrns/server/fastapi.py` | Factory and adapter per WebSocket; optional store per principal | Mount registered agents onto one shared runtime |
| `hrns_slim/hrns/server/acp/adapter.py` | Owns connections, sessions, execution, approvals, replay | Keep wire dispatch; extract connection-independent execution/state |
| `hrns_slim/hrns/server/acp/adapter.py:load_session` | Restores a separate state and cancels a prior resident turn | Attach to an existing live state; restore only when absent |
| `hrns_slim/hrns/server/acp/adapter.py:prompt` | Request cancellation normally cancels its turn | Separate request waiter from server-owned task |
| `hrns_slim/hrns/server/acp/session.py` | Configuration input requires an ACP client | Separate server session configuration from client attachment configuration |
| `hrns_slim/hrns/server/acp/adapter.py:session_history` | Exposes committed history, generally updated at turn completion | Publish live events independently of committed model history |
| `hrns_slim/hrns/client/turn.py` | Primarily derives turn completion from its own prompt response | Handle state of server-started turns after attachment |
| `hrns_slim/hrns/server/acp/server.py` | Single-agent convenience entry points | Route stdio and network hosting through the new runtime |

## Public Python API

The public API is implemented in `hrns_slim/hrns/server/acp/host.py` and `hrns_slim/hrns/server/host.py`.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from hrns.server import HrnsServer
from hrns.server.agent import build_agent


def make_coder_agent():
    return build_agent("provider:model", cwd="/srv/project")


server = HrnsServer()
coder = server.add_agent("coder", factory=make_coder_agent)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with server:
        yield


app = FastAPI(lifespan=lifespan)
server.mount_acp(app, prefix="/acp")
server.mount_ag_ui(app, prefix="/ag-ui")
```

Registration returns a typed `RegisteredAgent` handle. Its `new_session(cwd=...)` and `get_session(session_id)` methods select the agent without repeatedly passing its ID. Preserve the existing dependency/output typing through registration/session/turn handles; heterogeneous registrations must not require exposing `Any`.

`add_agent` accepts `factory`, per-agent session store, model resolver, permission policy, usage limits, and server/client configuration callbacks as needed from the existing adapter's options. Keep these as explicit typed keyword arguments rather than introducing a general plugin registry. Reject duplicate IDs and registration after server startup. Validate IDs as a single safe route segment.

```python
session = await coder.new_session(cwd="/srv/project")
turn = await session.submit("Investigate the deployment failure.")

# Independent of any ACP connection:
follow_up = await session.submit("Then inspect the migration.")
result = await turn.result()

await session.cancel()
queued = await session.pending_prompts()
await session.resume_queue()
await session.clear_queue()
```

`submit` accepts text and the supported typed prompt-content form. It accepts/schedules a turn and returns a `TurnHandle`; `result()` waits for that particular turn's outcome. Cancelling a waiter does not cancel the turn. Runtime failures complete the handle exceptionally; cancellation has a typed cancelled outcome translated into ACP's cancelled prompt response.

A registered factory takes no authenticated principal: server scope is fixed for the registration. The initial version can retain the existing adapter's supported dependency/output constraints where necessary; do not silently broaden supported output forms. Record any typing limitation during Task 1.

## Ownership and lifecycle

| Owner | Responsibilities |
| --- | --- |
| `AcpServer` | Registrations, live session lookup, lifecycle/task ownership, admission integration |
| `RegisteredAgent` | Factory/configuration, model resolver and store scope, typed session access |
| Session runtime | Agent instance, history, settings, todos/skills, transcript, active turn, FIFO queue, pending interactions |
| ACP attachment | Outbound client, negotiated capabilities, MCP caller/channel, client toolsets, editor/terminal resources |
| `HrnsServer` observer component | AG-UI conversion, current-run replay buffer, read-only snapshots/subscriptions |
| Transport | Authentication, serialization, delivery, disconnect cleanup |

Use one canonical live session per `(agent_id, session_id)`, with synchronized restoration. A store configured for a registration must not let another registration load/list its sessions. Define agent scoping in the persistence contract; if a backend is shared, it must explicitly partition by agent. Never infer ownership from knowledge of a session ID alone.

The base runtime exposes typed native events and lifecycle snapshots through a small internal subscription interface. The AG-UI component subscribes through composition. `AcpServer` must not import AG-UI types or require AG-UI at import time.

Server context startup enables execution. Shutdown rejects new submissions, cancels active and queued work, resolves waiting handles, and closes owned resources. Client detach only removes connection-owned resources. Initial deployment is one process/worker per runtime; cross-worker task ownership and fan-out are out of scope.

### Configuration split

A server `SessionContext` contains agent ID, session ID, and authoritative cwd, without requiring a client. Its configuration provides dependencies, server capabilities/toolsets, and workspace. An attachment configuration contains the outbound client, negotiated capabilities, MCP declarations, and caller/channel; it provides connection-owned resources only.

Run server configuration once for a new/restored live session. Run attachment configuration for each attachment. Detach must not replace server dependencies or workspace. Loading an existing session must validate its cwd rather than silently change its server workspace.

Client catalogs may change at model-step boundaries using upstream dynamic toolset support. Every client tool dispatch also validates its attachment generation, because control can change after a model request advertised its tools. Do not route an old call to a new client's same-named tool.

### Queue semantics

- Programmatic and ACP submissions share one FIFO queue in acceptance order. At most one turn runs per session.
- Queued prompts enter model history only when execution starts. Pending prompts are not replayed as completed conversation entries.
- Accepted work survives connection loss; the disconnected JSON-RPC request/response association does not. Never automatically resubmit a disconnected prompt.
- `cancel` stops the active turn and pauses queued follow-ups. `resume_queue` and `clear_queue` are Python-only operations; clear completes discarded handles as cancelled. An empty cancellation does not create work.
- New submissions to a paused queue remain queued until explicit programmatic resume. The ACP client does not gain an implicit resume/clear extension.
- Errors pause remaining queued work so failures cannot trigger an uncontrolled sequence of follow-ups. Explicit resume continues it.
- Queues are process-local; no pending-work persistence or recovery across server restart.
- Existing TUI-local drafts/queues remain separate; do not silently migrate them to server state.

### ACP attachment and interactions

`session/new` creates and attaches a new session. `session/load` reserves the one ACP attachment, reuses/restores the live session, and replays published history before enabling ordinary live delivery and pending requests. Reject another live attachment; reconnect after detach is allowed. Scope load/list/delete to the endpoint's agent.

Replay includes current-turn ACP content through an atomic snapshot/subscription boundary. Persisted model history remains committed history; observation/replay does not commit incomplete content to future model context.

Prompt handlers submit to the runtime and shield only their wait on the turn handle. Task cleanup belongs to the runtime. `session/cancel` cancels active work and pauses the queue according to the shared cancellation contract. Preserve ACP v1 `session/close` cancellation semantics; a transport disconnect is not a `session/close` request.

Server-started turns need lifecycle synchronization because an attaching client has no originating prompt request. Use a narrowly scoped, advertised `_hrns/session/state` notification and namespaced load-response `_meta` for status, active turn ID, revision, and last outcome. This extension is not queue management. Add support in the existing client without requiring it to understand or manage pending prompts.

Approvals and questions are session-owned pending interactions. With no ACP client, execution waits and observers see waiting state. Dispatch after attachment synchronization. Disconnect invalidates the old request but retains the pending decision for the next attachment. An attachment generation prevents late answers from affecting execution. Cancellation resolves waits and clears obsolete interactions.

Distinguish no client from a connected client lacking the required capability: the latter follows the adapter's existing unsupported-interaction outcome instead of waiting indefinitely for an impossible response.

MCP registrations and terminal IDs belong to their connection. Missing/unavailable client tools fail through the normal tool error path; do not wait for a previous client or replay side-effecting executions. A lost result may mean execution outcome is unknown. Preserve MCP-over-ACP's distinction between unavailable registration, inner unknown-tool error, and a tool execution error.

Model/provider routing remains scoped to the registered agent. Existing `providers/set` and `providers/disable` mutate its resolver for all its sessions, because those ACP methods have no session ID. Notify affected attached sessions through the registration runtime. Do not introduce new provider authorization policy as part of this work.

## Read-only AG-UI API

| Method/path | Purpose |
| --- | --- |
| `WS /acp/{agent_id}` | Standard ACP connection to one registration |
| `GET /ag-ui/{agent_id}/sessions/{session_id}` | Current read-only session/history snapshot |
| `GET /ag-ui/{agent_id}/sessions/{session_id}/events` | Long-lived AG-UI SSE subscription |

Apply admission checks to both mounts with equal server scope. HTTP and WebSocket authentication must share a suitable typed connection abstraction; neither observation route is public merely because it is read-only. Snapshot responses use AG-UI message conversion, not a new parallel frontend message schema. Advertise read-only endpoint semantics through OpenAPI and explicit application metadata; do not invent a standard AG-UI read-only capability.

Do not mount `AGUIAdapter.dispatch_request` or accept `RunAgentInput`. Unsupported mutation methods receive 405. Subscribing while idle waits for future execution; disconnecting stops only that subscription.

### Conversion and replay

Use `AGUIEventStream` once per native Pydantic AI run, with stable server-assigned thread/session and run IDs. Transform the same native events already sent to ACP and broadcast the converted output; never iterate a single native generator independently from two consumers or start a second run for an observer.

An application turn can contain multiple native runs around deferred tool approvals. Give each native run its proper AG-UI start/finish/error lifecycle, and publish session-level waiting/running status via a documented `STATE_SNAPSHOT` payload. The next native run continues the same thread. Do not falsely mark an entire application turn complete when one native run pauses for approval.

For an observer joining during a native run, atomically capture the completed-history baseline from before that run plus its converted event prefix, then subscribe to later events. Send `MESSAGES_SNAPSHOT` for the baseline, replay the current run from its beginning (including start events), and then deliver live events. Include the submitted user message exactly once. This avoids inventing a custom partial-message reducer.

Use `AGUIAdapter.dump_messages` for history conversion. Verify identity continuity between dumped messages and the live converter: reconnect or the next baseline must neither duplicate messages nor target deltas at unrelated IDs. Preserve any necessary ID bookkeeping in the observer component, without writing UI metadata into model-facing history indiscriminately.

Retain only the completed-history baseline and event buffer required for the current native run. After completion, synchronize the next baseline using committed native history. Failure/cancellation must terminate the observer lifecycle and preserve already-visible display content without treating it as committed model history. An existing observer may receive several runs on one SSE connection.

Per-subscriber queues are bounded. On overflow, disconnect that subscriber and allow a fresh snapshot/replay subscription; never silently drop arbitrary deltas. A slow subscriber cannot block model execution, ACP delivery, or other observers. Keep replay/subscribe ordering under short state locks, with no network I/O under a lock.

## Implementation order

Detailed acceptance criteria, verification, dependencies, and file scope are in [tasks/todo.md](tasks/todo.md).

1. [T1](tasks/todo.md#t1--confirm-public-contracts-and-conversion-feasibility): public contracts and offline AG-UI feasibility.
2. [T2](tasks/todo.md#t2--create-sessions-through-the-agent-registry): registration and client-independent session creation.
3. [T3](tasks/todo.md#t3--execute-one-server-owned-turn): one programmatic turn with server-owned lifetime.
4. [T4](tasks/todo.md#t4--schedule-programmatic-follow-ups): FIFO queue, cancellation, pause/resume/clear.
5. [T5](tasks/todo.md#t5--attach-acp-to-live-sessions): per-agent mounting, attachment, replay.
6. [T6](tasks/todo.md#t6--synchronize-server-started-turns-in-the-acp-client): lifecycle state and existing client integration.
7. [T7](tasks/todo.md#t7--retain-pending-interactions-across-disconnects): disconnected approvals/questions.
8. [T8](tasks/todo.md#t8--bind-client-tools-to-the-current-attachment): generation-aware client resources.
9. [T9](tasks/todo.md#t9--publish-ag-ui-from-existing-execution): upstream conversion and native-run lifecycle.
10. [T10](tasks/todo.md#t10--mount-read-only-observation): HTTP/SSE, history handoff, subscriber cleanup.
11. [T11](tasks/todo.md#t11--move-existing-launchers-to-the-server-api): CLI, runtime, stdio, examples, direct API replacement.
12. [T12](tasks/todo.md#t12--document-and-verify-the-complete-flow): user/API docs, architecture, final gates.

Sequential dependency chain: T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12. T9's conversion feasibility is checked in T1 so a protocol/identity mismatch is found before broad refactoring. Checkpoints follow every two tasks. No delegated agent work is required.

## Risks and decisions to resolve during the first slice

| Risk | Handling |
| --- | --- |
| Scope of extraction from a large adapter | Move execution semantics without rewriting provider, skill, approval, or tool behavior unnecessarily |
| Generic typing across heterogeneous registrations | Typed registration/session handles and a small private non-generic management boundary; avoid `Any` or casts |
| Current-run snapshot vs streamed message IDs | Offline prototype with join/rejoin and deferred runs before committing the observer storage contract |
| ACP client's behavior when joining a running session | Explicit state synchronization and focused headless client verification |
| Active-run replay buffer can grow large | Measure retention in the feasibility check and document its memory cost; do not silently truncate. If a replay reducer or disk spool becomes necessary, surface that substantial addition for a decision |
| Unsupported capabilities or disappearing tools | Distinguish no attachment, unsupported attachment, unavailable binding, and uncertain execution result |
| Same backend reused for two registrations | Explicit per-agent storage partitioning; reject unsupported ambiguous configuration |
| Shutdown or cancellation leaves unresolved waiters | Runtime owns task teardown, pending interactions, queued handles, and subscriber cleanup |

T1 must resolve exact public type signatures, storage partitioning shape, AG-UI version constraints/extra placement, and replay identity behavior using public upstream APIs. If satisfying them needs substantial additional machinery beyond this plan, report the concrete gap for a decision rather than adding it silently.

## Validation policy

No new tests. Each slice has a focused offline/manual scenario in the checklist, using a local `FunctionModel` or equivalent existing model facility rather than live providers. These are verification exercises, not new committed test suites. Run `just lint` and `just build` at implementation checkpoints; `just check` combines those gates. Run `just docs-build` when the public documentation is updated. Do not run implementation gates solely for this markdown-only planning change.

Final smoke flow: register two agents; start a session and turn in Python; subscribe two observers; join through ACP during execution; queue a follow-up; disconnect ACP; wait on an approval; reconnect and answer; verify continued execution and consistent messages; cancel with queued work; resume/clear in Python; close observers; shut down the server. Confirm agent/session isolation, rejected second ACP attachment, read-only HTTP behavior, and no duplicate model runs. Report offline validation separately from any later live-provider confirmation.

## Sources checked

- [Pydantic AI UI event streams: encoding without a request](https://pydantic.dev/docs/ai/integrations/ui/overview/#encoding-events-without-a-request): standalone transformer and per-run lifetime.
- [Pydantic AI AG-UI integration](https://pydantic.dev/docs/ai/integrations/ui/ag-ui/): conversion, encoding, deferred approval lifecycle.
- [Pydantic AI AG-UI API](https://pydantic.dev/docs/ai/api/ui/ag_ui/): `AGUIEventStream`, `AGUIAdapter.dump_messages`.
- [AG-UI events](https://docs.ag-ui.com/concepts/events): message snapshots and run lifecycle.
- [ACP v1 session setup](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/session-setup.mdx): load/replay, resume, close.
- [ACP v1 prompt turn](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/prompt-turn.mdx): prompt response lifecycle and cancellation; no queue-management contract.
- [ACP extensibility](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/extensibility.mdx): advertised custom methods/notifications and namespaced metadata.
- [MCP-over-ACP draft](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/rfds/mcp-over-acp.mdx): connection-owned registrations and error provenance.

Installed source was also inspected under `.venv/lib/python3.14/site-packages/pydantic_ai/ui/` and `pydantic_ai/toolsets/_dynamic.py`. Recheck against the declared dependency range during T1; installed support alone is not a dependency guarantee.

## Implementation record

T1–T12 are implemented. Concrete contracts are `AcpServer`, `RegisteredAgent[Deps, Output]`,
`Session[Deps, Output]`, `TurnHandle[Output]`, and `TurnResult[Output]`. Server setup uses
`SessionContext`/`AcpSessionConfig`; client setup uses `AcpSession`/`AcpAttachmentConfig`.
The attachment config only accepts toolsets. Resolver instances cannot be reused across registrations. Shared persistence requires separately
partitioned store instances; registering the same instance twice is rejected.

Observation is an optional `ag-ui` extra using `pydantic-ai-slim[ag-ui]>=2.57.0,<3`.
The public APIs were exercised at 2.57.0 with `ag-ui-protocol` 0.1.22. A standalone
text run encoded six events in 728 bytes; replay storage grows linearly with current
native-run event volume and is released on completion. Observer queues are bounded
at 256 events. The implementation does not add a reducer
or disk spool. Native response order, reasoning message order, and tool-call IDs map
stream identities onto cached dumped messages without altering model history.

Offline exercises covered typed output/dependencies, multiple registrations, duplicate
IDs/store rejection, synchronized restoration, waiter shielding, FIFO, cancel/pause/
resume/clear, failure propagation, interrupted display history, shutdown, real local
ACP framing/WebSocket attachment, rejected second attachment, approval reconnect,
question support/unavailability, replacement client tool catalogs, multi-session
provider notifications, authenticated GET/SSE, matching observer events, and slow
subscriber disconnection. The existing headless client reported one lifecycle per
server-started and client-started turn. CLI help and example startup/import were checked.

Verification: `just check` (format/lint/typing/build) and `just docs-build`. No new tests
were committed, and no live-provider execution was used. Supplemental validation
scripts were ephemeral files outside the repository.

The implementation also updates stale README, example, configuration URL, and index
references so removed APIs are not shown as supported. No compatibility shims were added.
