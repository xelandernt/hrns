# hrns

Define one Pydantic AI agent and expose it over both ACP and AG-UI.

```python
from hrns.app import build_agent

agent = build_agent("anthropic:claude-sonnet-5-5")
```

The backend is deliberately small:

- `hrns.app.agent` builds the `Agent` from Pydantic AI capabilities.
- `hrns.app.acp` wraps it with the Pydantic AI Harness ACP adapter.
- `hrns.app.agui` wraps it with Pydantic AI's AG-UI adapter.
- `hrns.app.fastapi` mounts either adapter on FastAPI.

There is no custom session server, plugin registry, policy catalog, wire protocol,
or extension surface. ACP session persistence and AG-UI thread handling belong to
the adapters and the application that hosts them. The terminal client speaks
standard ACP only.

## Run ACP over stdio

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app acp
```

## Run ACP WebSocket and AG-UI over FastAPI

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app serve --port 8000
```

- `WS /acp` — ACP over WebSocket
- `POST /agui` — AG-UI over HTTP/SSE
