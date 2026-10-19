# Verification and support boundaries

## Reproducible checks

```sh
uv sync --locked --all-packages
uv run ruff check .
uv run pytest -q
uv build --all-packages
```

The suite uses real Pydantic AI test/function models and the pinned official ACP SDK, without model-provider credentials. External SaaS, cloud sandbox and durability integrations require their own credentials and services; catalog metadata alone is not an integration test.

## Release record

> The test suite was removed for now. The results below are retained as a record of
> the last verification run before that removal.

Checked 18 October 2026 on macOS with Python 3.12:

- `uv run pytest -q`: **561 passed, 15 skipped** (the skipped tests are opt-in PostgreSQL integration tests; the PostgreSQL suite was separately run against PostgreSQL 17).
- Independent adversarial and spec reviews added `tests/test_runtime_review.py` (12), `tests/test_server_review.py` (32), `tests/test_acp_review.py` (16), and `tests/test_spec_{runtime,client,server,tui,manifest}.py` (65). Every confirmed finding was fixed: initial-configuration directional policy, close-during-composition cancellation, unavailable `add`, new-session-only `add`, duplicate definition pins, non-finite configuration values, deferred-approval cancellation settlement, complete `session/load` user-message and interruption replay, extension negotiation and error codes, model allow-list reload, unavailable standard options, root workspace matching, complete transaction retry identity, canonical cursors, capability/tool identity collisions, saved-policy revalidation, definition pin snapshots, transcript ordering, SQLite transaction error mapping, strict worker RPC frames, authoritative load-vs-resume recovery, advertised agent authentication, TUI read-only capability handling/pagination/attachment preflight, renderer fallback, and declarative binding-policy round-tripping.
- `uv run ruff check .`: clean.
- `uv build --all-packages`: four wheels and four source distributions built.
- Isolated wheel environments: protocol imports with only Pydantic; client imports without Pydantic AI or the runtime; runtime and server import without Textual; all console scripts answer `--version`/`--help`.
- Hosted end-to-end: authenticated catalog, WebSocket worker, capability disable, transaction retry replay, transcript reload, and the stdio bridge into the hosted worker all pass.

## Contract fixtures

`tests/fixtures/independent_acp_agent.py` is an SDK-free ACP v1 implementation. It deliberately advertises no Harness extensions, streams text and generic tool activity, requests permission using its own option IDs, supports authoritative history load, and can disconnect mid-turn. `test_independent_peer.py` validates it against the official ACP SDK. This tests cross-implementation behavior without claiming certification by an external agent vendor.

ACP notification handlers may run concurrently and finish after a prompt response arrives. Tests wait for observable events rather than assuming the response implicitly drains every notification.

## What counts as coverage

- **Runtime compatible:** a native capability can be composed in Python, without reinterpretation as a tool-only plugin.
- **Managed:** a registered factory validates configuration and resource requirements before construction.
- **Locally verified:** a test constructs or executes the integration using installed prerequisites.
- **Deployment-only:** the feature needs host-owned resources or immutable composition, and is not presented as an unrestricted next-turn switch.

Provider-specific behavior, isolation strength, API availability and durability semantics remain properties of the selected upstream backend. A catalog entry does not promise that arbitrary combinations are compatible.

## Operational boundaries

- Client and server use ACP v1. The WebSocket framing is a versioned project transport, not a claim of compatibility with draft ACP network transports.
- A running worker executes installed host Python code. Workspace isolation applies to workspace-aware operations, not arbitrary plugin code.
- Persistent conversations do not provide exactly-once external effects. Interrupted turns must be inspected; uncertain prompts are not resent automatically.
- Reference tests run on macOS/Linux. Provider-backed sandbox suites require their documented host prerequisites.
- The host is single-instance with per-connection workers. Shared database support does not imply distributed worker orchestration.

The final release check records actual test/build results in the repository's implementation plan and commit history.
