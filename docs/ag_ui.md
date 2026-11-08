# Read-only AG-UI observation

Install `hrns[server,ag-ui]` (or `uv sync --extra server --extra ag-ui`).
`AcpServer` does not import or require AG-UI. `HrnsServer` adds a publisher to each
live session and an independent HTTP mount:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from hrns.server import HrnsServer, build_agent

server = HrnsServer()
coder = server.add_agent("coder", factory=lambda: build_agent(cwd="/srv/project"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with server:
        # Optional server-started work, independent of a client connection:
        session = await coder.new_session(cwd="/srv/project")
        turn = await session.submit("Investigate the project")
        yield

app = FastAPI(lifespan=lifespan)
server.mount_acp(app, prefix="/acp")
server.mount_ag_ui(app, prefix="/ag-ui")
```

| Route | Result |
| --- | --- |
| `WS /acp/{agent_id}` | ACP attachment and interaction |
| `GET /ag-ui/{agent_id}/sessions/{session_id}` | Read-only messages, session state, and current-run replay events |
| `GET /ag-ui/{agent_id}/sessions/{session_id}/events` | Long-lived AG-UI SSE |

Pass the same `authenticate` callback to both mounts; it accepts Starlette's
`HTTPConnection` for HTTP and WebSocket. Unknown or nonresident sessions return 404.
Observation does not invoke factories, restore sessions, submit prompts, or execute
an agent. Unsupported mutation methods receive 405. OpenAPI and `readOnly: true`
application metadata describe this contract; there is no claimed standard AG-UI
read-only capability.

## Events and replay

The publisher uses Pydantic AI's public `AGUIEventStream.transform_stream` and encoder.
Each native run is converted once while its native events also feed ACP. It never
mounts `AGUIAdapter.dispatch_request` or accepts `RunAgentInput`.

A subscription receives `MESSAGES_SNAPSHOT`, `STATE_SNAPSHOT`, the current native run's
event prefix, and live events. Snapshot and subscriber installation happen atomically
on the runtime event loop. Idle subscriptions wait for future turns. Native thread/run
IDs match the server session/native run IDs, and all subscribers share message IDs.
Completed messages use `AGUIAdapter.dump_messages`; an observer-side identity table
associates native response order and tool-call IDs with streamed message IDs. UI IDs
are kept out of model history.

A turn may contain several native runs around approvals/questions. Each has its own
upstream run lifecycle; waiting between them is session state, not application-turn
completion. State snapshots contain:

```json
{"hrns": {"sessionId": "...", "revision": 2, "status": "waiting",
          "activeTurnId": "...", "lastOutcome": null, "lastUsage": null},
 "readOnly": true}
```

The snapshot endpoint returns `messages` in AG-UI's message format and `events` as
encoded SSE frames required to reconstruct the active run. On cancellation, the
publisher dumps Pydantic AI's interrupted-run snapshot for display. Partial display
messages survive in the process-local observer history while committed model history
remains unchanged. They are not restored after restart.

Subscriber queues hold at most 256 events. Overflow closes that subscription; reconnect
to obtain a fresh snapshot and current-run replay. Events are never arbitrarily dropped
from an open subscription, and slow subscribers do not block the model or ACP.
SSE sends keep-alive comments while idle. Disconnect closes only that subscription.

The current native run's encoded replay prefix grows with its event volume and is
released when the run completes. This is a process-local, single-worker API. There is
no persistent event log, cross-worker fan-out, frontend, or client mutation route.

See [agent servers](mount-acp.md) for queue controls and ACP interaction semantics,
and [Pydantic AI's AG-UI integration](https://pydantic.dev/docs/ai/integrations/ui/ag-ui/)
for the event conversion API.
