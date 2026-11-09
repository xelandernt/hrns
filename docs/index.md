# hrns

Define one Pydantic AI agent and expose it over ACP.

```python
from hrns.server import build_agent

agent = build_agent("anthropic:claude-sonnet-5-5")
```

## Reference

- [Configuration](configuration.md) — the one `config.toml` both the client and the local
  runtime read: locations, precedence, and every field with an example.
- [Capabilities](capabilities.md) — coding tools and agent behavior: `Coder`,
  `apply_changes`, `Todo`, `Skills`, user questions, and approval handling.
- [Skills](skills.md) — Agent Skills: discovery, model and user invocation, the ACP wire
  format, configuration, and security.
- [ACP support](acp.md) — the ACP method inventory: supported, conditional on wiring, or
  refused, plus what the adapter advertises and which client methods it calls.
- [MCP over ACP](mcp.md) — the stateless `mcp/message` binding: export and consent, what is
  served, the agent side, and catalog polling and consumer retries.
- [Agent servers](mount-acp.md) — registration, hosting, sessions, authentication,
  stores, and model/provider routing.
- [AG-UI observation](ag_ui.md) — read-only snapshots and live event streams.

## Run ACP over stdio

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 hrns acp
```

## Run ACP WebSocket over FastAPI

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 hrns serve --port 8000
```

- `WS /acp/agent` — ACP over WebSocket
