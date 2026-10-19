# Harness implementation plan

Build the concept as four independently installable Python distributions, with native Pydantic AI execution and ACP v1 at the process boundary. Deliver working vertical paths, then harden their policy, persistence, and recovery semantics. Tasks and acceptance checks are in [tasks/todo.md](tasks/todo.md); architectural decisions are in [tasks/plan.md](tasks/plan.md).

## Milestones

1. **Contracts and packaging:** versioned protocol models, reproducible development environment, package isolation.
2. **Programmable sessions:** native capability composition, policy validation, atomic revisions, SQLite persistence, deterministic offline demo.
3. **Local ACP experience:** SDK-based host, standalone client, streaming Textual UI, permissions and configuration.
4. **Remote deployment:** authenticated catalog, per-connection environment workers, bounded WebSocket transport, stdio bridge.
5. **Breadth and verification:** lazy capability catalog, starter definitions, integration tests, documentation and independent reviews.

Each milestone is verified and committed. Subagents own disjoint components against a shared contract; the coordinating agent integrates and verifies cross-component behavior. No changes are made to `candidate_2`.

## Verification strategy

Use real Pydantic AI test models (no provider credentials), policy and transaction tests, ACP SDK subprocess tests, Starlette transport tests, Textual Pilot tests, and wheel builds. Optional external providers are explicitly distinguished from locally verified integrations. Pin the actual installed upstream versions in a lockfile.

## Progress

- [x] Read concept and establish implementation plan.
- [x] Contracts and packaging: independent protocol wheel, locked workspace, strict DTO/framing tests and reviewed package boundaries.
- [x] Programmable sessions and persistence: native Pydantic AI composition, policy transactions, SQLite WAL, PostgreSQL option, workspace lifecycle, owned-job gating.
- [x] ACP host and standalone terminal: SDK host, standard-only interoperability, Textual terminal, headless runner and extension controls.
- [x] Remote hosting and bridge: authenticated catalog, isolated per-connection workers, bounded WebSocket transport, stdio bridge.
- [x] Catalog and examples: 107 reviewed entries with honest coverage tiers, developer/research starters, deterministic offline demo.
- [x] Independent reviews, documentation consistency and final release verification.

### Verified foundation

- Protocol, blob storage, independent standard-only ACP fixture and package-boundary checks pass.
- Protocol wheel builds and imports in an isolated environment without Pydantic AI or Textual; client and server wheels likewise install without the runtime/AI/UI packages they must not require.
- Independent review approved framing hardening, versioned list DTOs, authoritative lifecycle policy and scoped blob storage.
- Full local suite currently **561 passed, 15 skipped** (PostgreSQL integration tests are opt-in; the server author separately ran that suite against PostgreSQL 17).
- Hosted end-to-end test passes: authenticated catalog, WebSocket worker, native turn, next-turn capability disable, retry idempotency and transcript reload.
- Implementation and review agents initially used GLM-5.3-Flash at maximum reasoning. Provider rate limits and then fallback balance exhaustion required the coordinator to complete integration directly; subsequent reviews are retried with GLM and DeepSeek fallback.
