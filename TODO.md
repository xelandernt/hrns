# TODO

## ACP 1.x features our adapter does not implement yet

`src/hrns/app/acp/` now owns the complete ACP adapter and implements every
`acp.Agent` method. The methods below are explicitly unsupported in
`src/hrns/app/acp/adapter.py`: their capabilities are not advertised in
`initialize`, request methods raise `method_not_found`, and notification methods
are ignored. Replace each with a real implementation when the feature exists.

### Already implemented

- [x] `session/list` and `session/delete`, backed by `ListableSessionStore` /
      `DeletableSessionStore`.
- [x] `logout`: the adapter configures no authentication, so it is a no-op
      returning `schema.LogoutResponse()`.
- [x] `document/didOpen`, `didChange`, `didClose`, `didSave`, `didFocus`:
      per-session open-document state (URI, language id, version, text, saved
      flag, focus position, visible range), including applying ranged content
      changes. Nothing consumes it yet; it is the substrate for NES below.

### 1. Needs a design decision: `providers/*`

- [ ] `providers/list`: report configured providers, supported API types,
      `required`, and non-secret current routing (`apiType`, `baseUrl`).
- [ ] `providers/set`: replace one provider's `apiType`, `baseUrl`, and headers;
      validate the provider id and API type.
- [ ] `providers/disable`: clear one provider's current config; reject disabling
      a `required` provider; treat an unknown id as success.
- [ ] Advertise `agentCapabilities.providers = {}` only once the above is real.
- [ ] Decide whether provider configuration actually feeds model construction.
      A registry that does not affect the model resolver would be a lie; wiring it
      means mapping model ids → provider ids and instantiating models with the
      client's `baseUrl`/headers.
- [ ] Define whether changes apply to open sessions or only to sessions created
      or loaded afterwards (the RFD allows either; prefer the latter).
- [ ] Keep provider config process-scoped, in memory, and never echo headers
      back from `providers/list`.

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
  them when the corresponding capability is advertised. We currently advertise
  none of the group 1/2/3 capabilities.
- The adapter's stated contract is "translate protocol events; do not define
  agent behavior" (README). Groups 2 and 3 cross that line, so their designs need
  to say where the behavior lives.
- When implementing a group, replace the corresponding methods in
  `src/hrns/app/acp/adapter.py` and update README ("Limitations" and the
  session-store section) plus ARCHITECTURE.md if the adapter contract changes.
