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

## Reference

- [Configuration](configuration.md) — the one `config.toml` both the client and the local
  runtime read: locations, precedence, and every field with an example.
- [Capabilities](capabilities.md) — what this repository adds on top of Pydantic AI and
  the harness: the `Coder` and its `apply_changes` batch tool, `Todo`, and the approval
  policy.
- [ACP support](acp.md) — the ACP method inventory: supported, conditional on wiring, or
  refused, plus what the adapter advertises and which client methods it calls.
- [MCP over ACP](mcp.md) — the stateless `mcp/message` binding: export and consent, what is
  served, the agent side, and the `subscriptions/listen` gap.
- [`mount_acp`](mount-acp.md) — every argument of the FastAPI mount, with isolated
  examples, and how `ModelResolver` and `ProviderModelResolver` differ.

## Run ACP over stdio

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app acp
```

## Run ACP WebSocket over FastAPI

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 python -m hrns.app serve --port 8000
```

- `WS /acp` — ACP over WebSocket
