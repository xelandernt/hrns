# Concept compliance

This maps the original concept's success criteria to the current implementation. Verification
claims refer only to checks actually run in this revision; there is no test suite.

| # | Criterion | Status |
| --- | --- | --- |
| 1 | Define an agent and custom native capability; run through Python and open it in Textual | **Met.** `examples/custom_capability.py` composes a native capability; `hrns-local`/`hrns-host` serve definitions over ACP; `hrns` connects. |
| 2 | Managed capability contributes or is absent as a whole | **Met.** Enabled bindings are instantiated from the current selection; disabled bindings contribute nothing. Live toggles rebuild the native agent and apply on the next turn boundary. |
| 3 | Same definition runs remotely with different resources | **Met.** `AgentServer` registers native definitions and Python resources in FastAPI; the client never imports runtime/plugins. |
| 4 | Remote client cannot bypass mandatory policy | **Met.** Every edit is validated against availability, toggle/edit policy, lifecycle, requirements, conflicts, and schemas before the native agent is rebuilt. |
| 5 | Two sessions keep independent configuration/history | **Met.** The adapter owns per-session history; the store scopes rows by principal/deployment. |
| 6 | Client works with independent ACP implementations | **Met.** The client uses standard ACP methods and invokes extension methods only when advertised. |
| 7 | Standard ACP client drives local and hosted agents | **Met for the pinned ACP SDK.** Stdio and WebSocket use the same adapter. |
| 8 | Standard option and extension outcomes deterministic | **Model and effort** are standard options; managed configuration uses revision-checked extension updates with request-id idempotency. |
| 9 | Workspace tools use one selected environment | **Met for local paths.** Missing required workspace fails; no fallback. Sandbox providers remain deployment-specific. |
| 10 | Session load restores transcript/config after restart | **Met for committed turns.** The upstream adapter replays updates and restores native history; host metadata stores cwd/title/effort/bindings. |
| 11 | Catalog has managed descriptors or explicit constraints | **Partially restored.** The bundled catalog was removed for rework; `hrns.coder`, `hrns.todo`, and `hrns.skills` are restored with managed descriptors, `CAPABILITIES.md` retains the reviewed inventory, and custom plugins still provide managed descriptors. |
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
- Persistence uses upstream save/load semantics: save errors are logged. Hosted sessions
  additionally use a store-backed lease with a fencing token, so an actor that lost its
  lease is refused with `SESSION_CONFLICT` instead of overwriting the new owner.

## Release-gate contracts

The 0.0.7 public-contract audit classified ten contracts as missing (1-9 and 11). Current state:

| Contract | State |
| --- | --- |
| 1. Authoritative actor | **Met within one worker; lease-guarded across workers.** Connections to one deployment share one `HrnsACPAgent`; a store lease + fence makes ownership exclusive across workers and refuses stale saves. Cross-worker *attaching* still requires a future broker/routing layer. |
| 2. Fan-out + one global queue | **Met within one actor.** `FanoutClient` broadcasts updates, presentation, configuration, and command-catalog changes only to subscribers of the session, routes permissions to the turn's originator (never an arbitrary viewer), and one turn lock serializes prompts/configuration. A hosted actor has no update fallback after an unsubscribe; the sole-client fallback is local-stdio only. An in-flight client-origin call still routes to the connection that started it and is not re-authorized mid-turn. A second worker is refused with `SESSION_HELD`. |
| 3. Durable queue / uncertain turns | **Met.** `acp_prompt_queue` persists before execution; `enqueue`, `run`, `list`, and `retry` are public; cancellation/abandonment becomes `uncertain`; retries link to the original and execute once. |
| 4. Per-session policy | **Met.** `SessionPolicy` with private/shared/archived decisions, enforced on load, prompt, steering, configuration, commands, rename, access changes, and delete. An access change drops only viewers the new decision no longer admits, so a removed collaborator stops receiving the live stream while the owner and still-admitted collaborators keep their subscriptions. Reapplying an identical access and collaborator set is a no-op. |
| 5. Per-session capability gate | **Met.** `set_session_capabilities` overrides the deployment default per session; composition binds client-origin tools only when the session gate is open. |
| 6. Attribution metadata | **Met.** `RequestContext` carries principal, credential, connection, and agent IDs into policy decisions, queue rows, lifecycle hooks, and the persisted usage record. `request_id` exists but is not populated for ACP requests yet. |
| 7. Connection revocation | **Met.** `ConnectionRegistry` / `AgentServer.revoke_credential()` close exactly the matching sockets and await their tasks. |
| 8. Lifecycle hooks | **Met.** `SessionLifecycleHooks` covers create/resume/suspend/destroy and queue changes; destroy is once per host and failures surface as `LIFECYCLE_FAILED`. |
| 9. Dynamic deployments | **Met with app re-registration.** Revisions register while mounted, hide, retain, resolve by version, and persist in `acp_deployments`. Session scope is the logical agent, so a version bump does not orphan old sessions; the stored `definition_version` resolves the complete pinned revision and fails closed if it is not native. Stores from pre-namespace builds are unsupported and must be recreated. After restart the application re-registers native definitions; metadata-only placeholders are not served and never shadow an older native revision. |
| 10. Interactive load | **Met (unchanged).** |
| 11. Read-only replay | **Met.** Archived sessions load and replay; every mutating ACP path returns `SESSION_READ_ONLY`, and archived loads do not touch session metadata. |
| 12. Browser WebSocket auth | **Met (unchanged).** |

## Boundaries

- Installed capabilities are trusted host code; there is no malicious-plugin confinement.
- Optional provider, cloud sandbox, and durability integrations retain their upstream
  prerequisites and credentials.
- A local workspace is not a sandbox.
- The terminal does not offer client-side ACP filesystem/terminal services by default.
- Client-supplied MCP servers are rejected; MCP remains a deployment capability.
- Authentication is checked when the HTTP/WebSocket connection is established.
  `AgentServer.revoke_credential()` and `revoke_principal()` close the matching live
  sockets and await their handler tasks before returning; a revoked credential cannot
  open a new connection through `BearerAuth`.
