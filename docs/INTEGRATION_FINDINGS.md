# Integration findings

Historical cross-package findings and their current resolution. The adapter migration
supersedes several earlier workarounds.

| Finding | Resolution |
| --- | --- |
| `hrns run --server ... "prompt"` treated the prompt as a stdio command | Only explicit `--stdio` splits `CMD -- PROMPT`; remote targets use the remainder as prompt. |
| Server descriptors use `defaultWorkspace` / `allowedWorkspaces` | The client reads those stable wire names. |
| Upstream experimental ACP warning polluted host stderr | Filtered at the runtime package boundary before importing the pinned adapter. |
| PostgreSQL existed behind a store interface without a selector | `build_store()` selects by DSN for stdio; the embedded server accepts a `PostgresSessionStore`. |
| Background work had no binding ownership or disable gate | Removed with the managed job subsystem; there is no background-job API in this revision. |
| Configuration retry could cancel jobs twice | Removed with the job subsystem; live capability edits use in-memory revision/idempotency checks and apply at the turn boundary. |
| Workspace identity could be lost or silently replaced | Removed with managed leases; a local path is stored as host metadata and validated on reload. |
| Custom ACP loop duplicated execution, approvals, cancellation, and history | Replaced by `PydanticAIACPAgent`; the host only composes and scopes persistence. |
| Separate runtime event journal and turn store | Removed. The upstream `StoredSession` is the durable conversation record. |
| ACP `session/list` and `session/delete` unavailable from the upstream adapter | Small public subclass methods backed by scoped host metadata; delete uses the standard route. |
| No per-turn model-settings hook | A native capability returns per-request model settings, so effort changes live while bindings stay fixed. |

Verification of these items is recorded in [VERIFICATION.md](VERIFICATION.md). No regression
test suite is present in this revision.
