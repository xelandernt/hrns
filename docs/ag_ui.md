# Read-only AG-UI observation

Install `hrns[server,ag-ui]` (or `uv sync --extra server --extra ag-ui`).
`HrnsServer` adds a publisher to each
live session and an independent HTTP mount:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from hrns.server import HrnsServer, build_agent

server = HrnsServer()
coder = server.add_agent("coder", factory=lambda: build_agent("anthropic:claude-sonnet-5-5", cwd="/srv/project"))

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
Observation reads resident session state. Mutation methods receive 405.
OpenAPI and application metadata (`readOnly: true`) describe the endpoint contract.

## Events and replay

The publisher converts each native run once using `AGUIEventStream.transform_stream`
and its encoder. The same native events also feed ACP.

A subscription receives `MESSAGES_SNAPSHOT`, `STATE_SNAPSHOT`, the current native run's
event prefix, and live events. Snapshot and subscriber installation happen atomically
on the runtime event loop. Idle subscriptions wait for future turns. Native thread/run
IDs match the server session/native run IDs, and all subscribers share message IDs.
Completed messages use `AGUIAdapter.dump_messages`; an observer-side identity table
associates native response order and tool-call IDs with streamed message IDs. UI IDs
are kept out of model history.

A turn may contain several native runs around approvals/questions. Each has its own
upstream run lifecycle; session state reports the wait between native runs. State snapshots contain:

```json
{"hrns": {"sessionId": "...", "revision": 2, "status": "waiting",
          "activeTurnId": "...", "lastOutcome": null, "lastUsage": null},
 "readOnly": true}
```

The snapshot endpoint returns `messages` in AG-UI's message format and `events` as
encoded SSE frames required to reconstruct the active run. On cancellation, the
publisher renders the interrupted-run snapshot for display. Partial display
messages survive in the process-local observer history while committed model history
contains completed model messages. Partial display history lasts for the process lifetime.

Subscriber queues hold at most 256 events. Overflow closes that subscription; reconnect
to obtain a fresh snapshot and current-run replay. Each open subscription delivers
its events in order, with queue limits isolating slow subscribers.
SSE sends keep-alive comments while idle. Disconnect closes only that subscription.

The current native run's encoded replay prefix grows with its event volume and is
released when the run completes. Deploy one process and worker per runtime;
subscriptions, replay buffers, and partial display history live in that process.

See [agent servers](mount-acp.md) for queue controls and ACP interaction semantics,
and [Pydantic AI's AG-UI integration](https://pydantic.dev/docs/ai/integrations/ui/ag-ui/)
for the event conversion API.
