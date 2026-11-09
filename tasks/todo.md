# ACP server and AG-UI implementation checklist

Design and confirmed scope: [../PLAN.md](../PLAN.md).
Status: T1–T12 implemented and verified offline; human review pending.
No new tests; use focused offline/manual checks and existing project gates.
Paths marked new are proposed modules; keep the final layout concise.

## T1 — Confirm public contracts and conversion feasibility

**Description:** Lock typed registration/session/turn contracts and exercise public AG-UI conversion independently of HTTP before broad runtime extraction.

**Acceptance criteria:**
- [x] Factory-only registration, session configuration/attachment split, lifecycle ownership, and agent-scoped store contract have concrete signatures.
- [x] An offline native stream converts through `AGUIEventStream`; snapshot/replay preserves IDs, user messages, and deferred-run boundaries.
- [x] Required AG-UI dependency versions/extra placement and replay-buffer memory behavior are recorded; substantial missing upstream support is surfaced for a decision.

**Verification:** Review installed/public API signatures and run an ephemeral offline conversion exercise. Update the design with concrete outcomes; no implementation tests or live model calls.
**Dependencies:** None.
**Files likely touched:** `PLAN.md`, `tasks/todo.md`.
**Estimated scope:** Small, 2 files.

## T2 — Create sessions through the agent registry

**Description:** Introduce `AcpServer` and typed registration handles; create isolated sessions without a client.

**Acceptance criteria:**
- [x] Multiple factory-only registrations create independently configured sessions; duplicate/unsafe IDs and late registration fail clearly.
- [x] Agent/session lookups and persistence are scoped per registration; repeated live lookup does not invoke the factory again.
- [x] Server and client configuration inputs are separate and preserve existing supported dependency/output typing.

**Verification:** Offline creation/lookup for two registrations, concurrent same-session restoration, and wrong-agent access rejection; run `just lint`.
**Dependencies:** T1.
**Files likely touched:** `src/hrns/server/acp/host.py` (new), `src/hrns/server/acp/session.py`, `src/hrns/server/acp/store.py`, `src/hrns/server/acp/__init__.py`.
**Estimated scope:** Medium, 4 files.

## Checkpoint A — Registration contracts

- [x] T1–T2 acceptance criteria satisfied; `just lint` and `just build` pass.
- [x] Public API, scope isolation, and feasibility findings reviewed before execution extraction.

## T3 — Execute one server-owned turn

**Description:** Move one-turn execution into the session runtime while preserving native agent behavior and persistence.

**Acceptance criteria:**
- [x] Python can submit a prompt and await its typed handle without an ACP client.
- [x] Server lifecycle owns execution; cancelling a result waiter does not cancel the run, and shutdown joins owned work.
- [x] Existing model/settings, usage, todos, skills, tool rendering, and committed-history behavior remain correct; runtime publishes native events/lifecycle snapshots.

**Verification:** Offline submit/complete, waiter cancellation, execution failure, and shutdown scenarios; compare history/event output with existing behavior and run `just lint`.
**Dependencies:** T2.
**Files likely touched:** `src/hrns/server/acp/runtime.py` (new), `src/hrns/server/acp/host.py`, `src/hrns/server/acp/session.py`, `src/hrns/server/acp/adapter.py`.
**Estimated scope:** Medium, 4 files.

## T4 — Schedule programmatic follow-ups

**Description:** Add the shared FIFO scheduler and Python-only queue controls.

**Acceptance criteria:**
- [x] Accepted turns execute in order with one active turn; queued prompts enter history only when started.
- [x] Cancel pauses pending work; Python inspection/resume/clear has documented behavior, and errors pause remaining work.
- [x] Clear/shutdown resolves pending handles; new submissions do not implicitly resume a paused queue.

**Verification:** Offline ordering, cancel/pause/resume/clear, failure, and teardown exercises with several pending handles.
**Dependencies:** T3.
**Files likely touched:** `src/hrns/server/acp/runtime.py`, `src/hrns/server/acp/session.py`, `src/hrns/server/acp/host.py`.
**Estimated scope:** Medium, 3 files.

## Checkpoint B — Programmatic execution

- [x] T3–T4 acceptance criteria satisfied; `just lint` and `just build` pass.
- [x] Server-started execution and queue controls work without network clients.

## T5 — Attach ACP to live sessions

**Description:** Mount per-agent ACP endpoints and make the adapter delegate creation, loading, prompting, and cancellation to the runtime.

**Acceptance criteria:**
- [x] `mount_acp(app, prefix=...)` exposes registered agents; `session/load` attaches to live execution without restarting/cancelling it.
- [x] Exactly one ACP attachment is admitted per session; replay/live transition is ordered and validates session scope/cwd.
- [x] Standard prompt requests share the FIFO scheduler; disconnect removes the attachment/waiter but preserves accepted work. Close/delete retain explicit lifecycle semantics.

**Verification:** Offline/headless ACP connect during a Python-started turn, replay, second attachment rejection, disconnect/reconnect, queued prompt acceptance, and agent isolation.
**Dependencies:** T4.
**Files likely touched:** `src/hrns/server/fastapi.py`, `src/hrns/server/acp/adapter.py`, `src/hrns/server/acp/host.py`, `src/hrns/server/acp/session.py`.
**Estimated scope:** Medium, 4 files.

## T6 — Synchronize server-started turns in the ACP client

**Description:** Add minimal advertised execution-state synchronization for a client joining work it did not initiate.

**Acceptance criteria:**
- [x] Load metadata and `_hrns/session/state` expose revisioned active-turn status/outcome without custom queue controls.
- [x] Existing client displays running/waiting/completed server-started work and avoids duplicate lifecycle events for its own prompt responses.
- [x] Disconnect/reconnect does not automatically resend accepted prompts or convert the TUI-local queue into a server queue.

**Verification:** Headless client lifecycle checks for mid-turn attach, idle attach, native completion, cancellation, and delayed/stale state updates.
**Dependencies:** T5.
**Files likely touched:** `src/hrns/server/acp/adapter.py`, `src/hrns/client/client.py`, `src/hrns/client/turn.py`, `src/hrns/client/events.py`, `src/hrns/client/tui/chat/screen.py`.
**Estimated scope:** Medium, up to 5 files.

## Checkpoint C — ACP steering

- [x] T5–T6 acceptance criteria satisfied; `just lint` and `just build` pass.
- [x] ACP attaches to a running server session and reports its outcome without a second model run.

## T7 — Retain pending interactions across disconnects

**Description:** Move permission/question waiting into session-owned interaction state with attachment-aware dispatch.

**Acceptance criteria:**
- [x] With no client, approval/question requests wait; an attached client receives them after session synchronization.
- [x] Disconnect retains the decision; reattachment reissues it and rejects late replies from prior generations.
- [x] Cancellation/shutdown clears waits; unsupported connected-client capabilities follow defined adapter outcomes.

**Verification:** Offline pending approval and question flows before attachment, reconnect while pending, stale response, decline, cancel, and shutdown.
**Dependencies:** T6.
**Files likely touched:** `src/hrns/server/acp/interactions.py` (new), `src/hrns/server/acp/session.py`, `src/hrns/server/acp/runtime.py`, `src/hrns/server/acp/adapter.py`.
**Estimated scope:** Medium, 4 files.

## T8 — Bind client tools to the current attachment

**Description:** Select attachment toolsets through public dynamic-toolset support and guard dispatch against stale connection resources.

**Acceptance criteria:**
- [x] Client tool availability changes at model-step boundaries without replacing server configuration.
- [x] Calls tied to departed attachments fail visibly rather than silently executing against another client; old terminal/document resources are not adopted.
- [x] MCP unavailable-registration, inner protocol, tool-execution, and uncertain-disconnect outcomes retain their distinctions; provider changes notify all affected live sessions of that registration.

**Verification:** Offline MCP-over-ACP/client tool scenarios with different reconnect catalogs, an outstanding call, stale tool definitions, and provider-option changes.
**Dependencies:** T7.
**Files likely touched:** `src/hrns/server/acp/mcp.py`, `src/hrns/server/acp/toolsets.py`, `src/hrns/server/acp/session.py`, `src/hrns/server/acp/runtime.py`, `src/hrns/server/acp/adapter.py`.
**Estimated scope:** Medium, 5 files.

## Checkpoint D — Disconnected execution

- [x] T7–T8 acceptance criteria satisfied; `just lint` and `just build` pass.
- [x] Agent runs without ACP, waits for a decision, reconnects, and continues with valid client resources.

## T9 — Publish AG-UI from existing execution

**Description:** Add `HrnsServer` with an observer component that uses upstream AG-UI conversion on the runtime's native events.

**Acceptance criteria:**
- [x] `AcpServer` remains importable without AG-UI; the declared observation dependency set provides the public converter APIs actually used.
- [x] Conversion occurs once per native run with stable session/run/message identities; several observers share output from the same execution.
- [x] Deferred runs, user prompts, errors, cancellation, baseline snapshots, and waiting state have consistent AG-UI semantics and bounded per-observer queues.

**Verification:** Offline simultaneous ACP/AG-UI event capture, multi-observer comparison, interrupted/native-resume lifecycle, and history identity checks from T1.
**Dependencies:** T8, T1 feasibility findings.
**Files likely touched:** `src/hrns/server/host.py` (new), `src/hrns/server/ag_ui.py` (new), `src/hrns/server/__init__.py`, `pyproject.toml`, `uv.lock`.
**Estimated scope:** Medium, 5 files.

## T10 — Mount read-only observation

**Description:** Add independent authenticated HTTP snapshot/SSE mounting with atomic history replay and subscription.

**Acceptance criteria:**
- [x] `mount_ag_ui(app, prefix=...)` exposes per-agent snapshot/events routes and starts no execution; mutation methods receive 405.
- [x] Idle attach, mid-run attach, reconnect, and several runs on one subscription preserve history/event order without gaps or duplicates.
- [x] Authentication/scope applies to both mounts; slow/disconnected observers are cleaned up without stalling execution or ACP.

**Verification:** Local HTTP/SSE smoke with two observers, slow subscriber, join/rejoin during text and tool arguments, unsupported methods, rejected admission, and unknown/cross-agent sessions.
**Dependencies:** T9.
**Files likely touched:** `src/hrns/server/ag_ui.py`, `src/hrns/server/host.py`, `src/hrns/server/fastapi.py`, `src/hrns/server/acp/host.py`.
**Estimated scope:** Medium, 4 files.

## Checkpoint E — Read-only observation

- [x] T9–T10 acceptance criteria satisfied; `just lint` and `just build` pass.
- [x] Native execution is shared, AG-UI uses upstream conversion, and observation accepts no mutation path.

## T11 — Move existing launchers to the server API

**Description:** Update stdio, CLI/runtime entry points, and the FastAPI example directly to the new hosting API.

**Acceptance criteria:**
- [x] `hrns acp`, built-in runtime, and `hrns serve` share the runtime while keeping their intended transports/configuration.
- [x] FastAPI example registers factories, explicitly mounts ACP/AG-UI, and owns the server lifecycle correctly.
- [x] Removed hosting entry points have no aliases or parallel implementations; all repository callers use the final API.

**Verification:** Existing CLI help/config checks, offline stdio handshake, local multi-agent WebSocket serving, and example import/startup; run `just lint`.
**Dependencies:** T10.
**Files likely touched:** `src/hrns/server/acp/server.py`, `src/hrns/server/fastapi.py`, `src/hrns/server/runtime.py`, `src/hrns/cli.py`, `examples/fastapi_app.py`.
**Estimated scope:** Medium, 5 files.

## T12 — Document and verify the complete flow

**Description:** Publish the final API/lifecycle documentation and complete the combined smoke scenario.

**Acceptance criteria:**
- [x] `ARCHITECTURE.md` records actual ownership, mounting, queue, attachment, AG-UI replay, authentication, and single-process limitations.
- [x] Public docs explain programmatic startup, ACP steering, Python-only queue control, read-only endpoints, and explicit lifecycle management using current examples.
- [x] Combined smoke meets PLAN.md's scenario; final gates pass and results identify offline versus live-provider evidence.

**Verification:** `just check`, `just docs-build`, and focused combined offline/manual smoke. No new tests and no unnecessary repeated gates after success.
**Dependencies:** T11.
**Files likely touched:** `ARCHITECTURE.md`, `docs/mount-acp.md`, `docs/acp.md`, `docs/ag_ui.md` (new), `zensical.toml`.
**Estimated scope:** Medium, up to 5 files.

## Checkpoint F — Complete

- [x] T11–T12 acceptance criteria satisfied; final lint/build/documentation gates pass.
- [x] All confirmed requirements met; no backwards-compatibility shims, new tests, frontend implementation, or ACP queue-management methods added.
- [ ] Human review of final API and implementation completed. Implementation review and offline validation are complete; limitations are recorded in `ARCHITECTURE.md` and `docs/ag_ui.md`.

## Verification record

Implemented the tasks in order, with focused ephemeral offline exercises at the runtime,
ACP, attachment-resource, and observation boundaries. Details and resolved T1 contracts
are in [PLAN.md](../PLAN.md#implementation-record). Existing format/lint/type/build and
documentation gates passed. No new tests, live-provider calls, compatibility aliases,
frontend, or ACP queue-management methods were added. Additional caller/documentation
files were updated where they otherwise referenced removed APIs.
