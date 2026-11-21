# MCP over ACP

Configure MCP servers on the [client](configuration.md#mcp). The client starts or
connects to each approved server and offers it to a local or remote ACP agent
through the stateless MCP-over-ACP binding.

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

The native Ratatui client hosts the provider in Rust. It connects to MCP 2026-07-28
servers over stdio or Streamable HTTP, then declares them through ACP. The
headless runner hosts its provider in the Python client. Browser sessions have
no client-hosted provider.

## Configure a server

Add a stdio or HTTP server to the client configuration:

```toml
[mcp.servers.project-tools]
command = "project-tools"
```

The CLI writes the same fields:

```sh
hrns mcp add project-tools -- project-tools
hrns mcp add docs --url https://example.com/mcp --always-allow
hrns mcp get project-tools
```

`hrns mcp list` shows whether each server asks for consent or is pre-approved.

### Consent

At session start the client asks whether the agent may use each exported server:

- **allow once** — declare it for this session only;
- **allow for this connection** — declare it for every session until the connection closes;
- **always allow** — declare it now and persist `always_allow = true` in `config.toml`;
- **deny** — leave it out of this session's declarations.

Approval gates upstream startup and session declarations. A non-interactive client
(`hrns run`) exports servers with `always_allow = true`.

The client owns export consent. Once exported, the server exposes its tools to the agent.
`include_tools` and `exclude_tools` also apply to the exported view: a hidden tool is
absent from `tools/list`, and calling it returns an MCP error.

## Health

The terminal's `/mcp` command prints the MCP health table, and the `Ctrl-B` sidebar lists
each configured server's state, transport, target, and negotiated upstream
revision. Health refreshes when a connection or session starts. If a server
fails, the session continues with the servers that started successfully.

## What is served

The provider mirrors the upstream's advertised capabilities and exposes:

| Method | Behaviour |
| --- | --- |
| `server/discover` | Reports the proxied server's capabilities |
| `ping` | Checks that the upstream is reachable |
| `tools/list`, `tools/call` | Proxied; upstream `isError` results are preserved |
| `resources/list`, `resources/read`, `resources/templates/list` | Proxied when the upstream has resources |
| `prompts/list`, `prompts/get` | Proxied when the upstream has prompts |
| `completion/complete` | Proxied when the upstream has completions |
| `notifications/progress` | Forwarded as request-scoped ACP notifications |
| `input_required` results | Passed through opaquely (see [MRTR](#input-required-results)) |
| `$/cancel_request` | Cancels the operation and returns `-32800` |

Resources, prompts, and completions are advertised only when the upstream advertises them.
The client owns upstream authentication.

## The agent side

`hrns agent` uses `acp_mcp_toolsets` in its `attachment_config`, returning
`AcpAttachmentConfig`. The helper turns every
declaration into a toolset:

- `McpServerStdio`, `HttpMcpServer`, and `SseMcpServer` become ordinary `MCPToolset`s;
- `AcpMcpServer` becomes an `AcpMcpClientTransport` over the session's caller and channel,
  wrapped in `MCPToolset(..., id=server_name)`.

The client registers servers before session setup and releases their upstream connections
when the ACP connection closes. `session/load` and, when the agent advertises it, `session/resume`
declare and reconnect the same way as `session/new`. `hrns agent` itself supports
`session/load` for restoration.

## Transports and authentication

| Upstream | Configuration | Notes |
| --- | --- | --- |
| stdio | `command`, `args`, `env`, `cwd` | The client owns the child process |
| Streamable HTTP | `url`, `headers` | Pass credentials in client-side headers |
| Legacy HTTP+SSE | `url` | Supported by the Python headless client |

Headers and environment values support `{env:NAME}` expansion. The Python headless
client keeps OAuth tokens in the configured token store. Run
`hrns mcp auth <name>` to perform an interactive login and
`hrns mcp logout <name>` to clear the tokens. Complete interactive authentication before exporting.
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
