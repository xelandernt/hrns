# ACP server API and read-only AG-UI observation

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
