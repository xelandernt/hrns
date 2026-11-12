# Loro sessions

Every native session is backed by one [Loro](https://loro.dev) document. Python,
ACP, and any number of synchronized TypeScript replicas participate in the same
conversation while one server process owns the agent and executes it. Mounting ACP
is optional, and so is mounting Loro: both are interfaces to the same session.

The server extra installs the Python SDK (`loro>=1.17.1,<2`). It ships PyO3 wheels
for CPython 3.11+ on the usual macOS, Linux, and Windows platforms; the verified
combination for this documentation is CPython 3.14 on macOS arm64 with
`loro-crdt` 1.17.0 on Node 26. Client-only installs never import the server SDK.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from hrns.server import AcpServer, SQLiteDocumentStore, build_agent

server = AcpServer[None]()
store = SQLiteDocumentStore("sessions.sqlite", agent_id="coder")
coder = server.add_agent(
    "coder",
    factory=lambda session: build_agent(cwd=session.cwd),
    session_store=store,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with server:
        yield


app = FastAPI(lifespan=lifespan)
server.mount_acp(app, prefix="/acp")       # optional
server.mount_loro(app, prefix="/loro")     # creates, lists, and synchronizes documents
```

`SQLiteDocumentStore` is opt-in durable storage. The default is
`InMemoryDocumentStore`, which is process-local. Both persist a Loro checkpoint,
never a parallel JSON transcript. A single-process deployment does not need a
network database.

## Routes

| Method/path | Purpose |
| --- | --- |
| `POST {prefix}/{agent_id}/sessions` | Create a session. Body: `{"cwd": "/srv/project"}` |
| `GET {prefix}/{agent_id}/sessions` | List stored sessions; optional `cwd` and `cursor` |
| `GET {prefix}/{agent_id}/sessions/{session_id}` | Metadata, live status, and a base64 snapshot |
| `GET {prefix}/{agent_id}/sessions/{session_id}/snapshot` | Raw snapshot bytes |
| `DELETE {prefix}/{agent_id}/sessions/{session_id}` | Close a live session and delete its checkpoint |
| `WS {prefix}/{agent_id}/sessions/{session_id}/sync` | Synchronize one replica |

Pass the same `authenticate(HTTPConnection)` callback used by the other mounts.
A returned principal is passed to the store for `loro/snapshot`, `loro/list`,
`loro/sync`, and deletion. `None` rejects with HTTP 401 or WebSocket close 1008.
Denial is HTTP 403 / close 1008; an unknown session is HTTP 404.
[Authentication](authentication.md) describes the store contract.

Creating a session starts no model run. A session runs only when a contribution is
ready and the scheduler is awake.

## Document schema

The schema version is `1`; a document with any other version is rejected. Root
containers are fixed names; never repurpose one.

| Root key | Type | Writer | Purpose |
| --- | --- | --- | --- |
| `meta` | `LoroMap` | server | Schema version, agent/session ID, cwd, timestamps, counters |
| `lifecycle` | `LoroMap` | server | `status`, `revision`, active contribution, last outcome, `resume_required` |
| `settings` | `LoroMap` | server | Selected model, setting selections, cost, todos, invoked skills |
| `inputs` | `LoroList<LoroMap>` | everyone | Contributions; text in a `LoroText` child |
| `consumption` | `LoroList<LoroMap>` | server | Immutable capture records ordered by server sequence |
| `history` | `LoroList<LoroMap>` | server | Lossless native `ModelMessage` JSON, append-only |
| `display` | `LoroList<LoroMap>` | server | Display-only user/assistant/thinking/tool/plan/usage records |
| `interactions` | `LoroMap<LoroMap>` | everyone answers | Pending questions and approvals keyed by stable ID |

A contribution map has `id`, `author` (JSON), `state`, `origin`, `created_at`,
`blocks`, `skills`, optional `received_seq`/`consumed_seq`/`capture_version`, and a
`text` container. `blocks` is an optional JSON array of ACP prompt-content blocks
for non-text input; text-only clients leave it `"[]"`. `state` is one of `draft`, `ready`, `captured`, `delivered`,
`cancelled`, or `failed`. Structured fields (`author`, `blocks`, `skills`, display
`payload`, interaction `request`/`response`) are JSON *strings* in the map, because a
Loro map stores scalars; parse them in the client.

Each user display record carries the captured `author` object in its `payload`.
Native history is not a display transcript. `history` preserves tool calls/results,
cost/usage, run IDs, and metadata; `display` is a convenience projection that may
be partial after an interruption. A client must not write `history`, `consumption`,
`display`, `lifecycle`, or `meta`.

Text offsets in this contract are Unicode code points, matching Python
`LoroText`. JavaScript positions are UTF-16: convert at the boundary when editing
non-BMP text. Server-produced streamed text is append-only.

## Contributions and capture

Editing is collaborative; execution is not. A client creates a contribution with a
stable ID and `state: "draft"`, edits its `text`, and sets `state: "ready"` when it
should be eligible. A draft never executes.

At a model-request boundary the server:

1. selects every integrated contribution whose state is `ready`;
2. assigns server arrival order (`received_seq`) and monotonic consumption order
   (`consumed_seq`), so client clocks and CRDT list order never define order;
3. freezes the text and document version into an immutable `consumption` record;
4. persists that decision before any model dispatch;
5. delivers one native user request per contribution, with `metadata.author`,
   `contribution_id`, `consumption_seq`, and `capture_version`;
6. marks the consumption record and contribution delivered.

Input arriving while a tool is running is captured at the next request in the same
execution. Input arriving during the final response is consumed by a subsequent
execution; it never interrupts the current model request or tool call. A late edit
to captured text changes the visible draft only: the captured content is what the
model received. A correction is a new contribution.

Matching `consumption.seq` to a unique contribution is the receipt. Reconnect or
duplicate imports cannot deliver the same contribution twice. `cancelled` is the
outcome for a cleared ready contribution; `failed` marks a capture that an
interrupted process never delivered.

An explicit cancel stops the active execution and pauses the session. Ready
contributions stay pending until `resume_queue()` or `clear_queue()`. A restored
session is paused and `resume_required` is true; resume does not replay uncertain
tool side effects.

## Interactions

Questions and approvals are stored in `interactions` before any ACP request is
sent. Each has `id`, `kind` (`approval` or `question`), `state`, `request`,
optional `response`/`resolved_by`, and optional `detail`.

Approval answers use `{"option": "allow_once" | "allow_always" | "reject_once" |
"reject_always" | "cancel"}`. Question answers use
`{"action": "accept" | "decline" | "cancel", "content": {...}}` with the form fields
described by `request.schema` (the standard ACP form schema, also carried as
`request.questions`).

A Loro client answers by writing `response` and optional `resolved_by` while leaving
`state` pending. The server validates it, applies the first-answer rule, and sets
`state` to `resolved`; clients must not set `state` themselves. The server accepts the
first valid answer for a pending ID, from ACP or Loro, and records who resolved it.
Later, conflicting, stale, or cancelled answers change nothing. Disconnect retains a pending interaction; cancel marks it cancelled;
restart marks it `obsolete`. A client that lacks form support receives the existing
unavailable-question outcome.

## Wire protocol

One WebSocket binary message is one frame: one tag byte plus the payload.

| Frame | Tag | Payload |
| --- | --- | --- |
| `version` | `0x01` | JSON `{"schemaVersion":1,"vv":"<base64>","sessionId":"…","peerId":123}` |
| `update` | `0x02` | Raw Loro update bytes, at most 4 MiB |
| `snapshot` | `0x03` | `u32 headerLength`, JSON `{"index":0,"count":N}`, raw chunk ≤ 256 KiB |
| `ack` | `0x04` | JSON `{"durable":true,"seq":N}`, sent after the checkpoint is saved |
| `error` | `0x05` | JSON `{"code":"…","message":"…"}` |

Handshake:

1. The server sends `version` with its current version vector.
2. The client replies with `version`, including its own vector and peer ID.
3. If the client's vector is empty the server sends `snapshot` chunks; otherwise
   it sends only the `update` frames the client is missing.
4. The client imports what it received and sends the updates the server is missing,
   computed against the server's vector. Then each side sends local updates as they
   are committed.

Imported updates are fanned out to the other replicas, persisted, and only then
acknowledged. An import never starts a model run by itself; ready input does. The
server closes a peer that sends a frame over 4 MiB, more than 128 queued frames,
an unsupported schema, or a peer ID already connected: use close code 1008 for a
contract violation, 1013 for overflow, and 1011 for a persistence failure. A
reconnect always resynchronizes from version vectors rather than replaying a delta
log.

## Minimal TypeScript client

This example uses `loro-crdt@1.17.0`. It joins an existing session, edits pending
input, marks it ready, and receives output. A production UI would render `display`
records and pending interactions; this example logs them.

```ts
import { LoroDoc, LoroMap, LoroText, VersionVector } from "loro-crdt";

const base = "http://127.0.0.1:8000/loro/coder/sessions";
const headers = { authorization: "Bearer example-token", "content-type": "application/json" };
const created = await fetch(base, { method: "POST", headers, body: JSON.stringify({ cwd: "." }) });
const { sessionId } = await created.json();

const doc = new LoroDoc();
const peerId = crypto.getRandomValues(new Uint32Array(1))[0];
doc.setPeerId(peerId);
const ws = new WebSocket(`ws://127.0.0.1:8000/loro/coder/sessions/${sessionId}/sync`);
ws.binaryType = "arraybuffer";

const enc = new TextEncoder();
const dec = new TextDecoder();
const frame = (tag: number, payload: Uint8Array) => {
  const out = new Uint8Array(payload.length + 1);
  out[0] = tag;
  out.set(payload, 1);
  return out;
};
const json = (value: unknown) => enc.encode(JSON.stringify(value));
const b64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes));
const unb64 = (text: string) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));

let serverVv: VersionVector | null = null;
const snapshots: Record<number, Uint8Array> = {};
let snapshotCount = 0;

ws.onmessage = async (event) => {
  const data = new Uint8Array(event.data);
  const tag = data[0];
  if (tag === 0x01) {
    const hello = JSON.parse(dec.decode(data.subarray(1)));
    serverVv = VersionVector.decode(unb64(hello.vv));
    ws.send(frame(0x01, json({
      schemaVersion: 1,
      vv: b64(doc.oplogVersion().encode()),
      sessionId,
      peerId,
    })));
    const missing = doc.export({ mode: "update", from: serverVv });
    if (missing.length) ws.send(frame(0x02, missing));
  } else if (tag === 0x02) {
    doc.import(data.subarray(1));
  } else if (tag === 0x03) {
    const headerLength = new DataView(data.buffer, data.byteOffset + 1, 4).getUint32(0);
    const header = JSON.parse(dec.decode(data.subarray(5, 5 + headerLength)));
    snapshots[header.index] = data.subarray(5 + headerLength);
    snapshotCount = header.count;
    if (Object.keys(snapshots).length === snapshotCount) {
      const parts = Object.keys(snapshots)
        .sort((a, b) => Number(a) - Number(b))
        .map((key) => snapshots[Number(key)]);
      const size = parts.reduce((total, part) => total + part.length, 0);
      const snapshot = new Uint8Array(size);
      let offset = 0;
      for (const part of parts) { snapshot.set(part, offset); offset += part.length; }
      doc.import(snapshot);
    }
  } else if (tag === 0x04) {
    console.log("durable", JSON.parse(dec.decode(data.subarray(1))));
  }
};

// Add pending input the same way any replica does.
const inputs = doc.getList("inputs");
const contribution = inputs.pushContainer(new LoroMap());
contribution.set("id", crypto.randomUUID());
contribution.set("author", JSON.stringify({ id: "web", label: "Web", kind: "loro" }));
contribution.set("state", "draft");
contribution.set("origin", "loro");
contribution.set("created_at", Date.now());
contribution.set("blocks", "[]");
contribution.set("skills", "[]");
const text = contribution.setContainer("text", new LoroText());
text.insert(0, "Summarize the failing migration.");
doc.commit();

// Mark ready when the person presses send.
contribution.set("state", "ready");
const outbound = doc.export({ mode: "update", from: serverVv! });
doc.commit();
ws.send(frame(0x02, outbound));

// Later, read display records and pending interactions from `doc.getList`/`doc.getMap`.
```

Answering an interaction writes the answer into its map and commits, then sends
the delta the same way:

```ts
const interaction = doc.getMap("interactions").get(interactionId);
interaction?.set("response", JSON.stringify({ option: "allow_once" }));
interaction?.set("resolved_by", "web");
doc.commit();
```

A real client records the server's durable sequence per sent change and treats the
`ack` as durability, not as model consumption. Those are separate receipts.

## Python persistence and restart

```python
from hrns.server import SQLiteDocumentStore

store = SQLiteDocumentStore("sessions.sqlite", agent_id="coder")
```

One row per session stores the snapshot and pending imports atomically, plus
cwd/title/schema/update metadata for listing without opening the document. A fresh
process restores native history, pending contributions, settings, and partial
display. A session that was running is restored as `interrupted`, paused, with
`resume_required` true; pending interactions are marked `obsolete`. Nothing is
replayed automatically, because a tool side effect after a crash may have happened.

A persistence failure prevents session creation/prompt capture from being
acknowledged and prevents the next model dispatch. Shutdown flushes the current
checkpoint after resolving handles and closing replicas.

## Trusted writers

Admitted writers are trusted to edit the whole document. The writer column above
is an application convention, not field-level security: Loro peer IDs and
client-supplied `author` labels are not authentication. Use `authenticate` and the
store's operation checks for admission. Access decisions must stay outside mutable
document fields.
