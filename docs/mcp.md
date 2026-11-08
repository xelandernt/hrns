# MCP over ACP

This repository uses MCP in two directions:

- the local agent loads the `[mcp.servers.*]` entries from `config.toml`;
- a connected **client** can offer its own MCP servers to a remote ACP agent over the
  stateless `mcp/message` binding.

The first direction is ordinary MCP configuration and is documented in
[Configuration](configuration.md#mcp). This page covers the second direction: what the
binding serves, how export is approved, and what is deliberately still missing.

## The binding

MCP-over-ACP targets [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
and the ACP RFD
[MCP-over-ACP: Stateless MCP Requests over ACP](https://agentclientprotocol.com/rfds/mcp-over-acp).
It has no connection handshake:

- A provider registers an opaque `serverId` and declares it in `session/new`,
  `session/load`, or `session/resume` as `{"type": "acp", "name": ..., "serverId": ...}`.
- Every operation is one ACP `mcp/message` request carrying `{serverId, requestId, method,
  params}`. `requestId` is a fresh logical MCP request id; the outer JSON-RPC id is separate.
- There is no `mcp/connect`, no `mcp/disconnect`, and no MCP `initialize`. Each request
  carries its protocol version, client capabilities, and client info in `params._meta`.
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
| Consumer | The agent (`hrns-local` or another ACP agent) | Advertises `mcpCapabilities`, then turns each `type: "acp"` declaration into an `MCPToolset` backed by `AcpMcpClientTransport`. |

`hrns-local` advertises `mcpCapabilities.acp`, `http`, and `sse`. A client exports servers
only when the agent advertises `acp`; otherwise the declarations are omitted entirely.

The provider terminates both MCP sessions and translates at the method level. The upstream
and downstream sessions are independent, so the upstream may speak an older revision while
the ACP side stays on 2026-07-28. Raw MCP frames are never tunneled.

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

An unapproved server is never started, so no upstream process or connection is created for
a denied entry. A non-interactive client (`hrns run`) cannot ask; it exports only servers
with `always_allow = true` and denies everything else.

Consent is a client decision, not an agent approval policy. Per-call approval is out of
scope: once a server is exported, the agent can call all of its tools.

## Health

The terminal's `/mcp` command opens the MCP side panel. It lists every configured server
with its state (`running`, `failed`, `disabled`, `local only`, `not started`,
`waiting for approval`, `denied`, or `agent cannot use ACP`), transport, target, and
negotiated upstream revision.
`r` refreshes the view; it also refreshes after a connection or session change.

Failures are not silent. A server that fails to start, authenticate, or negotiate appears
in the panel with the reason, and the chat screen shows a transcript notice plus a status
hint. The session continues without that server.

## What is served

The provider mirrors the upstream's advertised capabilities and exposes:

| Method | Behaviour |
| --- | --- |
| `server/discover` | Reports the proxied server's capabilities |
| `ping` | Answered directly |
| `tools/list`, `tools/call` | Proxied; an upstream `isError` result stays an MCP result, not an ACP error |
| `resources/list`, `resources/read`, `resources/templates/list` | Proxied when the upstream has resources |
| `prompts/list`, `prompts/get` | Proxied when the upstream has prompts |
| `completion/complete` | Proxied when the upstream has completions |
| `notifications/progress` | Forwarded as request-scoped ACP notifications |
| `input_required` results | Passed through opaquely (see [MRTR](#mrtr-has-no-retry-policy)) |
| `$/cancel_request` | Cancels the handler; cleanup runs shielded and the outer request returns `-32800` |

Resources, prompts, and completions are advertised only when the upstream advertises them.
Upstream authentication stays on the client and never reaches the agent.

## The agent side

`hrns-local` passes `acp_mcp_toolsets` as its `session_config`. The helper turns every
declaration into a toolset:

- `McpServerStdio`, `HttpMcpServer`, and `SseMcpServer` become ordinary `MCPToolset`s;
- `AcpMcpServer` becomes an `AcpMcpClientTransport` over the session's caller and channel,
  wrapped in `MCPToolset(..., id=server_name)`.

Servers are connected during session setup, before the session id is returned, and disposed
when the session closes. `session/load` and, when the agent advertises it, `session/resume`
declare and reconnect the same way as `session/new`. `hrns-local` itself supports
`session/load` but refuses `session/resume`.

## Transports and authentication

| Upstream | Configuration | Notes |
| --- | --- | --- |
| stdio | `command`, `args`, `env`, `cwd` | The client owns the child process |
| Streamable HTTP | `url` | Headers and OAuth apply |
| SSE | `url` | Selected from the URL; headers and OAuth apply |

Headers and environment values support `{env:NAME}` expansion. OAuth tokens live in the
configured token store; `hrns mcp auth <name>` performs an interactive login and
`hrns mcp logout <name>` clears it. A runtime export never opens a browser — if a server
needs authentication, the failure is logged and that server is skipped for the session.

## Known gaps

### `subscriptions/listen` is not streamed

At MCP 2026-07-28 a client receives change notifications only by sending a
`subscriptions/listen` request whose **response is the stream**. There is no standing GET
stream, and there is no replay: a dropped stream is ended, and the client must re-listen and
refetch.

This provider serves the single-exchange path only: one `mcp/message` request produces one
`result` or `error`. It does not install a `subscriptions/listen` handler, so:

- the proxied server advertises `listChanged: false` for tools, prompts, and resources, and
  no resource subscription capability, because the MCP SDK derives those flags from whether
  `subscriptions/listen` is served;
- an agent that calls `subscriptions/listen` anyway receives an inner MCP
  `METHOD_NOT_FOUND` (`-32601`), not an outer ACP error;
- no upstream change notification is forwarded, so an agent must poll or refetch to notice
  a changed tool, prompt, or resource list.

Supporting it is a separate long-lived provider path, not a flag on the existing one. It
needs at minimum:

- a driver that keeps one operation alive after its acknowledgement instead of returning a
  single outcome;
- acknowledgement ordering: the `notifications/subscriptions/acknowledged` frame must be
  first, and the subscription must be registered before that frame is written so an event
  published during the write is buffered, not lost;
- correlation: every notification and the final result carry
  `_meta["io.modelcontextprotocol/subscriptionId"]` equal to the logical `requestId`;
- per-stream filtering of `toolsListChanged`, `promptsListChanged`,
  `resourcesListChanged`, and `resourceSubscriptions`;
- cancellation and disposal: ACP `$/cancel_request`, session close, and server disposal must
  end the stream without leaking its subscription slot; an abrupt end is not resumable.

The design notes and the remaining work breakdown are in
[`mcp-over-acp-analysis.md`](mcp-over-acp-analysis.md).

### MRTR has no retry policy

Results with `resultType: "input_required"` pass through to the agent unchanged. The
provider does not collect input responses, choose a fresh `requestId`, retry the request,
or decide when to stop. An agent that does not implement that policy simply sees an
`input_required` result it cannot complete.

### Legacy upstreams are best-effort

An upstream may speak a handshake-era revision (`initialize`, session state). `fastmcp`
negotiates with `mode="auto"`, so such a server still works for the methods the proxy
translates, and the negotiated revision is logged. This is not a second supported binding:

- capabilities are mirrored, so the agent never sees a resource, prompt, or completion the
  upstream does not have;
- reverse requests (`sampling/createMessage`, `elicitation/create`, `roots/list`) are not
  bridged and are not advertised. A server that needs one fails the call with the MCP SDK's
  `INVALID_REQUEST` instead of hanging.

### The agent is never a provider

`bind_provider` works on an `AgentSideConnection`, but ACP has no standard
agent-to-client declaration path. The supported flow is client-provided only.

## References

- [MCP-over-ACP RFD](https://agentclientprotocol.com/rfds/mcp-over-acp)
- [MCP 2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [ACP support](acp.md#known-gaps) — the method inventory and the adapter's own gaps
- [Configuration](configuration.md#mcp) — every `[mcp]` field
- [`mcp-over-acp-analysis.md`](mcp-over-acp-analysis.md) — implementation analysis and work
  breakdown
