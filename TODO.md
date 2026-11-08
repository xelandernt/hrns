# TODO

Only unfinished work. `docs/acp.md` inventories the implemented and refused ACP methods;
`docs/mcp-over-acp-analysis.md` holds the MCP-over-ACP design notes and work breakdown.

## MCP-over-ACP

- [ ] `subscriptions/listen` streaming. The single-exchange `serve_one` path has no
      long-lived operation; subscriptions need a separate provider path,
      acknowledgement ordering, correlation, and cancellation.
- [ ] Explicit MRTR retry policy. `input_required` results pass through opaquely,
      but there is no policy for collecting input responses, retrying with a fresh
      `requestId`, or deciding when to stop.

Reference: <https://agentclientprotocol.com/rfds/mcp-over-acp>

## NES (next edit suggestions)

- [ ] `nes/start`, `nes/suggest`, `nes/close`, `nes/accept`, `nes/reject`:
      implement an inline-edit-suggestion service. This needs a completion model,
      a prompt strategy for edit/jump/rename/search-and-replace suggestions,
      suggestion ids, and accept/reject bookkeeping. The per-session document state
      is already tracked; there is no completion backend today.

Reference schema types: `StartNesRequest`, `SuggestNesRequest`, `CloseNesRequest`,
`AcceptNesNotification`, `RejectNesNotification`, `NesEditSuggestionVariant`.

## Skills

- [ ] Remote registries. `pydantic-ai-skills` supports Git, S3, and HTTP catalogs;
      `[skills]` exposes local libraries only. Wiring a registry needs a declared cache
      location, refresh policy, and a trust decision the current document has no place for.
- [ ] Live catalog refresh. The catalog is a `session/new` snapshot carried in `_meta`.
      Refreshing it mid-session needs either a server push or an extension method; neither
      is worth the surface until a client asks for it.
- [ ] Sandboxed script execution. `run_skill_script` runs on the host behind the normal
      approval policy. Passing a sandboxed executor is supported by `pydantic-ai-skills`
      but is not configurable from `hrns` yet.

## Open questions

- [ ] `hrns agent` marks the entry this deployment starts with as `required`, because
      disabling it would leave the default session without a model. Revisit once a client
      needs to disable that entry, or once sessions can fall back to another model.
- [ ] Decide whether the agent should ever be an MCP provider. `bind_provider` works on
      an `AgentSideConnection`, but ACP has no standard agent-to-client declaration path;
      the current flow is client-provided only.
