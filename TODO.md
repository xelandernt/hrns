# TODO

## ACP 1.x features our adapter does not implement yet

`src/hrns/app/acp/` now owns the complete ACP adapter and implements every
`acp.Agent` method. Groups 1–3 record what remains open: group 2's core binding is
implemented in `src/hrns/acp_mcp/`, and the remaining gaps are listed there; group 3
(NES) is still explicitly unsupported in `src/hrns/app/acp/adapter.py`, so its
capabilities are not advertised and its methods raise `method_not_found`.

### Already implemented

- [x] `session/list` and `session/delete`, backed by `ListableSessionStore` /
      `DeletableSessionStore`.
- [x] `providers/list`, `providers/set`, and `providers/disable`, plus the `model`
      session config option, all backed by one `ModelResolver` and advertised only when one
      is configured; `hrns-local` supplies a resolver seeded from its provider entries.
- [x] `logout`: the adapter configures no authentication, so it is a no-op
      returning `schema.LogoutResponse()`.
- [x] `document/didOpen`, `didChange`, `didClose`, `didSave`, `didFocus`:
      per-session open-document state (URI, language id, version, text, saved
      flag, focus position, visible range), including applying ranged content
      changes. Nothing consumes it yet; it is the substrate for NES below.

### 1. Providers: done, with one open question

`providers/*` is implemented (see "Already implemented"). What is left is a judgement
call, not code:

- [ ] `hrns-local` marks the entry this deployment starts with as `required`, because
      disabling it would leave the default session without a model. Revisit once a client
      needs to disable that entry, or once sessions can fall back to another model.

Reference: <https://agentclientprotocol.com/rfds/custom-llm-endpoint>

### 2. MCP-over-ACP: implemented, with remaining gaps and open decisions

- [x] `mcp/message` / `notify_mcp`: stateless binding for MCP 2026-07-28 in
      `src/hrns/acp_mcp/` (`McpOverAcpCaller`, `McpOverAcpProvider`,
      `from_mcp_server`, `AcpMcpClientTransport`, and the SDK route shim).
- [x] Client provider: `ClientMcpProvider` launches a stdio command or connects to
      HTTP/SSE, proxies tools/resources/prompts/completions/progress, and declares
      `type: "acp"` servers in `session/new` / `load` / `resume`.
- [x] Agent consumer: `acp_mcp_toolsets` turns `type: "acp"` declarations into
      `MCPToolset`s and standard stdio/HTTP/SSE declarations into direct toolsets;
      `hrns-local` advertises `mcpCapabilities.acp/http/sse`.
- [x] Config/CLI: `[mcp.servers.<name>].export` and `hrns mcp add --export`.

Remaining:

- [ ] `subscriptions/listen` streaming. The single-exchange `serve_one` path has no
      long-lived operation; subscriptions need a separate provider path,
      acknowledgement ordering, correlation, and cancellation.
- [ ] Explicit MRTR retry policy. `input_required` results pass through opaquely,
      but there is no policy for collecting input responses, retrying with a fresh
      `requestId`, or deciding when to stop.
- [ ] Decide whether the agent should ever be a provider. `bind_provider` works on
      an `AgentSideConnection`, but ACP has no standard agent-to-client declaration
      path; the current flow is client-provided only.

Open decisions:

- [ ] Client-side consent for exported tools. `export = true` is the configuration
      gate, and the agent's permission policy drives per-call approval; an agent
      running with approval disabled can call the client's tools without a prompt.
      Decide whether the provider needs its own per-call policy.
- [ ] Surface provider startup or authentication failures in the TUI status instead
      of only logging and skipping the server.

Design notes and the remaining work breakdown are in
`docs/mcp-over-acp-analysis.md`.

Reference: <https://agentclientprotocol.com/rfds/mcp-over-acp>

### 3. Needs a new feature: NES (next edit suggestions)

- [ ] `nes/start`, `nes/suggest`, `nes/close`, `nes/accept`, `nes/reject`:
      implement an inline-edit-suggestion service. This needs a completion model,
      a prompt strategy for edit/jump/rename/search-and-replace suggestions,
      suggestion ids, and accept/reject bookkeeping. The document state tracked
      above is the input it needs. There is no completion backend today.

Reference schema types: `StartNesRequest`, `SuggestNesRequest`, `CloseNesRequest`,
`AcceptNesNotification`, `RejectNesNotification`, `NesEditSuggestionVariant`.

## Notes

- These methods are unstable ACP features except `logout`; clients only call
  them when the corresponding capability is advertised. We advertise `providers`
  when a registry is configured, and `mcpCapabilities` (`acp`, `http`, `sse`) when
  `hrns-local` supplies them.
- The adapter's stated contract is "translate protocol events; do not define
  agent behavior" (README). Group 3 still crosses that line, so its design needs to
  say where the behavior lives. Group 2 does not: the client provider and the
  agent toolsets own the MCP behavior, and the adapter installs only the caller
  route.
- When implementing a group, replace the corresponding methods in
  `src/hrns/app/acp/adapter.py` and update README ("Limitations" and the
  session-store section) plus ARCHITECTURE.md if the adapter contract changes.
