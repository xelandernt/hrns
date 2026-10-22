# Verification and support boundaries

Verification in this revision is manual. No test files were added; the project has no test
suite.

## Static checks and build

Run from the repository root:

```sh
uv run ruff format --check src examples test.py
uv run ruff check .
uv run pyrefly check src examples --no-progress-bar
uv build
```

Results recorded on 2026-10-22 (macOS, Python 3.13.5, pinned dependencies):

- `ruff format --check`: 67 files already formatted.
- `ruff check .`: clean.
- `pyrefly check src examples`: 0 errors.
- `uv build`: wheel and source distribution built (`dist/harness-0.1.0*`).

## Local stdio

A temporary definition module used a real `FunctionModel` with a streaming function. The
official ACP SDK client launched `python -m harness.runtime.host` as a subprocess and
exercised:

- `initialize` with `_meta.harness` advertisement;
- `session/new`, two `session/prompt` turns, `session/list`;
- `session/close`, `session/load` replay, and standard `session/delete`;
- a `FunctionToolset` tool with `requires_approval=True`: the client received
  `session/request_permission`, selected `allow_once`, and observed pending → in-progress →
  completed tool updates;
- process exit after stdin close, with the store closed and no stderr diagnostics;
- cancellation during a streaming turn: `session/cancel` returned `cancelled` after the first
  chunk, and the next prompt on the same session completed normally.

`harness-local --catalog` and `harness-host --help` start successfully. `harness-local`
itself was not run against a real provider because no credentials were used.

## Network WebSocket

A FastAPI/Starlette test application registered a native `FunctionModel` definition with
`AgentServer` and an SQLite store. Real WebSocket frames exercised:

- authenticated catalog discovery (`Principal("alice")` and `Principal("bob")`);
- `initialize`, `session/new`, `session/prompt`, `session/list`;
- cross-principal `session/load` rejection (Bob cannot load Alice's session);
- unauthenticated HTTP catalog rejection (401);
- application lifespan startup/shutdown.

## Live model and effort

The effort capability was checked directly: a native capability returned
`ModelRequestParameters.thinking == 'high'`, and the standard `effort` option changed from
`default` to `high` over the protocol, survived close/reload, and was advertised again as
`high`.

## Live capability configuration

Using the custom managed capability from `examples/custom_capability.py` against
`HarnessACPAgent`:

- `_harness/plugins/list` returned the registered plugin;
- `_harness/configuration/update` disabled the style binding, returned revision 2, and
  emitted `_harness/configuration/changed`;
- the next prompt ran without the capability's instructions (`style=none`);
- a `configure` change to `style=formal` returned the new config and applied on the next
  prompt;
- the same flow was exercised through `HarnessAcpClient` + `StdioAgentTransport` against a
  real `harness-host` subprocess, including local-binding-creation advertisement and the
  client-side `update_configuration` path;
- request-id replay returned the cached snapshot, and a stale revision was rejected before
  mutation.

The runtime did not add a durable revision table. Revisions and request-id idempotency are
in-memory for the live session.

## Disabled tools and DeepSeek DSML

When a capability that previously contributed a tool is disabled, the host reads native
function-tool names from the stored native `StoredSession` history and injects an error-only
`UnavailableTools` declaration. The stub is hidden when active tools exist, kept visible for
DeepSeek when no active tools remain, and raises `ToolFailed` if the model calls it. This
avoids DeepSeek leaking raw `｜｜DSML｜｜` markup after a tool disappears mid-conversation.

Checked against a synthetic managed tool: after one turn that called `ping`, disabling the
binding produced revision 2 with one `UnavailableTools` capability whose remembered set was
`{"ping"}`. Real DeepSeek output was not re-run with credentials in this environment.

## Persistence

SQLite round-trip was exercised through the real adapter: create, prompt, close, load
(replay), delete, and list. The store created a fresh `acp_sessions` table; no legacy tables
were read or migrated.

PostgreSQL live verification was **not run** in this environment because no service was
available. The class imports without `psycopg`; `initialize()` requires the `postgres` extra.
The SQLite and PostgreSQL implementations share the same row shape and scoped predicates.

## Generic ACP and client boundary

The client connects to standard ACP agents without invoking unadvertised extension methods.
A Harness host advertises `_harness/capabilities/list`, `_harness/configuration/get`,
`_harness/configuration/update`, `_harness/configuration/changed`, and
`_harness/session/rename`; `_harness/plugins/list` is added only when local binding creation
is enabled. `harness.client` does not import `harness.runtime` or Pydantic AI;
`harness.runtime` does not import FastAPI or Textual.

## Retired symbols

A repository search found no remaining imports or calls to `SessionManager`, `Session`,
`HostedSession`, `HarnessHostAgent`, `SessionJobManager`, `WorkspaceManager`,
`WorkspaceLease`, `_events`, or `runtime.session`. The old SQL tables
and runtime event journal are not created or read.

## Outstanding limitations

- PostgreSQL service round-trip.
- Real provider credentials and provider-specific reasoning behavior.
- Cancellation while a tool or permission dialog is in flight was not separately timed;
  streaming cancellation and post-cancel session reuse were verified.
- Long-duration transcript performance.
- Third-party ACP vendors other than the pinned SDK.

## Support boundaries

- An ACP agent executes installed Python code in its own process.
- A local workspace is not isolated; sandbox providers keep their upstream prerequisites.
- Persistence follows upstream save/load semantics (save failures logged, last writer wins).
- A completed prompt is not an exactly-once external effect.
- Provider, cloud sandbox, and durability integrations are not certified by catalog metadata
  alone.
