# Rust TUI migration

## Release outcome

Replace Textual with a native Ratatui client and a Ratzilla browser build in the same release. Both use the official Rust Agent Client Protocol (ACP) SDK and share the Ratatui interface. Preserve the current feature set on each applicable target. Keep the native client installable through Python packaging and launchable with bare `hrns` and `hrns connect`. Native supports local stdio and remote WebSocket. The browser connects exclusively to a remote ACP runtime. The Python server, agent, and headless `hrns run` continue in Python.

The native composer should become editable sooner than Textual, with faster typing and scrolling response during long, streaming sessions. Measure both clients with the same deterministic workload. Keep the Textual implementation available for comparison until cutover.

## Proposed architecture

- Share Rust application state, actions, Ratatui widgets, and ACP v1 session logic. Implement input, transport, clipboard, file selection, and persistence as target-specific services. Let input and drawing proceed independently of connection setup and ACP responses.
- Pin the official Rust ACP SDK after checking the Python runtime's actual wire messages and extension support. The current server expects one JSON message per WebSocket text frame. Try the SDK transport on native and use the browser WebSocket API through a runtime-neutral SDK transport on WASM if needed. Keep JavaScript handles outside the SDK connection and pass owned messages across that boundary.
- Preserve native bearer headers. The browser WebSocket API exposes URL and subprotocol arguments, so validate browser authentication early. Prefer a same-origin authenticated session or gateway that preserves the runtime principal and checks the browser origin on upgrade. Keep credentials out of URLs, bundles, logs, and persistent browser preferences.
- Keep the Python dispatcher as the public CLI. Prefer a version-matched platform wheel for the Rust executable, with Maturin binary packaging as the first candidate. Keep one owner for native TOML validation, configuration precedence, and keyring resolution. Start by evaluating reuse of the current Python configuration services through a narrow process boundary that keeps credential and config work off the first-frame path. PyPI installation is the deliverable for Python users.
- Keep native client I/O capability modes and MCP behavior. Use browser file selection and clipboard for user-initiated interactions. Host-only workspace, terminal, or MCP behavior in the browser uses a scoped remote service where needed for parity. The browser never launches a local agent. Advertise only capabilities that the current connection can serve.
- Preserve plugin command and renderer workflows through a Rust/WASM-compatible presentation contract and readable fallback. Legacy Python widget binaries depend on Textual. Inventory the existing integrations early so their behavior has a demonstrable replacement at cutover.
- Record architecture decisions proven in the early slices in [ARCHITECTURE.md](ARCHITECTURE.md). Update the affected architecture sections at cutover.

## Parity matrix

| Workflow | Native terminal | Browser |
| --- | --- | --- |
| Connection | Built-in Python agent, external stdio, remote WebSocket, profiles and overrides, reconnect, workspace and endpoint, initial session and cwd | Authenticated remote ACP, profiles and reconnect, remote workspace and endpoint, initial session and cwd |
| Composer and transcript | Multiline editing, keybindings and paste, draft and prompt history, completion and help, streamed Markdown/code/thoughts, expandable tools, scroll, search, copy and attachments | Same workflows using browser input, clipboard and file chooser |
| Sessions and turns | New/list/load/resume/rename/close/delete where supported, history and active-turn restoration, usage, plans, queue, steering and cancel | Same ACP-driven workflows |
| Agent configuration | Built-in setup, provider/login and model selection, effort and session settings | Remote provider/login, model and session settings where advertised |
| Interactions | Inline and modal approvals, permission modes, elicitation, client filesystem and terminal modes | Same approval and elicitation flows, with workspace operations scoped to the remote deployment |
| Skills and extensions | Skill catalog and mentions, advertised agent commands, command/tool/approval renderers and fallback | Same workflows using WASM-compatible renderers and fallback |
| MCP | Health, export and scoped consent, filters, auth and request routing | Same operator workflow using remote services for host-only transports |
| Preferences | Existing TOML precedence, profiles, last connection, theme, sidebar and transcript settings | Remote profiles and browser preferences with deployment-owned secrets |

Audit this matrix against the [Textual commands](hrns_slim/hrns/client/tui/commands.py), [widgets and workflows](hrns_slim/hrns/client/tui), [configuration reference](docs/configuration.md), [MCP behavior](docs/mcp.md) and [ACP contract](docs/acp.md). Use the resulting checklist for release verification.

## Implementation progress

**Current state.** A working preview is on the `ratatui` branch through `c0e6e2f`.
Install the separate `hrns-tui` wheel and set `HRNS_RUST_TUI_PREVIEW=1` to
preview the Python launch path. Textual still handles the default interactive
launch. The optimized browser bundle builds into `rust/dist/`.
[Build and launch details](ARCHITECTURE.md#rust-terminal-preview) ·
[Browser deployment](docs/authentication.md#serve-the-ratzilla-client-with-acp-authentication)

| Commit | Landed work |
| --- | --- |
| `8671f33` | Shared Ratatui composer and transcript, native stdio and WebSocket, Ratzilla WebSocket, ACP v1 streaming. |
| `e74b370` | Maturin binary wheel and opt-in Python launcher using existing target and credential resolution. |
| `f7ecdd6` | Session load and replay, workspace lookup, revisioned turn state, composer history, cached transcript layout, queue and steering, search and copy, model and effort options. |
| `a97dcda` | Paginated session listing, plan and usage display, advertised command dispatch, concurrent approval handling, native and browser file attachments. |
| `6baaf0d` | CI job for Rust formatting, native Clippy, and the WASM build. |
| `3eba27c` | Reconnect with `Ctrl-R`, keeping the draft, transcript and connection identity; verified native bearer and stdio startup cleanup. |
| `8938964` | In-app new, load, close and delete with capability checks, replay replacement and failure rollback. |
| `b06e8d8` | Turn lifecycle verification for steering, queueing, cancellation, reconnect and clear. |
| `c0e6e2f` | Markdown transcript rendering, status-aware expandable tool calls, and scroll anchoring across streaming and resize. |

### What was verified

- Native PTY and Chromium smokes each saw a text delta before completion
  under the same deterministic Pydantic AI workload. The native stdio smoke also streamed, and
  the browser showed the disconnect state after the server stopped.
- The macOS arm64 wheel installed in a clean Python 3.14 environment. The
  opt-in launcher streamed and exited on Ctrl-C. The final rebuilt wheel
  sent text and binary attachments through `hrns connect`.
- The same-origin HTTPS/WSS browser smoke rejected a missing cookie,
  accepted a secure cookie, and reconnected on reload with that cookie.
  Login and principal resolution belong to the deployment.
- Both targets loaded a saved session by ID without an explicit cwd, found
  the workspace through `session/list`, and displayed each replayed
  message once. Chromium also exercised draft editing, model selection
  and session listing. Native exercised queue dispatch, model and effort
  changes, and two concurrent ACP approvals.
- Native paths and the browser file chooser each staged text and binary
  attachments against an agent that advertised embedded context. The native
  build, optimized Ratzilla build, Rust formatting and Clippy, `just check`,
  and `just docs-build` passed locally. The hosted CI result remains to be
  checked.
- Startup smoke used macOS arm64, a 100×24 xterm-compatible PTY, seven
  launches per case, and one local WebSocket runtime. Median
  process-to-visible-draft was 499.7 ms for Textual `hrns connect` and
  24.8 ms for the direct Rust debug binary. The first direct Rust launch
  took 844.9 ms. Packaged startup, bare `hrns`, and long-stream p95 typing
  and scrolling are still needed for release qualification.
- Both targets reconnected with `Ctrl-R` after the ACP runtime restarted on
  the same endpoint. The draft, transcript and workspace identity survived,
  and the next turn-ready header showed a fresh session. Native rejected
  missing and wrong bearer tokens with the draft retained, and accepted the
  configured token. Native launched the built-in stdio agent, retained the
  draft after a missing command, and reaped the agent child on Ctrl-C.
- In-app `/new`, `/load <id>`, `/close` and `/delete` were exercised on both
  targets against a deterministic ACP agent. Loaded history replayed once,
  `Ctrl-R` reloaded it once, a missing session ID restored the prior
  transcript, and closing or deleting the current session started a new one.
  A native stdio switch left exactly one agent child and reaped it on exit.
  `/sessions` reflected the delete and close.
- Turn lifecycle ran on both targets against a slow deterministic agent. A
  prompt sent during an active turn steered into that turn; `/queue` held
  follow-ups locally; disconnecting kept the draft, transcript and queue, and
  `/resume-queue` after reconnect sent each queued prompt once. Cancellation
  stopped the turn, paused the local queue and kept the draft. The Python
  adapter pauses its server-side queue after `session/cancel` and exposes
  resume only in Python, so new ACP prompts stay queued until the session is
  closed and restored; the Textual client behaves the same way. The client
  only resumes its local queue explicitly.
- Transcript rendering on both targets styled a Markdown reply (heading,
  bold, inline code, link and fenced code), showed ACP tool calls with their
  status, expanded and collapsed tool details in place, kept the scroll
  anchor across streaming and a width change, and found retained text and
  copied the latest reply. Native copy emitted an OSC52 payload; the browser
  used its clipboard API.

### Next implementation pass

Start the remaining T05 wire checks alongside the connection work. Finish
the session-state checks before qualifying turn reconciliation, and confirm
each extension's wire contract before implementing its UI.

1. **Make the connection path recoverable (T04, T06, T07).** Verify the
   origin, expired-credential and native bearer cases. Exercise built-in
   stdio startup and child cleanup through the preview launcher. Add
   profile management and reconnect on both targets, preserving draft
   input and connection identity after a failed attempt.
2. **Close session and turn lifecycle gaps (T08, T09).** The in-app create,
   load, close and delete controls with capability checks are in place.
   Verify attachment to an already-running session, pending approvals after
   reconnect, cancellation, and queue pause/resume without replaying a
   submitted prompt. The current Python mount supports load and exposes
   session titles, but advertises neither resume nor rename, so verify the
   actual wire contract before adding those actions.
3. **Finish the protocol inventory and interaction paths (T05, T13–T17).**
   Check the pinned SDK against the deployed elicitation, provider and MCP
   methods. Restore approval modes, forms and provider login/setup.
   Implement and verify native `--client-io`, which the preview launcher
   currently rejects. Define how the remote deployment scopes browser
   workspace operations and MCP exports, then verify permissions, consent,
   and tool filtering end to end.
4. **Close UI and extension parity (T11, T12, T18).** Finish completion and
   skill mentions, activity panels and persisted settings. Port supported
   command and tool renderers. The Textual audit found inactive legacy
   plugin-command UI and Textual-specific renderer entry points. Use working
   Textual behavior and agent-advertised capabilities as the parity baseline.
5. **Qualify both release artifacts (T01, T03, T19–T21).** Capture the
   long-stream Textual baseline while feature work proceeds. Then compare
   packaged native and browser release builds. Align versions and build
   the platform wheel matrix, verify full/client-only/server-only
   installs, and publish the matching browser bundle. Change the default
   CLI only after both targets pass the parity and performance checks.

## Tasks

Paths under `rust/` are proposed locations. Size estimates count implementation files. Use existing checks and manual or ephemeral verification in accordance with the repo's test policy. The build commands and platform matrix for the new targets are outputs of the early slices.

### T01 — Record the Textual baseline

- [ ] **Acceptance** Inventory every supported workflow, keybinding, fallback and platform dependency. Record median time from launch to editable composer and p95 typing and scrolling input-to-render latencies under a deterministic long-history ACP stream.
- **Verify** Repeat on fixed hardware, terminal dimensions, message history and stream rate. Record the sample count and run-to-run variation for bare `hrns` and `hrns connect`.
- **Depends on** None. **Files** `PLAN.md`. **Size** S.

### T02 — Stream a remote prompt on both targets

- [x] **Acceptance** A native Ratatui preview and Ratzilla browser preview share the composer and transcript, establish ACP v1 against the same Python mount, and stream a prompt.
- **Verify** Build the native and `wasm32-unknown-unknown` targets, serve the browser bundle, and check that text arrives incrementally. Close the socket and check the disconnect state. Record the dependency pins and the browser transport approach.
- **Depends on** None. **Files** `rust/Cargo.toml`, `rust/src/app.rs`, `rust/src/native.rs`, `rust/src/web.rs`, `rust/index.html`. **Size** M.

### T03 — Launch a native preview from a Python wheel

- [ ] **Acceptance** A version-matched platform wheel installs without a Rust toolchain. A development dispatch path in the Python CLI launches it with inherited terminal streams and propagates exit status. The server-only extra installs independently of the binary wheel.
- **Verify** Build and install in a clean environment. Exercise normal exit and Ctrl-C, verify terminal restoration, and record install commands, versioning and candidate wheel platforms.
- **Depends on** T02. **Files** `rust/pyproject.toml`, `rust/Cargo.toml`, `hrns_slim/pyproject.toml`, `hrns_slim/hrns/cli.py`. **Size** M.

**Checkpoint after T03**

- [ ] Both previews stream against the Python mount, the wheel installs cleanly, and the Textual baseline is reproducible.

### T04 — Authenticate browser connections

- [ ] **Acceptance** A supported HTTPS deployment connects from the browser to the authenticated ACP mount, resolves the expected principal, and rejects unauthorized origins or missing credentials. Native bearer auth works against the same runtime.
- **Verify** Exercise login, socket reconnect, expired credentials, and a rejected origin in a real browser. Inspect the bundle, handshake, errors and status UI for secret redaction.
- **Depends on** T02. **Files** `rust/src/web/auth.rs`, browser deployment configuration, `docs/authentication.md`. **Size** M.

### T05 — Prove ACP extension interoperability

- [ ] **Acceptance** Check the pinned SDK against current session-state metadata/notifications, elicitation, providers, advertised commands and MCP-over-ACP. Record the schema features and any typed extension handlers required on each target.
- **Verify** Compare captured messages with the current client and server. Connect to an agent without the optional capabilities and check the corresponding fallback. Confirm the MCP wire revision and inventory Textual renderer integrations.
- **Depends on** T02, T04. **Files** `rust/Cargo.toml`, `rust/src/acp.rs`, `rust/src/acp/extensions.rs`, `ARCHITECTURE.md`. **Size** M.

### T06 — Start a local stdio agent

- [x] **Acceptance** Native can launch the built-in Python agent and an external stdio command with the selected cwd and environment, accepts draft input while connecting, and retains the draft after a failed launch.
- **Verify** Exercise both agent types, a child exit during startup, reconnect and clean shutdown. Check that owned child processes exit and that the WASM build still compiles.
- **Depends on** T02, T03. **Files** `rust/src/native.rs`, `rust/src/native/stdio.rs`, `rust/src/app.rs`. **Size** M.

**Checkpoint after T06**

- [ ] Authenticated web and both native transports work. Record the proven SDK and packaging decisions. Review required remote workspace and MCP integrations before implementing them.

### T07 — Restore connection profiles

- [ ] **Acceptance** Users can create, edit, select and reopen applicable profiles. Native preserves config precedence, overrides and keyring references. Both targets retain drafts on failed auto-connect and identify the workspace and endpoint in the UI.
- **Verify** Exercise explicit and remembered profiles, removed profiles, an unreachable target and invalid native config. Relaunch both targets and confirm preference persistence and redaction.
- **Depends on** T04, T06. **Files** `rust/src/connection.rs`, `rust/src/native/config.rs`, `rust/src/web/preferences.rs`, `rust/src/ui/picker.rs`, `hrns_slim/hrns/cli.py`. **Size** M.

### T08 — Restore and manage sessions

- [ ] **Acceptance** New, list, load, resume, rename, close and delete work where advertised. Restored history and shared live state appear in order without duplication. Opening a remote session by ID without explicit cwd can resolve it through paginated listing.
- **Verify** Load a prior session and join another during an active turn. Compare history and live state with Textual. Exercise an absent session, denied operation and unsupported capability.
- **Depends on** T05, T07. **Files** `rust/src/acp/session.rs`, `rust/src/session.rs`, `rust/src/ui/sessions.rs`, `rust/src/transcript.rs`. **Size** M.

### T09 — Restore prompt lifecycle controls

- [x] **Acceptance** Queue, steer, cancel, clear and resume work as supported by the current client and agent. Drafts stay editable while busy. Disconnection, explicit cancellation and session close have their existing distinct effects.
- **Verify** Queue multiple prompts, cancel mid-turn, resume or clear, then reconnect and check active-turn state and duplicate submissions. The Python adapter exposes server-side resume only in Python; after cancellation, new ACP prompts stay queued.
- **Depends on** T08. **Files** `rust/src/acp/turn.rs`, `rust/src/turn.rs`, `rust/src/ui/composer.rs`, `rust/src/ui/queue.rs`. **Size** M.

**Checkpoint after T09**

- [ ] Both targets connect, manage and restore sessions, and complete or cancel a turn. Recheck native startup through the preview entry point.

### T10 — Render the complete transcript

- [x] **Acceptance** Stream Markdown, code, thoughts and expandable tools. Preserve scroll position across streaming and resize. Find retained text and copy the latest completed response.
- **Verify** Replay a large mixed transcript. Scroll back mid-stream, resize, find an older result and copy a multiline response on both targets. Check for stalled input or full-history reformatting on every update.
- **Depends on** T09. **Files** `rust/src/transcript.rs`, `rust/src/ui/transcript.rs`, `rust/src/ui/format.rs`, `rust/src/native.rs`, `rust/src/web.rs`. **Size** M.

### T11 — Restore composer gestures

- [ ] **Acceptance** Restore multiline input, paste, draft and prompt history, completion, help and skill mentions. Restore attachment staging and removal with existing metadata and validation. Native uses file paths and browser uses an explicit file chooser.
- **Verify** Check keyboard shortcuts in both targets, agent and local completion, skill references, text and image attachments, and unsupported or oversized input feedback.
- **Depends on** T09. **Files** `rust/src/ui/composer.rs`, `rust/src/ui/completion.rs`, `rust/src/attachments.rs`, `rust/src/native/files.rs`, `rust/src/web/files.rs`. **Size** M.

### T12 — Restore status panels

- [ ] **Acceptance** Restore activity, usage, todo/plan and sidebar navigation. Persist theme, sidebar and transcript preferences. Preserve connection identity in narrow layouts.
- **Verify** Drive usage and plan updates through a turn, toggle panels and preferences, then relaunch both targets at narrow and wide dimensions.
- **Depends on** T08, T10. **Files** `rust/src/ui/status.rs`, `rust/src/ui/sidebar.rs`, `rust/src/ui/settings.rs`, `rust/src/preferences.rs`. **Size** M.

**Checkpoint after T12**

- [ ] Walk the transcript, composer and preferences rows on both targets. Measure long-history input and scrolling again and record regressions.

### T13 — Respond to interactive requests

- [ ] **Acceptance** Preserve inline approvals, modal fallback, approval modes and elicitation choices. Requests remain actionable across screen changes and resolve once across cancellation or disconnect.
- **Verify** Exercise allow, deny, cancel, auto mode and concurrent interactions. Compare wire responses and displayed turn state on both targets.
- **Depends on** T05, T09. **Files** `rust/src/acp/interactions.rs`, `rust/src/ui/approval.rs`, `rust/src/ui/elicitation.rs`, `rust/src/preferences.rs`. **Size** M.

### T14 — Set agent models and session options

- [ ] **Acceptance** Discover, configure and select providers and models. Preserve applicable login and credential flows. Restore effort and other advertised session options.
- **Verify** Start with missing credentials, configure a native built-in provider and a remote provider as supported, change model and effort in a running session, and reload the selected values.
- **Depends on** T05, T07, T08. **Files** `rust/src/acp/providers.rs`, `rust/src/ui/providers.rs`, `rust/src/ui/models.rs`, `rust/src/native/config.rs`, `rust/src/web/auth.rs`. **Size** M.

### T15 — Apply workspace access policy

- [ ] **Acceptance** Native filesystem and terminal callbacks match `off`, `read` and `all`. Browser workspace and command operations address the remote workspace and follow the deployment and session policy.
- **Verify** Use a disposable workspace to exercise allowed and denied reads, writes and terminal calls. Check cleanup, capability negotiation and the browser's remote workspace identity.
- **Depends on** T04, T06, T13. **Files** `rust/src/acp/client_io.rs`, `rust/src/native/io.rs`, `rust/src/ui/settings.rs`, remote deployment wiring. **Size** M.

**Checkpoint after T15**

- [ ] Compare approval, elicitation, provider and workspace operations with the parity matrix. Review credential ownership and the capabilities each connection advertises.

### T16 — Export native MCP tools

- [ ] **Acceptance** Restore MCP health, auth, export consent, filters, request routing and progress. Preserve one-time, connection and persistent consent behavior.
- **Verify** Use a deterministic MCP server to exercise each consent decision, credential failure, a filtered tool call, progress and reconnect. Compare framing to T05.
- **Depends on** T05, T07, T13. **Files** `rust/src/acp/mcp.rs`, `rust/src/native/mcp.rs`, `rust/src/ui/mcp.rs`, `rust/src/preferences.rs`. **Size** M.

### T17 — Export browser MCP tools through the remote deployment

- [ ] **Acceptance** Browser exposes the same applicable health, export and consent workflow through explicitly configured remote services. Host-only transports and credentials stay on the remote side, scoped to the current principal and session.
- **Verify** Repeat the T16 user flow through the browser. Exercise failure, reconnect and attempted reuse from an unrelated session.
- **Depends on** T04, T05, T16. **Files** `rust/src/web/mcp.rs`, `rust/src/ui/mcp.rs`, remote MCP integration and deployment documentation. **Size** M.

### T18 — Restore agent commands and presentations

- [ ] **Acceptance** Restore advertised agent commands, name collision routing, structured command/tool/approval rendering and renderer fallback. Explicit user actions are required to invoke a command from a view.
- **Verify** Run a colliding command, inspect a rendered tool and approval, replay without reinvoking a command, and exercise an unknown renderer on both targets.
- **Depends on** T05, T10, T11, T13. **Files** `rust/src/acp/commands.rs`, `rust/src/ui/commands.rs`, `rust/src/ui/renderers.rs`, `rust/src/ui/approval.rs`. **Size** M.

**Checkpoint after T18**

- [ ] Complete the parity walkthrough on both targets, including MCP scopes and plugin integrations. Resolve missing workflows before qualifying the release.

### T19 — Qualify responsiveness

- [ ] **Acceptance** Median native startup and p95 typing and scrolling latency improve beyond Textual's measured run-to-run variation. The browser meets a responsiveness target established from the preview. Both retain all input and transcript updates under the measured load.
- **Verify** Repeat T01 with the release build, the installed Python entry point, and a browser loading the deployed bundle. Record medians, p95s, environment and workload. Measure composer and runtime readiness separately.
- **Depends on** T12, T17, T18. **Files** `PLAN.md`. **Size** S for measurement. Track any necessary performance fixes as focused follow-up tasks.

### T20 — Produce paired release artifacts

- [ ] **Acceptance** CI produces native wheels for the supported platforms and a version-matched Ratzilla bundle. Deployment instructions cover HTTPS, authentication, and the remote ACP runtime. Python artifacts include the intended full, client-only and server-only install paths.
- **Verify** Install staged wheels in empty Python environments, check native launch, and serve the built web bundle against a remote runtime. Inspect artifact version metadata and CI results.
- **Depends on** T03, T04, T17, T19. **Files** `.github/workflows/ci.yml`, `.github/workflows/publish.yml`, `justfile`, `rust/pyproject.toml`, browser deployment docs. **Size** M.

### T21 — Switch the interactive command

- [ ] **Acceptance** Bare `hrns` and `hrns connect` launch Ratatui. The paired browser artifact is ready for the same release. Retire Textual and its dependency after checking callers. Keep shared Python modules used by `hrns run`.
- **Verify** Run `just check`, `just docs-build`, Rust and WASM CI checks, and installed-artifact smokes for interactive launch, a headless prompt and an agent launch. Repeat the deployed browser smoke with the final bundle.
- **Depends on** T19, T20. **Files** `hrns_slim/hrns/cli.py`, `hrns_slim/pyproject.toml`, `uv.lock`, `ARCHITECTURE.md`, `README.md`. **Size** M for cutover. Remove the legacy UI package in a separate mechanical change in the same release.

**Release checkpoint**

- [ ] Both targets pass their parity checklist, startup and responsiveness checks, and installed-artifact verification at the same version.

## Early decisions and risks

| Decision or risk | Resolve in | Required evidence |
| --- | --- | --- |
| SDK transport framing or unstable methods differ from the Python server | T02, T05 | Streaming prompt and extension interoperability against the real Python mount on both targets |
| Browser cannot supply an arbitrary bearer header | T04 | Authenticated HTTPS deployment with origin validation, expiry behavior and principal mapping |
| New wheels complicate the current Python release pipeline | T03 | Clean install, version resolution, one public CLI entry point, and a supported platform matrix |
| Native-only I/O and MCP need equivalent browser workflows | T05, T15, T17 | Capability mapping and scoped remote execution and export |
| Existing plugins return Textual widget objects | T05, T18 | Inventory and a demonstrated behavioral replacement for each supported renderer workflow |
| Python dispatch or long-history rendering erases performance gains | T01, T12, T19 | Repeatable measurements through the shipped entry points |

The preview proves the base transport on both targets. Benchmarking (T01),
release version coordination (T03), and the remaining browser authentication
checks (T04) can proceed alongside the connection work. Keep session
restoration ahead of turn reconciliation. Transcript and composer parity can
proceed alongside the interaction work once the turn state is stable.

## References

- [Official Rust ACP SDK](https://docs.rs/agent-client-protocol/latest/agent_client_protocol/) and [WASM support](https://docs.rs/crate/agent-client-protocol/latest#webassembly)
- [Native Rust ACP WebSocket transport](https://docs.rs/crate/agent-client-protocol-http/latest)
- [Ratatui](https://ratatui.rs/) and [Ratzilla build and deployment](https://github.com/ratatui/ratzilla)
- [Browser WebSocket constructor](https://developer.mozilla.org/en-US/docs/Web/API/WebSocket/WebSocket)
- [Maturin binary wheels](https://www.maturin.rs/bindings.html#bin)
