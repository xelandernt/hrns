# Integration findings

Issues found while connecting independently developed packages. All listed items are resolved and covered by regression tests; this file remains as a record of the cross-package contract checks.

| Finding | Resolution | Regression check |
| --- | --- | --- |
| `hrns run --server ... "prompt"` treated the prompt as a stdio command | Only explicit `--stdio` splits `CMD -- PROMPT`; local-model and remote targets use the remainder as prompt | headless positional-prompt check |
| Server descriptors use `defaultWorkspace` / `allowedWorkspaces`; client read other spellings | Client accepts the server's stable wire names | `test_server_catalog_supplies_client_remote_workspace` |
| Upstream experimental ACP warning polluted host stderr once per process | Filtered at the pinned adapter import boundary, not globally | manual host smoke; subprocess ACP tests |
| PostgreSQL existed behind the runtime store interface without a selector | `build_store()` selects via DSN; server passes `server.store_url` as the worker's `HARNESS_STORE` | `test_postgres_store_url_is_passed_to_every_worker`; opt-in PostgreSQL suite |
| Background work had no binding ownership or disable gate | `SessionJobManager`; disable cancels/drains, cannot claim success while work is active | `tests/test_jobs.py` |
| Configuration retry could cancel jobs twice | Completed retry returns from the stored transaction before any job handling | hosted end-to-end retry assertion |
| Workspace identity could be lost or silently replaced | Independently tested lifecycle service with durable records and `WORKSPACE_UNAVAILABLE` | `tests/test_workspaces.py`, runtime workspace tests |

The native headless path returns the agent's text, and the hosted end-to-end path exercises catalog discovery, authenticated WebSocket, capability disable, retry idempotency and authoritative transcript reload.
