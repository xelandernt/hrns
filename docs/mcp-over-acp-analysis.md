# MCP-over-ACP: feasibility and implementation analysis

Status: working analysis, 2026-11-07. Not a decision record yet.

Implementation status: the stateless binding primitives live in
`src/hrns/acp_mcp/` (`McpOverAcpCaller`, `McpOverAcpProvider`,
`from_mcp_server`, `AcpMcpClientTransport`, and the local Python SDK route
shim). The client provider (`ClientMcpProvider`), the `[mcp].export` switch,
the agent-side `acp_mcp_toolsets` helper, and `hrns-local` session wiring are
in place. Long-lived subscriptions and an explicit MRTR retry policy remain;
legacy upstreams are best-effort (section 3.4).

Scope: the **stateless** MCP-over-ACP binding for **MCP 2026-07-28**, implemented
on both ends of this repository, with no `_hrns/*` protocol extensions. The
client is the MCP provider; a remote ACP agent is the consumer.

Two provider cases are in scope:

1. the client **launches** an MCP server process (for example
   `npx @playwright/mcp@latest`) and offers it to the agent;
2. the client **connects** to an existing MCP server (HTTP/SSE, or an
   already-running stdio process) and offers it to the agent.

Both are the same mechanism: an upstream MCP client session bridged to a
stateless MCP-over-ACP provider on the ACP connection.

## 1. What the protocol requires

The binding is defined by the ACP RFD
[MCP-over-ACP: Stateless MCP Requests over ACP](https://agentclientprotocol.com/rfds/mcp-over-acp)
and targets only
[MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28).

The important consequences:

- A provider registers an opaque `serverId` and declares it in `session/new`
  as `{"type": "acp", "name": ..., "serverId": ...}`.
- The agent sends `mcp/message` requests with
  `{serverId, requestId, method, params}`. `requestId` is a fresh logical MCP
  request id; the outer ACP JSON-RPC id is separate and may be renumbered by a
  proxy.
- There is no `mcp/connect` / `mcp/disconnect` exchange and no MCP
  `initialize` handshake. Every request carries its protocol version and client
  capabilities in inner `params._meta`.
- A successful ACP response carries exactly one inner outcome:
  `{"result": ...}` or `{"error": {code, message, data?}}`. An MCP protocol
  error is a successful ACP response; only binding failures use outer ACP
  errors.
- Provider notifications are ACP notifications on `mcp/message` with the same
  `serverId` / `requestId`. They are request-scoped.
- Cancellation is ACP's existing `$/cancel_request` on the outer request.
- MRTR replaces reverse requests: a provider can return
  `resultType: "input_required"` with `inputRequests` / `requestState`; the
  caller retries with a fresh `requestId`, `inputResponses`, and the unchanged
  state.
- Subscriptions are long-lived `subscriptions/listen` requests; their
  notifications carry `_meta["io.modelcontextprotocol/subscriptionId"]` equal
  to the logical `requestId`.

## 2. What already exists

### In this repository

- `src/hrns/app/acp/session.py` already carries
  `AcpMcpServer | HttpMcpServer | SseMcpServer | McpServerStdio` in
  `AcpSession.mcp_servers`.
- `src/hrns/app/acp/adapter.py` already passes `mcp_servers` to
  `session_config`, but nothing implements `session_config`; `hrns-local`,
  `build_agent`, and the FastAPI example pass none, so a non-empty
  `mcpServers` is rejected with `invalid_params`.
- `src/hrns/client/session.py` always sends `mcp_servers=[]`.
- `src/hrns/config/mcp.py` already models `[mcp.servers.<name>]` with `command`
  (stdio) or `url` (HTTP/SSE), OAuth, token storage, and FastMCP transport
  construction. Today only the `hrns mcp` CLI consumes it.
- `src/hrns/app/acp/toolsets.py` is the pattern for client-backed tools
  (`acp_filesystem`, `acp_terminal`): the adapter supplies the connection, the
  helper builds a toolset, and `session_config` decides whether to use it.

### In the installed dependencies

- `agent-client-protocol==1.0.0rc3` has `AcpMcpServer`,
  `McpCapabilities.acp`, `use_unstable_protocol=True`, and the **stateful**
  methods `mcp/connect`, `mcp/message(connectionId, ...)`, `mcp/disconnect`.
  It has no stateless types, no `MessageMcpResponse` carrier, and no `McpError`.
- `mcp==2.3.0` already supports the modern era:
  - `LATEST_PROTOCOL_VERSION == "2026-07-28"`, `LATEST_MODERN_VERSION == "2026-07-28"`
  - `mcp.server.lowlevel.Server.run()` is dual-era
  - `mcp.server.runner.serve_one()` is the single-exchange modern driver
  - `mcp.server._streamable_http_modern._SingleExchangeDispatchContext` is the
    one-request dispatch context (progress sink, no back-channel)
  - `mcp.Client` supports a custom `Transport` and `mode="auto"`
- `pydantic-ai-slim[mcp]` exposes `MCPToolset`, which accepts a
  `fastmcp.client.transports.base.ClientTransport` directly (or a
  `fastmcp.Client`).
- `fastmcp-slim[client]` provides `ClientTransport`, stdio/HTTP/SSE transports,
  OAuth, and token storage. Server support is **not** installed and is not
  needed: the provider can use `mcp.server` from the `mcp` package.

### In the Python SDK upstream

The Python SDK's **generated** schema is behind the spec's **draft v1
schema**. The draft already defines the stateless `serverId` / `requestId`
envelope, `McpRequestId`, the `MessageMcpResponse` carrier, and
`MessageMcpNotification`
([published draft v1 schema](https://agentclientprotocol.com/protocol/v1/draft/schema)).
The SDK's `schema/schema.json` still defines `ConnectMcpRequest` /
`connectionId` / `DisconnectMcpRequest`, all `UNSTABLE`. No released version or
`main` supports the stateless binding. The Rust SDK has already removed the
stateful prototype
([migration doc](https://github.com/agentclientprotocol/rust-sdk/blob/main/md/migration-stateless-mcp.md)),
and the TypeScript SDK ships the stateless binding
([core.ts](https://github.com/agentclientprotocol/typescript-sdk/blob/main/src/mcp/core.ts)).
This is the gap tracked in `ISSUE.md`.

## 3. Architecture

### 3.1 Agent side (consumer)

The agent must:

1. Advertise `mcpCapabilities.acp = true` when it can consume ACP-provided
   servers.
2. On `session/new` / `session/load`, for each `AcpMcpServer` declaration,
   build an MCP client transport bound to that `serverId`.
3. Expose it to the model as a toolset.

The clean integration is a `ClientTransport` implementation:

```
AcpMcpClientTransport(ClientTransport)
    connect_session() -> ClientSession
        in-memory client streams
        pump task:
            outgoing SessionMessage -> ACP request:
                mcp/message {serverId, requestId, method, params}
                response carrier -> SessionMessage response
            inbound ACP notification (same serverId/requestId)
                -> SessionMessage notification
        close(): cancel outstanding operations
```

`pydantic_ai.mcp.MCPToolset` accepts the transport directly, so no custom
toolset is required:

```python
MCPToolset(AcpMcpClientTransport(client, server_id), id=server_name)
```

The adapter must own the inbound notification routing because `notify_mcp`
arrives on the adapter's connection. A connection-scoped registry keyed by
`(serverId, requestId)` is enough. The toolset helper (`acp_mcp_toolsets`, by
analogy with `acp_filesystem` / `acp_terminal`) stays outside the adapter; the
adapter only provides the connection and the notification hook. This keeps the
adapter's "translate protocol events; do not define agent behavior" contract.

Lifecycle: connect during session setup, before returning the session id, as
the RFD requires the provider to be ready when it publishes the declaration.
Disconnect on session close. A `session/load` replays the transcript and then
reconnects the servers.

Tool naming: use the server name as `MCPToolset(id=...)`; if the model-facing
names collide, prefix with the server name. Pydantic AI already namespaces
toolsets by id, but verify against the current version.

### 3.2 Client side (provider)

`HrnsAcpClient` is the ACP client object. It must implement the provider
direction of `mcp/message` and own a registry of offerings:

```
AcpMcpProvider (connection-scoped)
    offerings: dict[server_id, Offering]
    handle_mcp_message(params) -> MessageMcpResponse carrier
    handle_mcp_notification(params) -> None
    close()
```

`Offering` holds:

- the user-facing name (`project-tools`, `playwright`, ...);
- the upstream MCP client (`fastmcp.Client` built from
  `hrns.config.mcp.build_transport`);
- the downstream server (`mcp.server.lowlevel.Server` or `MCPServer`) whose
  handlers proxy to the upstream client;
- the process/connection lifecycle.

Upstream:

- `command` → `fastmcp.client.transports.StdioTransport` (the client starts and
  owns the process; this is feature 1).
- `url` → `StreamableHttpTransport` / `SSETransport` through the existing
  `build_transport`, preserving headers, OAuth, and token storage (feature 2).
- Reuse `McpServerSettings`, `expand_env_map`, and the token store; no second
  config schema.

Downstream:

- Build a low-level `Server` with handlers for `tools/list`, `tools/call`,
  `ping`, and optionally resources/prompts/completions.
- For each ACP request, use `mcp.server.runner.serve_one` with a
  one-exchange dispatch context adapted from
  `_SingleExchangeDispatchContext`. Replace its SSE sink with ACP
  `send_notification("mcp/message", {serverId, requestId, method, params})`.
- Return `{"result": result}` or `{"error": {"code", "message", "data"}}`.
  Map handler exceptions with `modern_error_data`; transport failures become
  outer `-33002`.
- Honour `$/cancel_request`: the handler task is cancelled, cleanup runs
  shielded, notifications stop, and the outer request returns `-32800`.

Registration and declaration:

- Generate an opaque `serverId` per offering for the ACP connection lifetime,
  e.g. `hrns:<name>:<8 hex>`. Do not rebind it.
- Declare `AcpMcpServer(name=..., server_id=...)` in `session/new` and
  `session/load` only when the agent advertises `mcpCapabilities.acp`.
- Keep the offering alive across sessions; a later session may reuse the same
  registration, as the RFD allows.

### 3.3 Why the proxy cannot tunnel raw MCP frames

The upstream and downstream sessions are independent MCP connections, often
different protocol eras:

- `npx @playwright/mcp` today speaks a legacy handshake-era MCP revision
  (`initialize`, session state).
- The downstream binding is stateless MCP 2026-07-28: no `initialize`,
  per-request `_meta`, MRTR.

Passing raw JSON-RPC through would send 2026-07-28 envelopes to a legacy
server, or legacy `initialize` to the agent. The provider must terminate both
sessions and translate at the method level:

- `tools/list` → upstream `list_tools()` → descriptors passed through;
- `tools/call` → upstream `call_tool()` → result or `McpError`;
- progress callbacks → downstream `ctx.progress` → ACP notification;
- upstream `isError` stays an MCP result, not an ACP error;
- upstream authentication stays on the client and never reaches the agent.

The `mcp` Python SDK's dual-era support makes this tractable: the upstream
client can negotiate legacy, while the downstream provider serves modern via
`serve_one`.

### 3.4 Legacy upstream policy

Supporting legacy upstreams fully is not a translation problem, it is a new
feature:

- A handshake-era server's reverse requests (`sampling/createMessage`,
  `elicitation/create`, `roots/list`) happen inside a live `call_tool()` await.
  The downstream binding has no back-channel: it can only return
  `input_required` and wait for a retry with a fresh `requestId`. Resuming would
  mean either re-issuing the upstream call (repeating side effects) or
  answering locally (a client-side model, client-side UI, or client-side
  filesystem), which is a product and trust decision, not protocol work.
- Legacy change notifications (`list_changed`, logging) map onto modern
  `subscriptions/listen`, which the provider does not serve yet.

The chosen policy is **best-effort, not supported**:

- `build_client` uses `mode="auto"`, so a legacy server still negotiates and
  works for the finite methods the proxy translates.
- `_proxy_server` mirrors the upstream's `server_capabilities`, so
  `server/discover` never advertises resources, prompts, or completions the
  upstream does not have.
- The negotiated revision is logged; a non-`2026-07-28` server logs a warning.
- Reverse requests are not advertised, so a legacy server that needs one gets
  the MCP SDK's `INVALID_REQUEST` ("Sampling not supported", "Elicitation not
  supported", "List roots not supported") instead of a hang or a bridge.
- `hrns mcp` keeps `mode="auto"` so a legacy server can still be inspected.

## 4. The Python SDK gap and the local workaround

### Option A: wait for upstream

Not acceptable if the feature is needed now, and the issue may take time to
land. The old prototype is also a dead end.

### Option B: local compatibility shim (recommended short term)

The Python SDK's connection layer is small enough to extend without forking:

- `MessageRouter` is public (`acp.router.MessageRouter`) and
  `add_route(Route(...))` replaces a method's handler. Requests and
  notifications are separate route tables, so `mcp/message` can be both.
- `Connection.send_request(method, params)` and
  `send_notification(method, params)` are generic; they do not require a typed
  wrapper.
- After `ClientSideConnection` / `AgentSideConnection` is constructed, the
  handler is at `connection._conn._handler` (private). Replace the
  `mcp/message` request and notification routes with stateless handlers, and
  send stateless messages through `_conn.send_request` /
  `send_notification`.

This is a **protocol-conformant** workaround: the wire bytes are the RFD's,
not an extension. It only bypasses the SDK's outdated pydantic model for one
method.

Risks:

- private attributes (`_conn`, `_handler`) can change between SDK releases;
- the SDK's typed `mcp_message` helpers become unusable for this method;
- error mapping and cancellation must be reimplemented at the shim boundary.

Mitigation: isolate everything in one module (for example
`src/hrns/app/acp/mcp_binding.py`, plus the client-side counterpart), pin
`agent-client-protocol`, and add a compatibility test that asserts the router
shape at startup. Remove the shim when the upstream issue lands.

### Option C: fork or vendor the SDK

Works, but creates a maintenance burden disproportionate to the size of the
change. Not recommended unless the upstream issue stalls.

### Option D: contribute the change upstream

The right long-term answer, and the subject of `ISSUE.md`. The change is
moderate: schema types, interface signatures, route registration, carrier
validation, and tests. The SDK already has the pieces (router, generic
connection, cancellation); the work is mostly semantics.

## 5. Work breakdown and rough effort

Estimates are person-days for one engineer who knows this repository. They are
for the MVP (tools only), not the full MCP feature surface.

| Component | Effort | Notes |
| --- | --- | --- |
| Stateless wire types + router shim (both ends) | 2–3 | Schema models, route replacement, carrier validation, ID tracking |
| Agent consumer transport + `MCPToolset` | 2–3 | `ClientTransport`, memory streams, notification routing, lifecycle |
| Client provider registry + `serve_one` dispatch | 2–3 | Envelope validation, active-ID bookkeeping, error mapping |
| Upstream proxy: spawn stdio + HTTP/SSE, tools | 3–5 | Reuse `config.mcp`, FastMCP clients, low-level server handlers |
| Progress notifications + cancellation | 2–3 | Sink-to-ACP, `$/cancel_request`, shielded cleanup |
| Config/TUI export + session declarations | 1–2 | Opt-in flag, `serverId`, `session/new` payload, status |
| Tests (unit, in-process echo, playwright smoke) | 3–5 | Most of the confidence comes from here |
| **MVP total** | **~15–24** | About 3–5 weeks, including review cycles |

Beyond the MVP:

| Feature | Additional effort | Risk |
| --- | --- | --- |
| Resources, resource templates, prompts, completion | 3–5 | Low–medium |
| Subscriptions (`subscriptions/listen`) | 3–5 | High: long-lived operation, ack ordering, cancellation |
| MRTR / `input_required` | 3–5 | High: opaque state, retries, policy |
| Full conformance and stress tests | 5–10 | Medium |

A realistic first milestone is **two weeks**: one in-process echo MCP server,
agent-side consumption, and the stateless envelope working end to end with
tests. The playwright-mcp stdio proxy is the next milestone.

## 6. Main risks

1. **Private SDK shim.** The router replacement relies on
   `connection._conn._handler`. A minor SDK refactor breaks it. Mitigate with a
   single module, a startup assertion, and an upstream PR.
2. **Legacy ↔ modern bridge.** Playwright and many current servers are
   legacy-era. The proxy terminates both sessions and translates at the method
   level; raw tunneling will not work. Finite methods are best-effort, but
   reverse requests are not bridged (section 3.4), so a legacy server that
   needs sampling, elicitation, or roots fails at the call.
3. **Stateless server lifecycle.** Per-request serving is straightforward;
   subscriptions and long-lived operations are not. Do not advertise
   subscription support until it is tested.
4. **Security and consent.** Exporting every configured MCP server to any
   connected agent gives that agent the client's local tools and credentials.
   Default to opt-in export (`acp = true` per server), and decide whether
   agent-initiated calls need a client-side approval prompt.
5. **Tool naming.** Multiple servers can expose the same tool name. Decide
   between prefixing and rejecting collisions before the model sees them.
6. **Protocol churn.** The binding is a draft RFD. Pin the MCP protocol
   version and keep the wire shim isolated so it can follow the RFD.
7. **Error-domain mistakes.** Mixing inner MCP codes with outer ACP codes is
   the most likely conformance bug (for example, treating an inner `-32000` as
   ACP auth-required). Test colliding codes explicitly.

## 7. Recommended sequence

1. File the upstream issue (`ISSUE.md`); optionally open a draft PR for the
   schema and router changes.
2. Implement the stateless models and the local shim in one module, with unit
   tests against a fake ACP connection.
3. Implement the agent consumer transport and prove it against an in-process
   echo MCP server.
4. Implement the client provider for a spawned stdio upstream
   (playwright-mcp) and run the first real end-to-end turn.
5. Add HTTP/SSE upstreams and reuse the existing OAuth/token store.
6. Add progress notifications and cancellation.
7. Add the opt-in export setting and the `session/new` declaration.
8. Add resources/prompts, then subscriptions, then MRTR, in that order.
9. Remove the shim when the Python SDK ships the stateless binding.

## 8. Open decisions

- **Export policy:** all enabled `[mcp]` servers, or only servers with an
  explicit `acp = true`? Recommendation: opt-in, default off.
- **Consent:** should the client prompt before executing an agent-initiated
  tool call, especially for file/shell/browser tools? The ACP permission flow
  is agent-to-client; this would be a new client-side policy.
- **Session scope:** declare every offering in every session, or let the user
  select which servers each session sees?
- **Feature surface:** tools only for the MVP, or resources/prompts too?
- **Naming:** prefix model-facing tool names with the server name, or rely on
  `MCPToolset(id=...)`?
- **Fallback:** when the agent does not advertise `mcpCapabilities.acp`,
  should the client declare reachable stdio/HTTP servers directly, or remain
  silent?
- **Protocol version:** the downstream binding is pinned to `2026-07-28` and
  rejects other versions with the inner `-32022` carrier as the RFD requires.
  The upstream connection stays on `mode="auto"` and is best-effort, so a
  legacy server is usable without becoming a supported binding (section 3.4).
