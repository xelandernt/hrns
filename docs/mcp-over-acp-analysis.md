# MCP binding internals

The stateless MCP-over-ACP binding serves client-provided tools, resources, prompts,
completions, progress, and input-required results. For configuration and examples, see
[MCP over ACP](mcp.md). Architecture decisions are recorded in `ARCHITECTURE.md`.

## Components

| Component | Module | Responsibility |
| --- | --- | --- |
| `McpOverAcpProvider` | `hrns.shared.acp_mcp.core` | Register server IDs, validate envelopes, dispatch requests, track active IDs, and map errors |
| `McpOverAcpCaller` | `hrns.shared.acp_mcp.core` | Send requests with protocol metadata, correlate notifications, and forward cancellation |
| `from_mcp_server` | `hrns.shared.acp_mcp.server` | Adapt server handlers to one exchange through `serve_one` |
| `AcpMcpClientTransport` | `hrns.shared.acp_mcp.transport` | Expose the binding as an MCP client transport |
| Route binding | `hrns.shared.acp_mcp.router` | Install request and notification handlers on the ACP connection |
| `ClientMcpProvider` | `hrns.client.mcp_provider` | Obtain consent, start upstream servers, publish declarations, and own cleanup |
| `acp_mcp_toolsets` | `hrns.server.acp.mcp` | Build attachment toolsets from client declarations |

## Request flow

1. The client obtains consent for an enabled server with `export = true`.
2. The client starts its upstream connection, registers an opaque `serverId`, and includes
   the declaration in the session request.
3. The agent creates an attachment toolset backed by `AcpMcpClientTransport`.
4. Each operation sends `mcp/message` with `serverId`, a fresh logical `requestId`, and
   per-request protocol metadata. The outer ACP JSON-RPC ID identifies the transport request.
5. The provider translates the method to its upstream session and returns an inner
   `result` or `error`. Progress notifications carry the same logical request ID.
6. Cancellation uses ACP `$/cancel_request`; handler cleanup runs under shielding.

Upstream and downstream sessions negotiate independently. The proxy translates methods,
preserves tool-error results, mirrors upstream capabilities, and keeps upstream credentials
on the client. MCP protocol errors use the inner carrier; binding failures use outer ACP
codes. See the [error table](mcp.md#the-binding).

## Connection routing

The route binding installs stateless `mcp/message` request and notification handlers on
both connection directions. It uses the connection's router and raw request/notification
send methods. Access to private connection attributes is isolated in `router.py`; review
that boundary when upgrading the ACP dependency.

A connection owns its server registrations and active request IDs. Attachment cleanup
releases those resources. Agent tool dispatch validates attachment generation to keep
calls associated with the client whose tools were advertised.

## Subscription work

The current provider serves finite exchanges. Implementing `subscriptions/listen` requires:

| Concern | Required behavior |
| --- | --- |
| Serving | Keep a provider operation alive after acknowledgement |
| Ordering | Register the subscription before writing `notifications/subscriptions/acknowledged`; buffer events during that write |
| Correlation | Set `_meta["io.modelcontextprotocol/subscriptionId"]` to the logical request ID on each notification and final result |
| Filtering | Respect tools, prompts, resources, and resource-subscription filters |
| Cancellation | Release the subscription on cancellation, session close, or server disposal |
| Recovery | Re-listen and refetch after a dropped stream |

Input-required results already pass through. A consumer retry policy must collect input,
choose a fresh request ID, preserve opaque state, and define when retries stop.
Remaining work is tracked in `TODO.md`.

## References

- [MCP-over-ACP RFD](https://agentclientprotocol.com/rfds/mcp-over-acp)
- [MCP 2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28)
