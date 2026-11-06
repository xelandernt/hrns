# hrns

Define one Pydantic AI agent and expose it over ACP.

```python
from hrns.app import build_agent

agent = build_agent("anthropic:claude-sonnet-5-5")
```

The backend is deliberately small:

- `hrns.app.agent` builds the `Agent` from Pydantic AI capabilities.
- `hrns.app.acp` is this project's ACP adapter.
- `hrns.app.fastapi` mounts the adapter on FastAPI.

There is no custom session server, plugin registry, policy catalog, wire protocol,
or extension surface. ACP session persistence belongs to the adapter and the
application that hosts it. The terminal client speaks standard ACP only.

## Run ACP over stdio

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app acp
```

## Run ACP WebSocket over FastAPI

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app serve --port 8000
```

- `WS /acp` — ACP over WebSocket
