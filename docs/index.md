# hrns

Define one Pydantic AI agent and expose it over ACP.

```python
from hrns.server import build_agent

agent = build_agent("anthropic:claude-sonnet-5-5")
```

## Reference

- [Configuration](configuration.md) — the one `config.toml` both the client and the local
  runtime read: locations, precedence, and every field with an example.
- [Capabilities](capabilities.md) — what this repository adds on top of Pydantic AI and
  the harness: the `Coder` and its `apply_changes` batch tool, `Todo`, `Skills`, and the
  approval policy.
- [Skills](skills.md) — Agent Skills: discovery, model and user invocation, the ACP wire
  format, configuration, and security.
- [ACP support](acp.md) — the ACP method inventory: supported, conditional on wiring, or
  refused, plus what the adapter advertises and which client methods it calls.
- [MCP over ACP](mcp.md) — the stateless `mcp/message` binding: export and consent, what is
  served, the agent side, and the `subscriptions/listen` gap.
- [`mount_acp`](mount-acp.md) — every argument of the FastAPI mount, with isolated
  examples, and how `ModelResolver` and `ProviderModelResolver` differ.

## Run ACP over stdio

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 hrns acp
```

## Run ACP WebSocket over FastAPI

```sh
HRNS_MODEL=anthropic:claude-sonnet-5-5 hrns serve --port 8000
```

- `WS /acp/agent` — ACP over WebSocket
