# Concept compliance

This maps the original concept's success criteria to the current implementation. Verification
claims refer only to checks actually run in this revision; there is no test suite.

| # | Criterion | Status |
| --- | --- | --- |
| 1 | Define an agent and custom native capability; run through Python and open it in Textual | **Met.** `examples/custom_capability.py` composes a native capability; `harness-local`/`harness-host` serve definitions over ACP; `hrns` connects. |
| 2 | Managed capability contributes or is absent as a whole | **Met.** Enabled bindings are instantiated from the current selection; disabled bindings contribute nothing. Live toggles rebuild the native agent and apply on the next turn boundary. |
| 3 | Same definition runs remotely with different resources | **Met.** `AgentServer` registers native definitions and Python resources in FastAPI; the client never imports runtime/plugins. |
| 4 | Remote client cannot bypass mandatory policy | **Met.** Every edit is validated against availability, toggle/edit policy, lifecycle, requirements, conflicts, and schemas before the native agent is rebuilt. |
| 5 | Two sessions keep independent configuration/history | **Met.** The adapter owns per-session history; the store scopes rows by principal/deployment. |
| 6 | Client works with independent ACP implementations | **Met.** The client uses standard ACP methods and invokes extension methods only when advertised. |
| 7 | Standard ACP client drives local and hosted agents | **Met for the pinned ACP SDK.** Stdio and WebSocket use the same adapter. |
| 8 | Standard option and extension outcomes deterministic | **Model and effort** are standard options; managed configuration uses revision-checked extension updates with request-id idempotency. |
| 9 | Workspace tools use one selected environment | **Met for local paths.** Missing required workspace fails; no fallback. Sandbox providers remain deployment-specific. |
| 10 | Session load restores transcript/config after restart | **Met for committed turns.** The upstream adapter replays updates and restores native history; host metadata stores cwd/title/effort/bindings. |
| 11 | Catalog has managed descriptors or explicit constraints | **Partially restored.** The bundled catalog was removed for rework; `harness.coder` and `harness.skills` are restored with managed descriptors, `CAPABILITIES.md` retains the reviewed inventory, and custom plugins still provide managed descriptors. |
| 12 | Long transcripts, cancellation, connection errors | **Met for bounded rendering and adapter cancellation.** No long-duration performance benchmark is claimed. |

## Deliberate changes

- Managed bindings can be edited live for `next_turn` capabilities; `new_session` bindings
  still require a new session.
- Configuration revisions and request-id idempotency are in-memory per open session rather
  than a durable revision table.
- Automatic model-generated titles were removed; first-prompt fallback and manual rename remain.
- Managed workspace leases and background-job ownership were removed; local workspace paths
  are passed through `AcpSessionConfig.workspace`.
- Direct Python `SessionManager`/`Session` APIs and runtime event journals were removed in
  favor of `compose_definition` and native agent runs.
- Persistence uses upstream save/load semantics: save errors are logged, and simultaneous
  writers are last-write-wins.

## Boundaries

- Installed capabilities are trusted host code; there is no malicious-plugin confinement.
- Optional provider, cloud sandbox, and durability integrations retain their upstream
  prerequisites and credentials.
- A local workspace is not a sandbox.
- The terminal does not offer client-side ACP filesystem/terminal services by default.
- Client-supplied MCP servers are rejected; MCP remains a deployment capability.
- Authentication is checked when the HTTP/WebSocket connection is established. Continuous
  token revocation is application-owned.
