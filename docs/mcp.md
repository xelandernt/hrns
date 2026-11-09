# MCP over ACP

This repository uses MCP in two directions:

- the built-in agent loads the `[mcp.servers.*]` entries from `config.toml`;
- a connected **client** can offer its own MCP servers to a remote ACP agent over the
  stateless `mcp/message` binding.

The first direction is ordinary MCP configuration and is documented in
[Configuration](configuration.md#mcp). This page covers the second direction: what the
binding, export consent, and request handling.

## The binding

MCP-over-ACP targets [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
and the ACP RFD
[MCP-over-ACP: Stateless MCP Requests over ACP](https://agentclientprotocol.com/rfds/mcp-over-acp).
Each operation is a self-contained exchange:

- A provider registers an opaque `serverId` and declares it in `session/new`,
  `session/load`, or `session/resume` as `{"type": "acp", "name": ..., "serverId": ...}`.
- Every operation is one ACP `mcp/message` request carrying `{serverId, requestId, method,
  params}`. `requestId` is a fresh logical MCP request id; the outer JSON-RPC id is separate.
- Each request carries its protocol version, client capabilities, and client info
  in `params._meta`.
- A successful ACP response carries exactly one inner outcome: `{"result": ...}` or
  `{"error": {code, message, data?}}`. An MCP protocol error is a successful ACP response;
  only a failure of the binding itself uses an outer ACP error.
- Provider notifications are ACP notifications on `mcp/message` with the same
  `serverId` / `requestId`. They are request-scoped.
- Cancellation is ACP's existing `$/cancel_request` on the outer request.

Inner MCP error codes and outer ACP codes are different domains. The binding reserves these
outer codes:

| Code | Meaning |
| --- | --- |
| `-32602` | Malformed `mcp/message` envelope |
| `-32800` | The outer request was cancelled |
| `-33000` | Active-request limit reached |
| `-33001` | Unknown or disposed `serverId` |
| `-33002` | The provider handler or transport failed |

## Roles

| Side | Owner | What it does |
| --- | --- | --- |
| Provider | The client (`hrns` CLI, TUI, or `hrns run`) | Launches or connects to a configured MCP server and serves it to the agent over the ACP connection. |
| Consumer | The agent (`hrns agent` or another ACP agent) | Advertises `mcpCapabilities`, then turns each `type: "acp"` declaration into an `MCPToolset` backed by `AcpMcpClientTransport`. |

`hrns agent` advertises `mcpCapabilities.acp`, `http`, and `sse`. A client exports servers
only when the agent advertises `acp`; otherwise the declarations are omitted entirely.

The provider terminates both MCP sessions and translates at the method level. The upstream
and downstream sessions are independent, so the upstream may speak an older revision while
the ACP side stays on 2026-07-28. The proxy translates individual methods between them.

## Configuring an export

Export is opt-in per server. The server must also be `enabled = true`:

```toml
[mcp.servers.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest"]
export = true
```

For an HTTP server, set `url` instead of `command`; headers, environment expansion, OAuth,
and token storage behave exactly as they do for local use.

The CLI writes the same fields:

```sh
hrns mcp add playwright --export -- npx -y @playwright/mcp@latest
hrns mcp add docs --url https://example.com/mcp --export --always-allow
hrns mcp get playwright
```

`hrns mcp list` shows the export state in its `ACP` column: `-` when export is off,
`export` when the client asks at session start, and `always` when the grant is pre-approved.
`--always-allow` is only valid together with `--export`.

### Consent

At session start the client asks whether the agent may use each exported server:

- **allow once** — declare it for this session only;
- **allow for this connection** — declare it for every session until the connection closes;
- **always allow** — declare it now and persist `always_allow = true` in `config.toml`;
- **deny** — leave it out of this session's declarations.

Approval gates upstream startup and session declarations. A non-interactive client
(`hrns run`) exports servers with `always_allow = true`.

The client owns export consent. Once exported, the server exposes its tools to the agent.

## Health

The terminal's `/mcp` command opens the MCP side panel. It lists every configured server
with its state (`running`, `failed`, `disabled`, `local only`, `not started`,
`waiting for approval`, `denied`, or `agent cannot use ACP`), transport, target, and
negotiated upstream revision.
`r` refreshes the view; it also refreshes after a connection or session change.

A server that fails to start, authenticate, or negotiate appears
in the panel with the reason, and the chat screen shows a transcript notice plus a status
hint. The session continues with the servers that started successfully.

## What is served

The provider mirrors the upstream's advertised capabilities and exposes:

| Method | Behaviour |
| --- | --- |
| `server/discover` | Reports the proxied server's capabilities |
| `ping` | Answered directly |
| `tools/list`, `tools/call` | Proxied; upstream `isError` results are preserved |
| `resources/list`, `resources/read`, `resources/templates/list` | Proxied when the upstream has resources |
| `prompts/list`, `prompts/get` | Proxied when the upstream has prompts |
| `completion/complete` | Proxied when the upstream has completions |
| `notifications/progress` | Forwarded as request-scoped ACP notifications |
| `input_required` results | Passed through opaquely (see [MRTR](#input-required-results)) |
| `$/cancel_request` | Cancels the handler; cleanup runs shielded and the outer request returns `-32800` |

Resources, prompts, and completions are advertised only when the upstream advertises them.
The client owns upstream authentication.

## The agent side

`hrns agent` uses `acp_mcp_toolsets` in its `attachment_config`, returning
`AcpAttachmentConfig`. The helper turns every
declaration into a toolset:

- `McpServerStdio`, `HttpMcpServer`, and `SseMcpServer` become ordinary `MCPToolset`s;
- `AcpMcpServer` becomes an `AcpMcpClientTransport` over the session's caller and channel,
  wrapped in `MCPToolset(..., id=server_name)`.

Servers are connected during session setup, before the session id is returned, and disposed
when the session closes. `session/load` and, when the agent advertises it, `session/resume`
declare and reconnect the same way as `session/new`. `hrns agent` itself supports
`session/load` for restoration.

## Transports and authentication

| Upstream | Configuration | Notes |
| --- | --- | --- |
| stdio | `command`, `args`, `env`, `cwd` | The client owns the child process |
| Streamable HTTP | `url` | Headers and OAuth apply |
| SSE | `url` | Selected from the URL; headers and OAuth apply |

Headers and environment values support `{env:NAME}` expansion. OAuth tokens live in the
configured token store; `hrns mcp auth <name>` performs an interactive login and
`hrns mcp logout <name>` clears it. Complete interactive authentication before exporting.
Authentication failures are logged and the affected server is skipped for that session.

## Request scope

### Catalog updates

The provider serves one result or error per `mcp/message`. Tool, prompt, and resource
catalogs advertise `listChanged: false`. Consumers can poll or refetch to observe changes.
A `subscriptions/listen` request receives the inner MCP `METHOD_NOT_FOUND` (`-32601`).

Long-lived subscriptions require an additional provider path with acknowledgement
ordering, subscription correlation, filtering, and cancellation. The implementation
notes are in [MCP binding internals](mcp-over-acp-analysis.md).

### Input-required results

Results with `resultType: "input_required"` pass through to the consumer. The consumer
owns input collection, retry requests with fresh `requestId` values, and a stopping policy.

### Upstream revisions

Upstream negotiation uses automatic protocol selection. Handshake-era servers work on a
best-effort basis for the finite methods the proxy translates. The proxy mirrors upstream
capabilities and logs the negotiated revision. A reverse request for sampling, elicitation,
or roots receives `INVALID_REQUEST`.

### Provider declarations

The supported declaration flow is client to agent: clients offer servers in session
requests, and the agent consumes them through `AcpMcpClientTransport`.

## References

- [MCP-over-ACP RFD](https://agentclientprotocol.com/rfds/mcp-over-acp)
- [MCP 2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [ACP support](acp.md#runtime-scope) — method inventory and runtime scope
- [Configuration](configuration.md#mcp) — every `[mcp]` field
- [`mcp-over-acp-analysis.md`](mcp-over-acp-analysis.md) — binding internals and subscription requirements
