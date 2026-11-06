# TODO

## ACP 1.x features our adapter does not implement yet

`src/hrns/app/acp/` now owns the complete ACP adapter and implements every
`acp.Agent` method. The remaining groups are explicitly unsupported in
`src/hrns/app/acp/adapter.py`: their capabilities are not advertised in
`initialize`, request methods raise `method_not_found`, and notification methods
are ignored. Replace each with a real implementation when the feature exists.

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

### 2. Needs a new feature: MCP-over-ACP

- [ ] `mcp/message` (request) and `notify_mcp` (notification): implement an
      MCP-over-ACP binding. This is a draft RFD targeting MCP 2026-07-28 only.
      The adapter does not register `AcpMcpServer`s today; supporting this means
      owning server registrations, request-id routing, cancellation, and
      request-scoped notifications. Do not advertise `mcpCapabilities.acp` until
      it works.

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
  them when the corresponding capability is advertised. We now advertise
  `providers` when a registry is configured, and none of the group 2/3 capabilities.
- The adapter's stated contract is "translate protocol events; do not define
  agent behavior" (README). Groups 2 and 3 cross that line, so their designs need
  to say where the behavior lives. Providers stayed on the right side of it: the
  registry and the resolver own the state and the routing.
- When implementing a group, replace the corresponding methods in
  `src/hrns/app/acp/adapter.py` and update README ("Limitations" and the
  session-store section) plus ARCHITECTURE.md if the adapter contract changes.
