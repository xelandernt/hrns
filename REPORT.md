# Lody: Shared Developer AI Chats on Loro and ACP

**Subject:** [`LodyAI/Lody`](https://github.com/LodyAI/Lody) at `0ccc65a9035955456c0001eab1ab66a61638b5ef` (2026-11-10)  
**Written:** 2026-11-12  
**Method:** code reading plus Loro's public documentation. Lody's hosted backend (`@lody/convex` and the private Web app) and the Loro Streams server are not in the public repository, so statements about those are marked as declared architecture or inference. No runtime multi-account test was performed.

---

## 1. Executive summary

Lody is not a hosted chat server with an agent bolted on. It is a **local-first collaboration layer around ACP agents**:

- A conversation is a **Loro CRDT document** replicated to every participating client.
- The **owning machine's CLI daemon** is the only ACP client for that conversation, and the only writer of agent-produced output.
- **ACP remains 1:1** between the daemon and one agent process. Multi-user collaboration is achieved by making the transcript, queue, permission state, and presence **shared data**, not by multiplexing ACP.
- The cloud has two separate responsibilities: **Convex** for identity, membership, machine registry, and access grants; **Loro Streams** for documents, presence, and Machine RPC request/response streams.
- Execution is pinned to `SessionMeta.machineId`. If that machine is offline, the conversation can still be read and edited through the CRDT, but no agent runs.
- Scale comes from stateless clients, sharded durable streams, and one daemon per machine. There is no monolithic session server whose memory is the only copy of the chat: every client persists a replica, and Loro Streams durably stores the session stream's snapshots and updates.

The core design sentence is:

> **Loro makes the conversation shared; ACP makes the agent executable; the machine-owned daemon makes execution single-owner.**

---

## 2. What Lody is

Lody describes itself as "a shared workspace for the coding agents your team already uses" (`README.md:33`). The product has four surfaces:

- **Desktop** — Electron app, with an embedded/local CLI.
- **Web** — cloud-composed React client.
- **Mobile** — native iOS (Swift/UIKit + SwiftUI) and Android (Kotlin/Compose), using Lody Cloud.
- **CLI** — `npx lody daemon start`, plus session-management commands.

Agents are brought in over **ACP**: Claude Code, Codex, Kimi, OpenCode, Grok, Devin, DeepSeek Harness, Pi, and custom ACP agents. The agent keeps its own subscriptions, logins, models, and permission configuration on the machine where it runs.

There are three different things called "sharing" in the product, and they should not be conflated:

| Mode | What is shared | Live? | Who can write |
|---|---|---|---|
| **Live team collaboration** | The same Loro session document | Yes | Members with machine/project edit access |
| **Live read-only grant** | The same Loro session document | Yes | Nobody except the owner/editors; recipient reads |
| **Static published share** | An immutable snapshot package | No | Nobody; it is a published copy |

The live modes are governed by `specs/session-access.md`; the static copy is governed by `specs/session-sharing.md`. The rest of this report focuses on the live modes, then covers static sharing separately.

---

## 3. Stack

Versions below are from the checked-out lockfile and package manifests; they will drift.

| Layer | Technology |
|---|---|
| Monorepo | pnpm 10.20, Node `>=22.23.4 <23` / `>=24.22.1 <25` / `>=26.12.1 <27`, TypeScript 5.9, Vite 7, esbuild, Vitest 5 |
| CLI | Node, Commander 12, Effect 4.0.2, `better-sqlite3`, `ws`, Winston, PostHog |
| Desktop | Electron 44.9.3, React 19.2, Jotai, TanStack Router, StyleX, Tailwind 4 |
| Web / shared UI | React 19.2, `@assistant-ui/react`, Jotai, StyleX + Tailwind 4, Storybook |
| Mobile | Native Swift/UIKit + SwiftUI; Kotlin/Jetpack Compose; shared Loro/Flock protocol runtime |
| Document CRDT | `loro-crdt` 1.17.0 |
| Typed CRDT schema | `loro-mirror` 2.3.2 |
| Collection / sync layer | `loro-repo` 0.25.0 |
| Key-value CRDT | `@loro-dev/flock-wasm` 0.4.4 |
| Streams transport | `@loro-dev/streams-crdt` 0.19.2, `@loro-dev/streams-client` 0.12.1 |
| ACP | `@agentclientprotocol/sdk` 1.3.0 in the CLI; adapters may pin newer ACP SDKs (up to 2.5.0) |
| Control plane | Convex 1.33.1 + Better Auth 1.6.33 |
| Content plane | Loro Streams (external hosted service; public site currently labels it alpha) |
| E2EE | `@lody/e2ee-core` (signed membership ledger, epoch keys), optional `iroh` transport |
| Testing | Vitest, Playwright, Storybook, native mobile test suites |

Lody's own `AGENTS.md` is explicit that the public repo excludes hosted backends and private Web sources (`AGENTS.md:80-90`). The public build of the desktop is **local-only**; cloud collaboration comes from a separate cloud composition.

---

## 4. Architecture at a glance

### 4.1 The four planes

```text
┌──────────────────────────────────────────────────────────────┐
│ CLIENT (desktop / web / mobile)                              │
│                                                              │
│  UI  ◄──►  LoroDoc replica  ◄──►  LoroRepo                   │
│                  ▲                     │                     │
│                  │ local IPC           │ cloud HTTPS/SSE     │
└──────────────────┼─────────────────────┼─────────────────────┘
                   │                     │
        ┌──────────▼─────────┐    ┌──────▼─────────────────────┐
        │ CLI daemon          │    │ Loro Streams               │
        │ (owning machine)    │◄──►│ docs, Flock, presence, RPC │
        │                     │    └────────────────────────────┘
        │  LoroRepo + SQLite  │               ▲
        │  ACP client         │               │ authorization
        │  dispatch watcher   │    ┌──────────┴─────────────┐
        │  MessageHandler     │    │ Convex control plane   │
        └──────────┬──────────┘    │ auth, membership,      │
                   │               │ machine + session ACLs │
                   │ ACP/JSON-RPC  └────────────────────────┘
        ┌──────────▼──────────┐
        │ Agent process        │
        │ Claude / Codex / ... │
        └──────────────────────┘
```

The Lody communication architecture document describes the same split (`specs/communication-architecture.zh.md:19-47`):

- **Convex** — organization, membership, and permission information; platform APIs.
- **Loro Streams** — documents, catalogs, conversations, and the separate Machine RPC request/response streams.
- The local desktop uses Electron IPC to the CLI; the CLI owns local execution resources and SQLite.

### 4.2 What runs where

| Concern | Runs on | Owner / mechanism |
|---|---|---|
| UI rendering | Desktop, web, mobile | React/native UI |
| Client document replica | Each client | `LoroDoc` inside `LoroRepo`; IndexedDB (web/Electron renderer) or native storage |
| Local control path | Electron main ↔ CLI | Unix socket, push-only, peer-scoped data plane |
| ACP client | Owning machine's CLI daemon | `AgentClient` implementing `acp.Client` |
| Agent process | Owning machine, child of the CLI | Spawned over stdio with NDJSON JSON-RPC |
| Authoritative agent output writes | Owning machine's CLI | `MessageHandler` + `HistoryWriter` |
| User/UI durable writes | Each client | Renderer direct-authoring into its own replica |
| Conversation sync | Loro Streams | Snapshot + version-vector deltas |
| Presence / liveness | Loro Streams ephemeral channel | `EphemeralStore`, 30s heartbeat, 90s TTL |
| Cross-machine request/response | Loro Streams JSON streams | Machine RPC, 24h retention |
| Identity, membership, grants | Convex | Private backend; public DTOs in `packages/cloud-api` |
| Static share storage | Cloud object storage + control plane | Immutable package, bearer link |
| E2EE key custody | Client / desktop native host | Signed ledger + epoch keys; ciphertext through Streams |

For the live-chat path, only **Convex** and **Loro Streams** are server-side. ACP, agent execution, and all transcript writes are client-side/daemon-side. The server does not read ACP, does not run the agent, and does not merge the chat.

### 4.3 The critical architectural choice: no session server

Lody does **not** have a central process that:

- holds the live transcript,
- reads ACP updates and fans them out to clients,
- serializes user messages,
- or runs the agent on behalf of everyone.

Instead:

1. Every client has a replica of the session document and merges CRDT operations locally.
2. The owning daemon writes agent output into its replica; Streams propagates the operations.
3. Other clients render the merged result.
4. Execution requests reach the daemon through the document itself (plus an optional RPC fast path).

This is why multiple clients can collaborate without a multi-client ACP extension. It is also why there is no single server container whose memory contains "the chat."

That is **not** the same as saying the session is not persisted. "No session server" means no process owns the only authoritative in-memory copy or performs CRDT merges for clients. Persistence is separate and is deliberately redundant:

| Persistence layer | What is stored | What happens when every client disconnects |
|---|---|---|
| Each UI client | A local `LoroDoc` replica in IndexedDB (web/Electron renderer) or native storage (mobile) | The replica remains on the device; the next open resumes from it |
| Owning CLI daemon | `~/.lody/loro-repo/<workspaceId>/repo.sqlite3`: document snapshots, update journals, Flock/meta snapshots, remote cursors | The daemon process can restart and reopen its replica; it does not need a UI client to be connected |
| Loro Streams | Per-session durable stream `<workspaceId>:s:<sessionId>`: snapshot plus appended updates (Zstandard snapshots, 5s upload debounce for plaintext) | The stream remains in the content service; a new or reconnecting client bootstraps from the snapshot and catches up by version vector/offset |
| Meta Flock | Session metadata, status, ownership, and dispatch pointers in the workspace meta stream | Session lists and activation pointers survive independently of any UI client |
| Static share | A frozen package in object storage | The published copy remains readable independently of the live session |

The exception is **presence**, not the session: machine heartbeats and "who is viewing" are intentionally ephemeral (30s heartbeat, 90s TTL, no history). The conversation document is durable.

If all UI clients and the owning daemon are offline, the **conversation still exists in Loro Streams**; what is unavailable is execution. When a client comes back, it opens the stream, imports the snapshot plus missing updates, and merges. When the daemon comes back, it reopens its SQLite replica and catches up the same way. If the daemon is online but every UI client is offline, the daemon can still execute queued work from its replica/stream connection, because it is the execution owner.

Code references: the local SQLite path is `apps/cli/src/lib/loro/sqlite-repo-store.ts:16-20`; the Streams snapshot upload and data-before-cursor persistence are `apps/cli/src/lib/loro/streams-transport.ts:95-112`; the renderer uses `IndexedDBStorageAdaptor` via `packages/components/src/providers/workspace-repo-storage.ts`.

Two qualifications matter:

- **Cloud composition only.** The OSS desktop is local-only: its persistence is the local CLI SQLite plus renderer IndexedDB. There is no cloud copy in that build.
- **Retention is a service policy.** Lody's client does not assume a session stream is deleted when a client disconnects; it uploads snapshots and stores remote cursors. Actual stream retention/expiry is owned by the Streams bucket/service, not by the absence of connected clients. Deleting a session is a separate soft-delete/purge lifecycle.

---

## 5. The Loro model

### 5.1 LoroDoc

`LoroDoc` is a replicated JSON-like document. A document contains:

- a tree of containers: `LoroMap`, `LoroList`, `LoroMovableList`, `LoroText`, `LoroTree`, `LoroCounter`;
- an operation log (**OpLog**) and a materialized **state**;
- per-operation IDs of `(peerId, counter)`;
- changes with Lamport timestamps;
- version vectors and frontiers;
- snapshots, incremental updates, and pending imports.

Sync is just export/import:

```ts
const delta = docA.export({ mode: "update", from: docB.oplogVersion() });
docB.import(delta);
```

`import` is idempotent and order-independent. If an update arrives before its causal dependencies, Loro retains it as a pending import and applies it when the missing operations arrive. A new replica bootstraps from a snapshot; subsequent changes are deltas.

Merge semantics are per container:

| Container | Concurrent-edit behavior |
|---|---|
| `LoroText` | Character-level sequence CRDT (Fugue); concurrent typing interleaves |
| `LoroList` | Ordered sequence; concurrent inserts get a deterministic order |
| `LoroMovableList` | `move()` is a first-class operation, not delete+insert |
| `LoroMap` | Last-writer-wins per key, deterministic tie-break |
| `LoroTree` | Movable tree |
| `LoroCounter` | Increment/decrement |

CRDTs give **convergence**, not **semantic conflict resolution**. If two users both "answer the permission prompt," both writes are valid CRDT operations and the application must choose a winner. Lody does this explicitly: first persisted permission outcome wins (`packages/shared/src/session-data/planner.ts:97-110`).

### 5.2 Flock

Flock is a separate CRDT: a **last-writer-wins key-value store** with composite keys.

- Each entry has an HLC-like clock: physical time, logical counter, peer ID.
- Conflict rule: higher physical time, then higher logical counter, then lexicographically greater peer ID.
- Deletes are tombstones.
- Version vectors support incremental export.
- Keys are arrays such as `["session", sessionId]`; prefix scans are efficient.

Flock is a poor fit for a transcript (no character-level text, no list semantics) but an excellent fit for **lots of small rows**: session metadata, machine catalogs, workspace catalogs, file indexes.

Lody uses both:

| Data | CRDT | Why |
|---|---|---|
| Conversation history | LoroDoc | Ordered turns, streaming text, nested tool content |
| Message queue | LoroMovableList | Reorder/edit convergence |
| Session metadata / dispatch pointers | Flock | Many small fields, cheap range scans |
| Machine catalog | Flock | Provider configs, local projects, commands |
| Workspace catalog | Flock | Projects, MCP servers, Agent Roles |
| Code Collab file index | Flock | Path-keyed rows, large independent updates |

### 5.3 LoroRepo

`LoroRepo` is the collection layer above one `LoroDoc` or `Flock` instance. It owns:

- metadata catalog and per-document metadata;
- lazy document opening (`openPersistedDoc`, `openDetachedDoc`);
- room joins (`joinDocRoom`, `joinFlockDocRoom`, `joinMetaRoom`);
- storage adapters (SQLite for the CLI, IndexedDB for the browser);
- transport routing and per-`(room, transport)` status;
- remote cursors and durability barriers;
- soft delete, restore, and purge.

A repo can have several transports at once. Lody uses this for **local + cloud dual-homing**: the same room can be joined over local IPC and over Streams. CRDT imports commute, so duplicate delivery is harmless; status is never merged across transports. That is exactly what makes offline-first and cloud collaboration coexist.

### 5.4 Loro Streams

Loro Streams is the hosted durable-stream service. From the client side it provides:

- stream creation and deletion;
- snapshot bootstrap;
- append-only updates with offset/version-vector catch-up;
- live SSE (with long-poll fallback);
- retention and sharding;
- ephemeral channels for presence.

It is **not** a CRDT merge server. It stores and serves the bytes; the clients run Loro's merge logic. Those bytes are durable: a snapshot plus appended updates are enough for a fresh client to reconstruct the document with no other client online, subject to the stream's retention policy. Lody's `StreamsTransportAdapter` is configured with:

- per-workspace bucket and stream IDs;
- a gateway base URL and shard suffix;
- a JWT auth callback;
- a snapshot codec (Zstandard);
- a snapshot upload policy (5s debounce for plaintext);
- optional payload protection for E2EE.

Relevant code: `apps/cli/src/lib/loro/streams-transport.ts:80-115`, `packages/components/src/providers/workspace-streams-transport.ts:70-125`.

---

## 6. The session data model

### 6.1 Two documents per conversation

Lody splits a conversation into:

1. **A session document** — the transcript and session-local control state.
2. **A metadata row in the workspace "meta Flock"** — the index entry, status, ownership, and dispatch pointers.

The session document ID is `session-<uuid>`; its Streams ID is `<workspaceId>:s:<sessionId>` (`packages/shared/src/index.ts:404-448`). The metadata lives in the repo's internal **meta Flock**, the same structure that lists documents (`packages/shared/src/local-loro-data-plane-server.ts:109-112`).

This split is load-bearing:

- Startup can list thousands of conversations by reading only metadata, without opening thousands of session documents.
- The dispatch watcher can see `latestUserMsgId` and status without joining the history room.
- A read-only client can list conversations without hydrating bodies.
- A session document can be opened lazily when the user actually opens the conversation.

Lody's own rule says opening a doc is not cheap: it joins a room and performs an initial sync (`apps/cli/src/lib/loro/AGENTS.md`). Bulk scans must use metadata indexes first.

### 6.2 Session document schema

The root schema is defined with `loro-mirror` (`packages/shared/src/schema.ts:1461-1499`):

| Root key | Type | Purpose |
|---|---|---|
| `session` | `LoroMap` | Session ID (metadata lives elsewhere) |
| `history` | `LoroList<LoroMap>` | One map per turn, keyed by `id` |
| `mq` | `LoroMovableList` | Shared message queue |
| `humanInputReceipts` | `LoroMap` | Trusted human-send outbox |
| `cancelRequest` | `LoroMap` | Latest Stop request |
| `forkOperation` | `LoroMap` | Durable state for async fork |
| `notification` | `LoroMap` | Terminal notification projection |
| `preview` | `LoroMap` | Preview candidate/connection state |
| `externalHistoryCursor` | `LoroMap` | External ACP history import cursor |
| `acpRuntimeConfig` | `LoroMap` | Shared ACP runtime baseline |

A turn (`packages/shared/src/schema.ts:681-720`) contains:

- `id`, `userTurnId`, `acpTurnId`
- `role` (`user` / `assistant` / `system`)
- `timestamp`, `startedAt`, `endedAt`, `permissionWaitMs`
- `status` (pending / seen / processing / handled / failed / canceled, etc.)
- `items`: a `LoroList` of message items (text, thought, tool call, plan, image, file, subagent task, etc.)
- `plan`, `fileDiff`, `modelInfo`, `author`, `inputConfig`
- scheduling/fork provenance fields

Crucially, only **streaming** fields are `LoroText`: assistant `text`, `thought`, tool `output`/`markdown`, nested content text, and similar. Metadata such as tool IDs, titles, paths, commands, and statuses stays primitive. The schema comment explains why: a blanket `defaultLoroText: true` would create thousands of unnecessary containers across a long session (`packages/shared/src/schema.ts:212-252`). A `LoroText` per token field is useful; a `LoroText` per command argument is not.

### 6.3 Session metadata

`SessionMeta` (`packages/shared/src/schema.ts:1083-1250`) is a large row with, among many fields:

- `id`
- `machineId` — the immutable execution owner
- `createdAt`, `lastMessageAt`, `lastReadAt`
- `title`, `titleSource`, `groupTitle`
- `userId`
- `status`
- `isArchived`, `isPinned`
- `cliType`, `agentType`, `agentConfigId`
- `acpSessionId`
- `project`, `repoFullName`, `baseBranch`, `branchName`, `isWorktree`
- `latestUserMsgId`
- `lastHandledUserMsgId`
- `processingUserMsgId`
- `steerTurnStatuses`, `steerOperationLedger`
- `queuePromotionLedger`
- `lastCanceledTurn`
- `messageQueueUpdatedAt`, `messageQueueCheckedAt`
- `awaitingUserSince`
- `diffStats`, `workspaceDirty`, `workspaceUnpushed`
- `pullRequests`, `pullRequestState`

The dispatch pointers are an **activation index**, not the transcript. The design notes repeatedly warn that `latestUserMsgId !== lastHandledUserMsgId` is not itself a pending-work predicate; `hasPendingUserTurnActivation` is the single predicate (`packages/shared/src/session/AGENTS.md`).

`SessionMeta.status` is durable/historical state, not live "working now" state. Live activity comes from presence. A stale durable status left by a crashed daemon is reset by the dispatch watcher, and the UI deliberately does not treat metadata status as proof of a running turn (`apps/cli/src/lib/message-handler.ts` status repair; `.agents/docs/sessions-live-status.md`).

### 6.4 Mirror and writer

Lody does not let UI code write raw Loro containers. There are two seams:

- **`loro-mirror` + Immer** for control fields (`packages/shared/src/session-mirror.ts:29-70`).
- **`HistoryWriter`** for history (`packages/shared/src/history-writer.ts:428`).

`HistoryWriter`:

- validates new turns and changed fields against shared schemas;
- diffs against the stored representation and applies only changed fields;
- preserves unknown stored fields and unchanged opaque items;
- keeps legacy string rows readable;
- writes history through one writer instance per document.

The session mirror writes control deltas; history deltas are routed to the writer. This prevents a second, unvalidated history write path.

`SessionData` is the business seam above the writer (`packages/shared/src/session-data/loro.ts`). Commands such as `appendTurn`, `setTurnField`, `respondPermission`, `replaceEditableTail`, and `importHistory` are expressed as domain operations. The CLI and renderer share the same planners, so a rule cannot drift between surfaces.

### 6.5 Why `loro-mirror`

`loro-mirror` maps a typed schema onto Loro containers. It provides:

- typed get/set;
- change subscriptions;
- schema-driven container creation;
- forward compatibility through `ignoreUnknownProperties: true`.

The forward-compatibility choice is deliberate: a synced document outlives the build that wrote it. A newer client may add a root key that an older client does not know; the older client must still be able to read and write the document. Removing or repurposing a root field is a breaking change (`packages/shared/src/schema.ts:1442-1461`).

### 6.6 History backend seam

Lody is already testing a second history backend, **Roost**, behind a developer feature gate. The session's `historyBackend` is chosen at creation and is immutable for the session lifetime:

- `loro` — history is part of the Loro session document (the default in this checkout).
- `roost` — history is stored by the Roost native/SDK backend, while the Loro document still carries the control plane.

This matters architecturally: the control document and the history store are separable. The shared session model does not strictly require history to live in the same Loro document, as long as the control plane, read model, and authorization semantics stay the same. Roost is not the default and is outside the main path described here (`packages/shared/src/schema.ts:1027-1040`, `.agents/docs/roost-validation.md`).

---

## 7. End-to-end: one shared turn

Let us trace a concrete case: Alice is on desktop, Bob is on mobile, and the agent runs on Alice's workstation.

### 7.1 Sending

1. **Bob or Alice composes a message.** The UI resolves a submit route from live presence and the durable unfinished-turn state (`packages/components/src/components/sessions/session-message-submit-route.ts:17-45`):
   - **direct dispatch** when the session is idle,
   - **guide/steer** when the agent is busy and the agent supports acknowledged steering,
   - **queue** when busy, forced, or an unfinished assistant turn exists.

2. **The client writes locally.** It writes:
   - a `humanInputReceipts` entry (trusted human provenance),
   - the user turn into `history`,
   - `latestUserMsgId` into session metadata.
   
   These are local commits on the client's replica. Persistence and upload run independently (`packages/components/src/lib/session-send-delivery.ts:88-160`).

3. **Optional fast path.** The client may send `session/dispatch-turn` over Machine RPC to the owning machine with the full turn payload. The RPC response is a **delivery ACK only**; the session document remains the durable source of truth (`apps/cli/src/session/session-dispatch-watcher.ts:281-288`).

4. **The owning daemon's dispatch watcher wakes** from one of three sources (`apps/cli/src/session/session-dispatch-watcher.ts:271-289`):
   - metadata watch (`repo.watch('doc-metadata')`),
   - session document subscription (`subscribeAll`),
   - RPC turn push (`offerRpcTurn`).

5. **The watcher decides** (`apps/cli/src/session/session-dispatch-logic.ts:100-190`):
   - Is this session owned by this machine?
   - Is it archived?
   - Is there an active turn or a pending ACP create?
   - Is there a rewrite barrier or release block?
   - Is the status stale?
   - Is there a dispatchable user turn, or a queue item to promote?

6. **The execution service starts or resumes the ACP session** (`apps/cli/src/session/session-execution-service.ts`). It may:
   - create a new agent process and `session/new`,
   - `session/load` or `session/resume` an existing ACP session,
   - `unstable_forkSession` for a fork,
   - apply mode/model/config selections.

7. **The daemon sends `session/prompt`** with ACP content blocks, then waits for updates.

### 7.2 Streaming back

```text
Agent process
   │  ACP session/update (NDJSON JSON-RPC)
   ▼
AgentClient.sessionUpdate
   │
   ▼
SessionManager emits onACPUpdateMessage
   │
   ▼
MessageHandler.enqueueACPUpdate
   │  buffer (10 ms first batch, 100 ms subsequent)
   ▼
appendACPNotificationsToAssistantEntry
   │  merge text into LoroText / tool rows by toolCallId
   ▼
HistoryWriter
   │  local Loro commit
   ▼
LoroRepo persistence (SQLite) + Streams upload
   │
   ▼
Every client imports the update and re-renders
```

The batching constants are in `apps/cli/src/lib/message-handler.ts:876-877`. Batching is not just an optimization: one CRDT operation per token would explode the OpLog and snapshot size.

The update mapping is in `packages/shared/src/acp/history-apply.ts`:

- `agent_message_chunk` / `agent_thought_chunk` -> text/thought items, merged with overlap/dedup logic (`packages/shared/src/history-stream-text.ts:1-90`);
- `tool_call` / `tool_call_update` -> a tool-call map merged by `toolCallId`;
- `plan` -> turn plan;
- `available_commands_update` -> runtime config/available commands;
- `current_mode_update` / `config_option_update` -> runtime config;
- `session_info_update` -> titles, goals, warnings, specialized provider metadata;
- `usage_update` -> token/context usage, outside the transcript;
- `subagent_event` -> nested subagent task item.

Tool payloads are deliberately compacted for history: full file contents, edit old/new text, and unbounded terminal snapshots are stripped. Edit evidence is captured separately for the diff store (`packages/shared/src/acp/history-apply.ts:113-140`).

### 7.3 The other client sees it

Bob's client is subscribed to the same session document. It imports the update, merges the `LoroText` and list operations, and renders the new content. There is no "server pushes ACP event to Bob"; Bob receives **CRDT operations** through Loro Streams.

This is the key inversion compared with a conventional chat app:

| Conventional chat | Lody |
|---|---|
| Server owns transcript, broadcasts messages | Clients own replicas, exchange CRDT ops |
| One writer (server) | Multiple writers (clients + daemon) |
| Client reconnect replays messages | Client reconnects and reconciles version vectors |
| Agent events go through the chat server | Agent events become CRDT writes on the owner |

---

## 8. ACP in detail

### 8.1 Transport and process

Lody is the ACP **client**; the coding agent is the ACP **server**.

- Transport: JSON-RPC over the child process's stdio, framed as NDJSON.
- The CLI spawns the agent with `stdio: ['pipe', 'pipe', 'pipe']`.
- `AgentClient` implements `acp.Client`; `ClientSideConnection` from `@agentclientprotocol/sdk` drives the connection.
- The agent runs in its own process group so Lody can terminate the whole tree.

Relevant code:

- `apps/cli/src/session/session.ts:582-857` — spawn, `ndJsonStream`, `createAcpClient`.
- `apps/cli/src/agent/acp-runner.ts:154-199` — construct `AgentClient`, call `startSession`.
- `apps/cli/src/agent/acp-runner.ts:839-848` — stdout/stdin to NDJSON stream.
- `apps/cli/src/agent/agent-client.ts:632` — `AgentClient`.

There is a **process-wide startup gate** (default 2 concurrent ACP session starts) because unbounded Codex starts contend on `~/.codex` and freeze in-flight sessions (`apps/cli/src/agent/README.md`).

### 8.2 Session lifecycle

`AgentClient.startSession` negotiates:

1. **`initialize`** — protocol version and client capabilities:
   - `fs.readTextFile` / `fs.writeTextFile`
   - `terminal`
   - `plan`
   - form elicitation (used for AskUserQuestion)
   - session config options
   - MCP capabilities
   - Lody extension capabilities under `_meta.lody` (subagent events, prompt suggestions, etc.)

2. **Session establishment** — one of:
   - `newSession({ cwd, mcpServers, _meta.lody.sessionConfig })`
   - `loadSession({ sessionId, cwd, mcpServers, ... })` (preferred when the agent advertises `loadSession`)
   - `resumeSession(...)` (when the agent advertises resume and load is not preferred)
   - `unstable_forkSession(...)` for forks, optionally at a specific turn.

3. **State application** — modes, models, config options, available commands, current model.

4. **`prompt`** — content blocks plus optional `_meta` for goal actions or steering.

5. **`cancel`** — sends ACP cancel for the active session.

6. **`closeSession`** — optional, when the agent advertises it.

The ACP session ID is stored in `SessionMeta.acpSessionId`. It is not in the session document, because it is execution state, not transcript content.

### 8.3 Client-side ACP methods

Because Lody is the client, it implements the agent's callbacks:

| ACP method | Lody behavior |
|---|---|
| `session/update` | Translate to history/control writes |
| `session/request_permission` | Persist a permission request, wait for a CRDT outcome |
| `fs/read_text_file` | Read the file, map missing file to `resourceNotFound` |
| `fs/write_text_file` | Write the file; capture edit evidence for diffs |
| `terminal/*` | Manage PTYs and bounded output |
| `elicitation/create` | Bridge AskUserQuestion forms onto the permission UI |
| `session/update` subagent events | Store nested subagent tasks |

ACP extensions add capabilities beyond the base protocol:

- **acknowledged steer** — apply an input into the running turn, with explicit applied/not-applied/unknown evidence;
- **goals** — start/resume/pause/clear a goal;
- **subagent events** — normalized child-agent activity;
- **prompt suggestions** — inline suggestions;
- **session titles** — agent-owned title;
- **history import** — read an external ACP session without resuming it;
- **rate limits / usage** — quota and context telemetry.

These are negotiated via `agentCapabilities._meta.lody`, not by guessing the provider name.

### 8.4 Permissions: ACP request, CRDT resolution

This is one of the most interesting cross-plane flows (`apps/cli/src/lib/message-handler.ts:10518-10530`):

1. The agent calls `session/request_permission`.
2. The daemon writes the request into the matching tool-call item in history:
   - `permissionRequest: { requestId, options, ... }`.
3. Any client with edit access can write an outcome into that item:
   - `permissionRequest.outcome = { outcome: "selected", optionId }` or `cancelled`.
4. The daemon subscribes to session changes and resolves the ACP promise when it sees an outcome.
5. If no outcome arrives, it times out or is cancelled.
6. The outcome is **first-write-wins**. `applyRespondPermission` refuses to overwrite an existing outcome (`packages/shared/src/session-data/planner.ts:97-110`).

That is how a phone can approve a permission request raised on a workstation without either one being the ACP peer.

The spec also records the security limitation: `permissionRequest` and other mutable CRDT fields are forgeable by a member who can write the document. The permission UI is not a cryptographic capability.

### 8.5 Stop, steer, and queue

- **Stop** is a control-document request (`cancelRequest`) plus a metadata marker (`lastCanceledTurn`). The daemon's cancel chain sees it and calls ACP `cancel`. This is separate from the dispatch chain so a cancel can proceed while a turn is running (`apps/cli/src/session/session-dispatch-watcher.ts:328-338`).
- **Steer/guide** writes a `pending_apply` user turn and sends `session/steer` over Machine RPC. If the agent supports acknowledged steering, the text is injected into the running prompt. If not, it falls back to the queue.
- **Queue** writes a row to `mq` with a stable `$cid`, an optional `operationId`, and an editing lease (`isEditing` + `editingStartedAt`, 5 minutes). The write also bumps `messageQueueUpdatedAt` metadata, which wakes an idle daemon to re-evaluate (`packages/components/src/providers/workspace-writer-impl.ts:58-85`). The daemon promotes the head row only when the session is idle and the row is not being edited (`apps/cli/src/lib/loro/doc.ts:3764-3788`). Promotion appends a history turn and publishes `latestUserMsgId` as one logical operation (two writes, history first) (`apps/cli/src/session/session-dispatch-watcher.ts:314-315`).

Queue promotion writes a receipt into `SessionMeta.queuePromotionLedger`. History and the queue live in different CRDT roots, so that receipt is the recovery point if a client crashes after appending the history turn but before removing the queue row (`apps/cli/src/lib/loro/doc.ts:3084-3104`).

The single-turn contract is enforced by routing: the UI will not direct-dispatch while an unfinished assistant turn exists, and the daemon will not start a second turn while one is active. This is a cooperative protocol, not a distributed lock.

### 8.6 Resume, replay, and fork

- If the agent supports `session/load` or `session/resume`, Lody reuses the native ACP session.
- If not, it creates a new ACP session and reconstructs context from stored history. The replay builder excludes the current user turn and imports prior turns as prompt context.
- `fork` uses the agent's fork capability when available. Lody stores a `forkOperation` in the session document for crash recovery, creates a target document, and publishes target metadata only after the commit.
- `edit-and-resend` replaces the last editable user turn, uses `forkAtTurn` when the agent supports it, cancels the exact turn, and commits history/meta behind a barrier.

### 8.7 What ACP does not do

ACP has:

- no multi-client fan-out,
- no durable transcript,
- no presence,
- no message queue,
- no per-user access control,
- no session discovery across a workspace,
- no execution ownership model.

ACP is a **1:1 execution protocol**. Everything collaborative is layered above it.

### 8.8 Agents as collaborators

Lody also lets agents operate on other conversations. The CLI exposes a Lody MCP server (HTTP for agents that advertise `mcpCapabilities.http`, otherwise a per-session stdio server) with tools to:

- create or reuse sessions,
- send a message to another session,
- read session status and history,
- cancel work,
- list machines, agent configs, and quota.

This is built on the same shared-session model, not a separate message bus. The orchestration spec adds:

- a maximum causal delegation depth of 16 hops;
- a prohibition on replying to the current task sender (no reverse chat loop);
- steering into a busy target when the agent supports it, otherwise queueing;
- delivery receipts (`steered`, `queued`, `dispatched`) that are response-only and not persisted;
- failure reasons (`usage_limit`, `context_overflow`, `model_error`, `permission_denied`, etc.);
- a durable Inbox for operation completion with idempotent consumption.

Agent-authored input is still a user-role turn to the target, but it carries an `author` snapshot identifying the source session, agent, model, and optional Role. The human remains the authorization principal. This is how one "coordinator" conversation can drive a dozen implementation/test conversations without pretending the human typed all of them.

### 8.9 Code changes, attachments, and previews

The transcript is only part of the shared state:

- **Diffs** — ACP tool calls are compacted for history, but edit evidence (diffs, written text) is captured separately in a content-addressed turn-diff store. The session document carries diff references, not full file bodies.
- **Code Collab** — a per-session file index is stored in a Flock document; file watchers and diffs update it independently of the transcript.
- **Attachments** — images/files are uploaded separately; history carries references and metadata, not bytes. Local images retain machine-local identity for offline reads.
- **PR/CI status** — `SessionMeta.pullRequests` and `pullRequestState` are written by the CLI PR poller, not the agent.
- **Previews** — browser/simulator preview state lives in the session document's `preview` map and in machine-side services.
- **Terminals** — PTYs are managed by the CLI, with bounded output; ACP terminal calls are a separate lifecycle.

This separation is deliberate: the CRDT should carry collaborative *decisions and display state*, not unbounded file contents or process streams.

---

## 9. Sync and transport

### 9.1 Local persistence

| Client | Storage |
|---|---|
| CLI daemon | `~/.lody/loro-repo/<workspaceId>/repo.sqlite3` (`apps/cli/src/lib/loro/sqlite-repo-store.ts:16-20`) |
| Electron renderer | IndexedDB via `IndexedDBStorageAdaptor` |
| Web | IndexedDB |
| Mobile | Native runtime storage |

The SQLite store holds:

- document snapshots and update journals;
- Flock document snapshots and update journals;
- metadata snapshot/updates;
- remote cursors;
- replica-bound checkpoints (`replica_checkpoints`).

Cursor saves are ordered after the data they cover. This is the difference between "resumed from a cursor" and "skipped history because the cursor advanced before the data was durable." Lody's docs are emphatic about this, and the storage adapter implements a durability barrier before each cursor save (`apps/cli/src/lib/loro/streams-transport.ts:95-99`).

### 9.2 Local data plane (Electron)

The renderer cannot directly use the CLI's in-process repo. Electron therefore exposes a dedicated local data plane:

- a Unix socket in a 0700 run directory;
- protocol version 7, push-only, peer-scoped;
- every message carries `workspaceId` + `peerId`;
- the server keys sync state per peer (`lastSentVV`, Flock bundle hash);
- doc rooms sync by version-vector deltas;
- Flock rooms export independently importable LWW entries;
- outbound work is serialized through one process-wide writer with backpressure;
- large doc deltas are chunked into `doc-update-chunk` frames;
- ping/pong + idle timeout acts as a watchdog.

Key docs: `.agents/docs/cli-lib-local-loro-data-plane.md`; code: `packages/shared/src/local-loro-data-plane-server.ts`, `packages/shared/src/local-loro-transport.ts`, `apps/electron/src/main/services/loro-data-plane-relay.ts`.

The renderer's connection is **broadcast**: the relay sends every server push to every window, and each adapter filters by `workspaceId` + `peerId`. The server does not echo a peer's own operations back to it.

A renderer that opens a historical session does **not** retain a live cloud room. It performs a bounded one-shot `joinDocRoom` reconciliation and releases it after first sync or leave. Otherwise visiting thousands of historical conversations would retain thousands of rooms. This is a deliberate memory/lifecycle rule.

### 9.3 Cloud data plane

The cloud transport is a `StreamsTransportAdapter`:

- `metaStreamId = <workspaceId>:meta`
- `docStreamId = <workspaceId>:s:<sessionId>`
- `flockDocStreamId = <flockDocId>`
- JWT from `/api/loro-streams/token`
- snapshot upload every 5 seconds (plaintext) or via the protected admission policy
- Zstandard snapshot codec
- shard URLs derived from the token provider's shard suffix
- SSE live reads, bounded long-poll fallback

Lody's `rpc-loro-streams-rpc.md` notes the live-read watchdog: the SDK's connect timeout applies only to the initial SSE fetch, so a server that holds the connection open silently can hang forever. The client watchdog closes and retries.

### 9.4 Routing: local, cloud, dual

`WorkspaceTargetRouter` decides which transport joins each room (`packages/components/src/providers/workspace-target-router.ts:244-270`):

| Room | local-only build | cloud build | Electron dual build |
|---|---|---|---|
| meta | local | cloud | local + cloud |
| doc owned by local machine | local | cloud | local + cloud |
| doc owned by remote machine | local (no remote execution exists in this build) | cloud | cloud |
| workspace-scoped doc | local | cloud | cloud |

The precise rule:

- `syncMode === 'local'`: every room mounts only `local`; zero cloud I/O.
- `syncMode === 'cloud'`: every room mounts only `cloud`.
- `syncMode === 'dual'`:
  - `meta` is `['local', 'cloud']`;
  - a locally owned doc/Flock is `['local', 'cloud']`;
  - a remote or unknown-owner doc/Flock is `['cloud']`.

The local plane is the **readiness owner** for dual-homed rooms; the cloud transport converges renderer-authored operations best-effort. This is the "two planes, not one" rule: cloud failure must not poison the local reconnect loop, and local failure must not be reported as cloud health.

The renderer does not fall back to cloud if the local bridge is missing in Electron dual mode. It fails initialization instead. A local-first app with no local transport would be a silent data black hole (`packages/components/src/providers/create-workspace-runtime.ts:3631-3642`).

### 9.5 Reconnect and convergence

- Doc rooms reconcile by version vectors.
- Flock rooms reconcile by LWW bundles and version vectors.
- Every (re)join is a reconciliation point: the renderer up-syncs its delta from the server's returned version even if its in-memory dirty flag is false.
- Pending imports survive restarts; a snapshot alone is not enough if there are received-but-unintegrated operations.
- Reconnect is not replay. The client asks "what am I missing?" rather than replaying a message log.
- Presence has its own TTL and is not used to prove content sync.

### 9.6 Presence

Presence is deliberately **not** in the durable document (`specs/loro-ephemeral-presence-channel.md`):

- machine heartbeats;
- active-session status;
- "who is viewing this session."

It uses a Loro `EphemeralStore` over Streams:

- 30s heartbeat;
- 90s TTL (`packages/shared/src/presence.ts:19-20`);
- latest-state-only;
- serial publication;
- no history replay;
- separate from durable docs.

The spec records a real failure: one session published a presence update per reasoning chunk, producing 4,000 writes in three seconds; the workspace's machine then read as offline while conversation sync stayed healthy. The invariant is: publish presence only from timers, lifecycle transitions, or navigation, never from stream/progress callbacks.

Lody also keeps **two presence stores**:

- a workspace replica containing local + remote entries;
- a local-origin store containing only this process's entries.

The local data plane relays only the local-origin store, so a renderer does not gain authority over remote peers it already replicates from the cloud.

### 9.7 Machine RPC

Cross-machine commands use JSON streams on Loro Streams, not the CRDT doc:

- request stream: `<workspaceId>:rpc:req:<machineId>`
- response stream: `<workspaceId>:rpc:res:<uuid>` (new) or legacy per-machine form
- retention: 24 hours (`packages/loro-streams-rpc/src/rpc.ts:217`)
- SSE-first, long-poll fallback
- request IDs, replay after server restart, owner-side receipts
- encrypted dispatch for E2EE workspaces

Machine RPC is the low-latency path for `session/dispatch-turn`, `session/history-watch`, file preview, Code Collab, terminal input, and lifecycle commands. It is not a replacement for document sync: a request can succeed while the document has not converged, and vice versa.

One security detail matters here: workspace Machine RPC authenticates workspace access, not the individual member's identity. Lody's rules therefore forbid sending an untrusted requester through workspace Machine RPC without a separate identity/authorization check (`apps/cli/src/session/AGENTS.md`).

---

## 10. Access, sharing, and security

### 10.1 Effective access

`specs/session-access.md` is a **draft** contract; its owner decisions were pending approval when this report was written. It defines the live access matrix (`specs/session-access.md:21-29`):

| Source | Access |
|---|---|
| Machine owner | Manage and edit; always visible |
| Shared machine and, for a local project, shared project | Edit |
| Matching team or personal session read grant | Read only |
| Assigned session owner | Read only, unless machine/project access permits editing |
| None | Hidden |

Key points:

- Only the owner of the machine named by `SessionMeta.machineId` manages sharing.
- `SessionMeta.userId` is a read-only visibility fallback, not execution authority.
- A session read grant never opens machine/project execution switches.
- Grants apply only when their machine matches the current metadata.
- Contained tabs/side chats inherit only direct, same-machine grants.
- A read-only page hides Stop, steer, queue, permission replies, run controls, file saves, terminal input, Commit/Push, and previews.

Ordinary workspaces store the team session-ID set and explicit `{ sessionId, userId }` read grants on machine rows in Convex. Encrypted workspaces store owner-signed v3 policies with `{ sessionId, audience, level: "read" }` independent of machine/project switches. The public repo owns the DTOs and capability-gated calls; the backend handlers are private.

### 10.2 The trust boundary

The spec is unusually candid:

> "This prevents accidental interface exposure for honest members; it does not isolate malicious members who directly edit synchronized data."  
> — `specs/session-access.md:99-104`

`machineId`, containment, and `userId` are mutable CRDT fields. A malicious member who can write the document can forge them. There is no authoritative session-to-machine registry in this phase.

This is a general property of CRDT collaboration: the document is shared state, not an authorization database. Lody's mitigation is interface-level plus, in E2EE workspaces, signed policies and signed execution intents.

For E2EE, the spec adds another caveat: members retain workspace content keys, so read grants are interface hiding, not cryptographic isolation between members. Stronger isolation needs per-session keys and server filtering by session, which is a later phase.

### 10.3 Static share links

`specs/session-sharing.md` is another **draft** contract and describes a different feature:

- A workspace writer publishes a selected conversation and related conversations as an **independent readable copy**.
- The copy is frozen; later source edits do not change it.
- "Update deployment" captures a new copy and atomically replaces the current deployment without changing the link.
- The link is `/s/<shareId>#access=v1.<secret>` with a random 32-byte bearer.
- The control plane stores a hash, not the raw secret.
- Readers need no Lody login; the bearer is the access credential, and the page has no login entry.
- Readers pin a deployment; no Loro subscription.
- Source deletion does not remove the published copy.
- Reset rotates the credential; revoke disables every deployment.
- The publisher can update/reset/revoke their own share; admins can revoke others, not take over.
- E2EE workspaces require explicit consent because sharing publishes a decrypted copy outside E2EE.

This is not live collaboration. It is a snapshot with a capability URL.

### 10.4 E2EE

The optional encrypted workspace mode is a separate architecture:

- A **signed membership ledger** decides membership and the current epoch key.
- Keys are delivered to admitted devices through an untrusted mailbox.
- Documents are sealed with keys derived from the epoch key.
- The backend stores ciphertext and relays messages, but cannot read content or forge membership.
- Per-room payload protection uses an application namespace plus logical room identity as AAD; the server URL is not cryptographic identity.
- Owner-signed machine-sharing policies carry read grants.
- Signed execution intents bind a machine-authored input to its content in the same commit.
- Presence is the deliberate plaintext exception, with a minimal allowlist and bounded fields.

Relevant code/docs: `packages/e2ee-core/README.md`, `specs/e2ee-foundations.md`, `.agents/docs/e2ee-workspace-bootstrap.md`, `packages/shared/src/workspace-streams-content.ts`.

### 10.5 Machine access

Execution is authorized separately from content visibility:

- `canUseMachineFromCliToken` checks workspace membership and machine/project visibility.
- The daemon may start from a cached allow: owner allow does not expire; a member allow is cached for one hour after the control plane last confirmed it, keyed by requester + local project + account + machine.
- A transport failure is not a denial. It retries, then fails closed with a retryable error.
- A definitive denial clears the cache.

This is the local-first compromise: an offline machine can still execute for the owner, and for a member whose allow was recently confirmed, but a revoked member cannot extend access by going offline.

---

## 11. Scaling and deployment

**Short answer:** yes for the collaboration/control/content tiers, no for the execution tier. Lody can run its web/UI and stream/control services across many containers, but a live conversation's agent still runs on exactly one owning machine. There is no stateless pool of "chat workers" that can pick up an arbitrary running session.

### 11.1 What scales horizontally

- **Web/UI containers** — Lody's client design keeps no authoritative session state in the web tier, so those containers can be replicated and any instance can serve the app. Session state is in the client replica + Streams.
- **Loro Streams** — sharded by stream ID; a session is a stream. Clients resume by offset/version vector, not by connecting to the same server process.
- **Convex** — control-plane queries and mutations; scales independently of content.
- **Static share readers** — read immutable objects; no CRDT subscription.
- **Machine RPC** — durable request/response streams are not tied to a particular gateway instance; requests can be replayed/recovered.

### 11.2 What does not scale horizontally

- **The owning daemon** — one machine owns a session. Execution does not migrate when the machine goes offline.
- **The active turn** — one active turn per session; a second turn queues or steers.
- **The local SQLite replica** — per machine, not shared across containers.
- **An agent process** — one per active session; ACP is 1:1.
- **Local presence writer** — one serial queue per workspace.

### 11.3 Multi-container implications

If Lody's cloud were run in multiple containers:

- The **web tier** can be replicated freely; it holds no authoritative chat state.
- The **content tier** (Streams) must provide durable, sharded streams with offset resume; a client can reconnect to any shard/gateway.
- The **control tier** (Convex) must provide consistent membership/ACL reads and authorization.
- The **execution tier** is not a pool of stateless chat workers. It is a set of machines, each running a CLI daemon with local disk, agent runtimes, and a session owner. A container that hosts a daemon is stateful for that machine.
- **No sticky routing by session** is required by the client design for content sync in the web tier. A deployment that still puts session state in one process would need it; Lody's replicated-document model does not.

Lody's own architecture docs describe the same separation: Convex for organization/permissions, Loro Streams for content and Machine RPC. The public repo cannot verify the deployed topology of either service.

### 11.4 Scaling bottlenecks

- **Document size and op volume.** A long session with many streaming tokens produces many operations; snapshots and compaction matter. Lody batches ACP updates at 10ms/100ms and uses Zstandard snapshots.
- **Snapshot cost.** A new client bootstraps from a snapshot; very large docs make first open expensive. Lody avoids opening historical docs in bulk.
- **One active turn.** A single session cannot execute two prompts concurrently by design.
- **Daemon memory.** Lody has an idle GC and memory-pressure eviction; cached session docs are bounded, but a daemon with many active sessions is still a large process.
- **Presence fan-out.** The ephemeral channel is a shared serial budget; high-rate publishers can starve heartbeats.
- **Streams quotas / retention.** The hosted service owns retention; Lody configures TTLs for some Flock docs and relies on Streams for the rest.

---

## 12. Trade-offs and design tensions

| Decision | Benefit | Cost |
|---|---|---|
| CRDT transcript instead of server-owned chat | Multi-writer, offline, local-first, no central transcript server | Op volume, snapshots, semantic conflicts, harder authorization |
| Dual-author (clients write user data; daemon writes agent data) | Low-latency local writes; no proxy-authoring bottleneck | Two writers can race on metadata; needs careful activation rules |
| Session doc + meta Flock split | Fast list/startup; lazy bodies | Two consistency domains; pointers can disagree with history |
| Single machine owner | Simple execution, credentials, worktrees, local tools | No failover; offline machine means no execution |
| ACP 1:1 | Works with existing agents unchanged | Collaboration must be built entirely above ACP |
| Presence separate from durable doc | Cheap liveness, no transcript churn | A separate failure domain; can falsely report offline |
| Interface-level ACL | Simple, compatible with CRDT | Not a security boundary against malicious writers |
| E2EE optional | Strong content privacy | Key management complexity; sharing/read grants become policy, not cryptography |
| Static share as snapshot | Simple, durable, revocable link | Not live; content can be stale; disclosure is irreversible |
| Loro Streams hosted | Durable sync, snapshots, sharding | External dependency; server internals not public |

The deepest tension is **collaboration vs authority**. A CRDT is intentionally permissive: any replica can write. Lody wants team collaboration, but also wants a single executor and meaningful access control. It resolves this by:

- making execution authority a separate, pinned, single-owner fact (`machineId`);
- making most collaboration data shared and convergent;
- accepting that access control is interface-level for honest members;
- adding E2EE signatures for the cases where forgery matters.

That is a coherent design, but it is not a security boundary in the classical sense.

---

## 13. What is reusable (and what to copy carefully)

If you are building a similar system, the transferable patterns are:

1. **Separate control, content, and execution planes.** Do not put ACLs, transcripts, and agent processes in one service.
2. **Make the transcript a replicated document only if you need multi-writer/offline.** If you only need many readers and one writer, a sequenced event log is simpler.
3. **Keep ACP at the edge.** One ACP connection per agent session; translate events into your own durable model.
4. **Single execution owner.** Pin a session to a machine; do not migrate a running agent.
5. **Separate activation from content.** Writing a user turn should not implicitly start work; an activation pointer/queue makes dispatch recoverable.
6. **Make writes idempotent.** Use stable turn IDs, operation IDs, receipts, and dedupe on replay.
7. **Do not use presence as a data path.** Presence is latest-state liveness; transcripts are durable.
8. **Batch streaming writes.** One CRDT op per token is a scalability trap.
9. **Persist data before advancing cursors.** Otherwise reconnect skips history.
10. **Treat ACL as policy unless you have cryptographic enforcement.** A mutable CRDT field is not an authorization claim.

A useful comparison:

- **Lody** = CRDT document + single machine executor.
- **A conventional chat app** = server-owned log + fanout.
- **t3code** (from the earlier comparison) = server-owned SQLite event log + sequence replay.
- **hrns** = single attached ACP client + replay.

Lody is the most ambitious of these because it makes the conversation itself multi-writer. That is also why it has the most moving parts.

---

## 14. Python SDK parity (verified)

**Verdict:** Loro **core** is usable from Python — the wheel exposes the core CRDT API, including every capability Lody relies on. Lody's **Loro Stack wrappers** — `loro-mirror`, `LoroRepo`, the Loro Streams client, and Flock — are not available as Python packages in this checkout and would need to be reimplemented or replaced.

### 14.1 Verification environment

I installed the official Python bindings (`loro` on PyPI, [`loro-dev/loro-py`](https://github.com/loro-dev/loro-py)) in an isolated virtual environment:

```text
loro==1.17.1          (PyPI, PyO3 wheel, core 1.17.1)
Python 3.14.5          (CPython, macOS arm64)
loro-crdt@1.17.0       (JavaScript, for cross-language interop)
```

The package declares Python `>=3.8` and ships `loro/__init__.pyi` with typed bindings. I exercised 16 top-level feature groups (covering the rows below) against the real package; all passed. Only the macOS arm64 / CPython 3.14 wheel was exercised; other platform wheels were not. The verification script was kept outside the repository.

### 14.2 Loro core: feature matrix

| Capability used by Lody / described in this report | Python API | Result |
|---|---|---|
| Create a document | `LoroDoc()` | PASS |
| Set/read peer ID | `doc.peer_id = ...` | PASS |
| Root map/list/text/movable-list/tree/counter | `get_map`, `get_list`, `get_text`, `get_movable_list`, `get_tree`, `get_counter` | PASS |
| Nested containers | `insert_container`, `push_container`, `get_or_create_container` | PASS |
| Mergeable children | `ensure_mergeable_map/list/text/movable_list/tree/counter` | PASS |
| Map insert/delete/keys/items | `insert`, `delete`, `keys`, `items` | PASS |
| List insert/delete/push/pop | `insert`, `delete`, `push`, `pop` | PASS |
| Movable-list reorder | `LoroMovableList.mov(from_, to)` | PASS |
| Text insert/delete/splice/update | `insert`, `delete`, `splice`, `update`, `update_by_line` | PASS |
| Rich text marks/delta | `mark`, `unmark`, `to_delta`, `apply_delta` | PASS |
| Cursors | `get_cursor`, `doc.get_cursor_pos` | PASS |
| Tree create/move/delete/meta | `create`, `mov`, `mov_to`, `delete`, `get_meta` | PASS |
| Counter increment/decrement | `increment`, `decrement`, `value` | PASS |
| Commit | `commit`, `commit_with`, `set_next_commit_*` | PASS |
| Snapshot export/import | `ExportMode.Snapshot()`, `import_()` | PASS |
| Delta export/import | `ExportMode.Updates(vv)`, `import_()` | PASS |
| Updates in an ID range | `ExportMode.UpdatesInRange([IdSpan])` | PASS |
| Shallow snapshot | `ExportMode.ShallowSnapshot(frontiers)` | PASS |
| State-only snapshot | `ExportMode.StateOnly(None)` | PASS |
| Snapshot at version | `ExportMode.SnapshotAt(frontiers)` | PASS |
| Pending imports | `ImportStatus.pending`, `export_pending_updates()` | PASS |
| Version vectors | `oplog_vv`, `state_vv`, `VersionVector` | PASS |
| Frontiers | `oplog_frontiers`, `state_frontiers`, `Frontiers` | PASS |
| JSON updates | `export_json_updates`, `import_json_updates` | PASS |
| Diff / apply diff | `diff`, `apply_diff` | PASS |
| Fork | `fork`, `fork_at` | PASS |
| Checkout/attach/detach | `checkout`, `attach`, `detach`, `set_detached_editing` | PASS |
| Root/local subscriptions | `subscribe_root`, `subscribe_local_update` | PASS |
| Container subscriptions | `container.subscribe` | PASS |
| Ephemeral presence store | `EphemeralStore(timeout)` | PASS |
| Awareness | `Awareness(peer, timeout)` | PASS |
| Undo/redo | `UndoManager` | PASS |
| Revert to version | `revert_to` | PASS |
| JSONPath | `jsonpath`, `subscribe_jsonpath` | PASS |
| Redaction | `redact_json_updates` | PASS |
| Import batches / origin | `import_batch`, `import_with` | PASS |

The tested Python equivalents of Lody's calls are:

```python
from loro import LoroDoc, LoroMap, LoroText, ExportMode, VersionVector

doc = LoroDoc()
turn = doc.get_list("history").insert_container(0, LoroMap())
turn.insert("id", "turn-1")
text = turn.insert_container("text", LoroText())
text.insert(0, "hello")
doc.commit()

snapshot = doc.export(ExportMode.Snapshot())
delta = doc.export(ExportMode.Updates(peer_vv))
doc.import_(delta)              # note the underscore
# doc.oplog_vv / doc.state_vv / doc.oplog_frontiers
```

### 14.3 API naming differences

The Python API is close to the JS API but not identical:

| JS / Lody usage | Python |
|---|---|
| `doc.import(bytes)` | `doc.import_(bytes)` |
| `doc.export({ mode: "snapshot" })` | `doc.export(ExportMode.Snapshot())` |
| `doc.export({ mode: "update", from: vv })` | `doc.export(ExportMode.Updates(vv))` |
| `doc.oplogVersion()` | `doc.oplog_vv` |
| `doc.version()` / state version | `doc.state_vv` |
| `doc.frontiers()` | `doc.oplog_frontiers` (or `state_frontiers`) |
| `doc.setPeerId(n)` | `doc.peer_id = n` |
| `LoroMovableList.move(from, to)` | `LoroMovableList.mov(from_, to)` |
| `doc.subscribeRoot(cb)` | `doc.subscribe_root(cb)` |
| `doc.subscribeLocalUpdates(cb)` | `doc.subscribe_local_update(cb)` |
| `doc.exportPendingUpdates()` | `doc.export_pending_updates()` |
| `doc.applyDiff(diff)` | `doc.apply_diff(diff)` |

The callback contract also differs in one place: `EphemeralStore.subscribe` expects a callback returning a truthy value to keep the subscription alive; returning `None` triggers a Rust panic in 1.17.1. Return `True` explicitly.

A few JavaScript debug/history conveniences are not exposed as direct Python methods in 1.17.1: `debugHistory`, `getAllChanges`, `getOpsInChange`, `getChangeAtLamport`, `getUncommittedOpsAsJson`, and `peerIdStr`. None are used by Lody. Python's `peer_id` is an integer; the event/history APIs that remain are sufficient for Lody's version-vector and snapshot workflows.

One subtle parity issue: Lody's JavaScript code uses JavaScript string indexes, which are **UTF-16 code units**. Python's default `LoroText.insert`, `delete`, `slice`, and `len` use **Unicode code points**. For text with emoji or other non-BMP characters, use `insert_utf16`, `delete_utf16`, `slice_utf16`, `len_utf16`, and the `*_utf16` mark/unmark variants if you need to match Lody's offsets exactly. Python's `insert_utf16(3, ...)` inserts before `b` in `a😀b`, while `insert(2, ...)` inserts after the emoji by code-point position.

### 14.4 Lody-specific layers that are not in the Python SDK

These are separate packages or application code, not Loro core:

| Lody layer | Python status | Practical replacement |
|---|---|---|
| `loro-mirror` (typed schema + Immer) | No PyPI package | Pydantic models + manual container mapping, or a small schema layer |
| `LoroRepo` (metadata, rooms, transports, cursors) | No PyPI package | Build a collection layer over `LoroDoc` + SQLite + HTTP/WebSocket |
| Loro Streams client (`@loro-dev/streams-crdt`) | No PyPI package | Implement the Streams HTTP/SSE protocol, or use `pycrdt-websocket` |
| Flock (`@loro-dev/flock-wasm`) | No official Python package; PyPI `flock` is unrelated file locking, `pyflock` is a placeholder | SQLite/Postgres rows, Redis, or a LoroMap |
| Lody `HistoryWriter` / `session-mirror` | Application code | Reimplement validation/diff/write rules |
| Lody `LoroText` streaming merge | Application code | Use `LoroText.update` / `insert` with your own chunk merge |
| Streams snapshot upload, sharding, cursors | Transport code | Implement with your own object storage and offset/version-vector tracking |
| `EphemeralStoreAdaptor` | Core `EphemeralStore` exists; adaptor does not | Publish encoded ephemeral updates over your own transport |

One nuance: Flock is not just a LoroMap. Flock resolves conflicts with an HLC-like clock (physical time, logical counter, peer ID), while LoroMap uses Loro's own ordering. A LoroMap can store metadata in a Python service, but it does not reproduce Flock's exact conflict semantics.

This distinction matters: **"Loro is available in Python" is true; "Lody's entire collaboration stack is available in Python" is not.**

### 14.5 Cross-language interoperability

I also verified binary interoperability between the Python and JavaScript bindings:

1. Python created a document with a map, text, and movable list; exported a snapshot.
2. Node.js `loro-crdt@1.17.0` imported the snapshot, read the values, appended text, and exported a new snapshot.
3. Python imported the Node snapshot and read the appended text and reordered movable list.
4. Node exported a second document snapshot; Python imported it successfully.

Both snapshot and update exchange passed in both directions. Since Lody uses `loro-crdt@1.17.0` and the Python wheel is `loro@1.17.1` on the same core line, a Python service can exchange snapshots/updates with Loro JS clients in principle. This test is a compatibility smoke test, not a formal guarantee for every Loro version.

### 14.6 Practical conclusion for a Python service

A Python backend can own the authoritative `LoroDoc` for a session and sync with Loro clients, but it must supply the layers Lody gets from `loro-repo` and Loro Streams:

- a WebSocket/SSE transport with per-peer version vectors;
- snapshot + update persistence and compaction;
- pending-import persistence;
- room/collection metadata;
- schema validation and a `HistoryWriter` equivalent;
- presence and RPC as separate channels.

If the requirement is only many readers and one writer, a sequenced event log is still simpler than a CRDT. If multi-writer/offline editing is required, `loro` Python is a viable document core; the missing work is the collaboration substrate around it.

---

## 15. Evidence, limits, and open questions

### 15.1 What is verified

- The public CLI, Electron, mobile, shared packages, specs, and architecture notes at the pinned commit.
- Loro's public API and CRDT semantics, plus a Python SDK smoke test and JavaScript/Python snapshot/update interop.
- `loro-repo` and Flock public package documentation.
- Lody's own code paths for ACP spawn, update handling, permission resolution, dispatch, sync, presence, and access checks.

### 15.2 What is not verified

- The hosted Convex backend implementation, deployment topology, and exact authorization queries.
- The Loro Streams server implementation, retention behavior under load, and multi-tenant isolation.
- The private Web app composition.
- A real two-account, multi-device acceptance test.
- Production behavior of the draft `session-access` and `session-sharing` specs.

### 15.3 Open questions

- How does Lody handle a session document that grows to hundreds of megabytes? Snapshot/compaction policy is visible only at the client and Streams adapter level.
- How are per-user read receipts represented in a shared transcript? The visible design stores a shared read/seen status; a per-user inbox is not evident in the public code.
- What is the exact authorization model at the Streams room level? The client requests a workspace token; per-session read-only enforcement is interface-level.
- How does the system behave when two clients simultaneously promote different queue items? The promotion ledger and serialized daemon writer are designed for this, but a multi-account stress test is not public.
- How does Lody migrate a session to another machine? The current design pins ownership; ownership transfer is a separate operation requiring recipient machine access.

---

## Appendix A: Key file map

| Area | File |
|---|---|
| Product overview | `README.md` |
| Public/private boundary | `AGENTS.md` |
| Communication architecture | `specs/communication-architecture.zh.md` |
| Session access | `specs/session-access.md` |
| Static sharing | `specs/session-sharing.md` |
| Presence contract | `specs/loro-ephemeral-presence-channel.md` |
| Session doc schema | `packages/shared/src/schema.ts` |
| History writer | `packages/shared/src/history-writer.ts` |
| Session mirror | `packages/shared/src/session-mirror.ts` |
| Loro session data | `packages/shared/src/session-data/loro.ts` |
| Shared planners | `packages/shared/src/session-data/planner.ts` |
| ACP update mapping | `packages/shared/src/acp/history-apply.ts` |
| Streaming text merge | `packages/shared/src/history-stream-text.ts` |
| Loro document manager | `apps/cli/src/lib/loro/doc.ts` |
| Streams transport | `apps/cli/src/lib/loro/streams-transport.ts` |
| SQLite repo store | `apps/cli/src/lib/loro/sqlite-repo-store.ts` |
| Local data plane | `packages/shared/src/local-loro-data-plane-server.ts` |
| Local transport | `packages/shared/src/local-loro-transport.ts` |
| Electron relay | `apps/electron/src/main/services/loro-data-plane-relay.ts` |
| Workspace runtime | `packages/components/src/providers/create-workspace-runtime.ts` |
| Transport routing | `packages/components/src/providers/workspace-target-router.ts` |
| ACP client | `apps/cli/src/agent/agent-client.ts` |
| ACP runner | `apps/cli/src/agent/acp-runner.ts` |
| Session / process | `apps/cli/src/session/session.ts` |
| Dispatch watcher | `apps/cli/src/session/session-dispatch-watcher.ts` |
| Dispatch logic | `apps/cli/src/session/session-dispatch-logic.ts` |
| Execution service | `apps/cli/src/session/session-execution-service.ts` |
| Message handler | `apps/cli/src/lib/message-handler.ts` |
| Machine RPC | `packages/loro-streams-rpc/src/machine-rpc-server.ts` |
| E2EE core | `packages/e2ee-core/README.md` |
| Workspace streams content | `packages/shared/src/workspace-streams-content.ts` |

## Appendix B: Glossary

- **ACP** — Agent Client Protocol. JSON-RPC protocol between an editor/client and a coding agent.
- **LoroDoc** — A Loro CRDT document; replicated JSON-like state with an OpLog.
- **Flock** — A Lody/Loro LWW key-value CRDT, used for metadata and catalogs.
- **LoroRepo** — Collection-sync layer above LoroDoc/Flock: metadata, rooms, transports, storage, cursors.
- **Loro Streams** — Hosted durable-stream service for CRDT snapshots/updates, presence, and JSON RPC streams.
- **Session** — A Lody conversation; a Loro document plus a metadata row.
- **Turn** — One user or assistant entry in the conversation.
- **Machine** — A computer running the Lody CLI daemon and agents.
- **Owning machine** — The machine named by `SessionMeta.machineId`; the only executor for that session.
- **Meta Flock** — The repo's internal metadata document: document registry, session metadata, activation pointers.
- **Dual-author** — The renderer writes user/UI data; the CLI writes agent data.
- **Static share** — An immutable published copy of conversations behind a bearer link.
