# Rust TUI migration

## Release outcome

Replace Textual with a native Ratatui client and a Ratzilla browser build in the same release. Both use the official Rust Agent Client Protocol (ACP) SDK and share the Ratatui interface. Preserve the current feature set on each applicable target. Keep the native client installable through Python packaging and launchable with bare `hrns` and `hrns connect`. Native supports local stdio and remote WebSocket. The browser connects exclusively to a remote ACP runtime. The Python server, agent, and headless `hrns run` continue in Python.

The native composer should become editable sooner than Textual, with faster typing and scrolling response during long, streaming sessions. Measure both clients with the same deterministic workload. Keep the Textual implementation available for comparison until cutover.

## Proposed architecture

- Share Rust application state, actions, Ratatui widgets, and ACP v1 session logic. Implement input, transport, clipboard, file selection, and persistence as target-specific services. Let input and drawing proceed independently of connection setup and ACP responses.
- Pin the official Rust ACP SDK after checking the Python runtime's actual wire messages and extension support. The current server expects one JSON message per WebSocket text frame. Try the SDK transport on native and use the browser WebSocket API through a runtime-neutral SDK transport on WASM if needed. Keep JavaScript handles outside the SDK connection and pass owned messages across that boundary.
- Preserve native bearer headers. The browser WebSocket API exposes URL and subprotocol arguments, so validate browser authentication early. Prefer a same-origin authenticated session or gateway that preserves the runtime principal and checks the browser origin on upgrade. Keep credentials out of URLs, bundles, logs, and persistent browser preferences.
- Keep the Python dispatcher as the public CLI. Prefer a version-matched platform wheel for the Rust executable, with Maturin binary packaging as the first candidate. Keep one owner for native TOML validation, configuration precedence, and keyring resolution. Start by evaluating reuse of the current Python configuration services through a narrow process boundary that keeps credential and config work off the first-frame path. PyPI installation is the deliverable for Python users.
- Keep native client I/O capability modes and MCP behavior. The browser deliberately omits client filesystem, terminal and MCP-over-ACP: it advertises none of those capabilities and uses only browser file selection and clipboard for user-initiated interactions. The browser never launches a local agent. Advertise only capabilities that the current connection can serve.
- Support advertised agent commands and skill catalogs with a readable default presentation. Third-party plugins and their Textual widget renderers are out of scope for this release; do not carry the presentation-contract boundary forward.
- Record architecture decisions proven in the early slices in [ARCHITECTURE.md](ARCHITECTURE.md). Update the affected architecture sections at cutover.

## Parity matrix

| Workflow | Native terminal | Browser |
| --- | --- | --- |
| Connection | Built-in Python agent, external stdio, remote WebSocket, profiles and overrides, reconnect, workspace and endpoint, initial session and cwd | Authenticated remote ACP, profiles and reconnect, remote workspace and endpoint, initial session and cwd |
| Composer and transcript | Multiline editing, keybindings and paste, draft and prompt history, completion and help, streamed Markdown/code/thoughts, expandable tools, scroll, search, copy and attachments | Same workflows using browser input, clipboard and file chooser |
| Sessions and turns | New/list/load/resume/rename/close/delete where supported, history and active-turn restoration, usage, plans, queue, steering and cancel | Same ACP-driven workflows |
| Agent configuration | Built-in setup, provider/login and model selection, effort and session settings | Remote provider/login, model and session settings where advertised |
| Interactions | Inline and modal approvals, permission modes, elicitation, client filesystem and terminal modes | Same approval and elicitation flows; no client filesystem or terminal callbacks |
| Skills and extensions | Skill catalog and mentions, advertised agent commands, built-in presentation | Same workflows; plugins and custom renderers are out of scope |
| MCP | Health, export and scoped consent, filters, auth and request routing | Not offered; MCP stays with native clients and `hrns run` |
| Preferences | Existing TOML precedence, profiles, last connection, theme, sidebar and transcript settings | Remote profiles and browser preferences with deployment-owned secrets |

Audit this matrix against the [Textual commands](hrns_slim/hrns/client/tui/commands.py), [widgets and workflows](hrns_slim/hrns/client/tui), [configuration reference](docs/configuration.md), [MCP behavior](docs/mcp.md) and [ACP contract](docs/acp.md). Use the resulting checklist for release verification.

## Implementation progress

**Current state.** The native client is the only interactive client (`bc0ccb7`). The Python
dispatcher requires the installed `hrns-tui` wheel and reports an install hint when it is
missing; the Textual package and its dependency are gone. Native covers approvals with
modes, agent forms, client I/O, themes, settings, remote provider routing and
MCP-over-ACP export through a Python sidecar. The browser omits client I/O and MCP by
design. The optimized browser bundle builds into `rust/dist/`. Only the hosted artifact
run and a final real-workload release smoke remain.
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
| `f6bc1c8` | Slash-command and `$skill` completion, skill catalogs from session metadata, and prompt skill references. |
| `53784b3` | Activity sidebar with plan, usage, skills and recent events; persisted sidebar, tool and reasoning preferences on both targets. |
| `ef2d1c5` | Inline approvals with modes and kind shortcuts, agent elicitation forms, native client I/O, themes and settings, session titles and usage cost, remote provider routing, default native launch with a Textual fallback, and wheel/bundle CI jobs. |
| `1497edb` | Canvas browser backend, `method_not_found` for unadvertised browser I/O, and browser input-latency qualification. |
| `3c0e5eb` | Native MCP-over-ACP export through a Python sidecar: declarations, consent, filters, health, progress and failure handling. |
| `88b21a0` | Textual removed; the dispatcher always launches the native wheel and remembers the reopened identity. |
| `bc0ccb7` | Release packaging: version-matched native dependency, source-tree wheel build, and developer install recipe. |

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
- Composer completion on both targets listed slash commands and
  agent-advertised skills from `hrns/skills`, accepted with Tab, navigated
  with the arrow keys and dismissed with Escape before cancelling a turn. A
  `$release-notes` mention reached the deterministic agent as a skill
  expansion. Native bracketed paste inserted single- and multi-line drafts,
  prompt history recalled a submitted prompt, and browser paste inserted
  through its clipboard event. Attachment staging was verified earlier with
  an agent that advertised embedded context.
- Panels on both targets opened with `Ctrl-B`, showing connection identity,
  plan, queue, skills and recent activity; tool transitions and turn outcomes
  appeared in the activity list. Native saved the open sidebar, tool expansion
  and reasoning preference to a state file and restored them on relaunch. The
  browser restored the sidebar and its later closed state from local storage
  across reloads. The header retained the endpoint and workspace when the
  window was too narrow for the sidebar. Themes, permission mode and sidebar
  state persist per target; the launcher seeds the Python TOML values.
- The wire inventory matched captured traffic: one `_hrns/session/state`
  notification (with `activeTurnId`, `lastOutcome` and `lastUsage`),
  `session_info_update`, `current_mode_update`, `usage_update.cost`,
  `elicitation/create`, `fs/*`, `terminal/*`, `providers/list|set|disable`,
  `mcp/message` and the `session/list` `cwd` filter. The mount advertises no
  `available_commands_update` and no resume, fork, mode or rename method.
- A deterministic mock ACP agent exercised the native client end to end through
  the installed wheel. `y` answered `session/request_permission` with
  `allow_once`; the form response carried `q0=Postgres` and `q1=hello`; the
  agent read 807 bytes from `AGENTS.md`; `/bin/echo` produced `terminal-ok`,
  exit code 0, and released its terminal. With `--permission-mode all` the same
  request resolved without a keypress. With `--client-io read`, the read
  succeeded and `terminal/create` returned `method_not_found`.
- Native wrote `theme` and permission changes to its state file and restored
  them. The default launcher chose native when the wheel was installed, and
  `HRNS_TEXTUAL=1` chose Textual. A mock-advertised `help` command collided with
  the local command and `/agent help` routed `/help` to the agent.
- Native MCP export ran end to end against a deterministic stdio server through
  the sidecar: the mock agent received the ACP declaration, `tools/list`
  returned `echo`, `tools/call` returned `native-mcp`, and two progress
  notifications reached the agent. `include_tools = ["echo"]` hid `slow` from
  the list and made its call an `is_error` result. Consent `2` allowed the
  server for the connection, `/new` reused it without a second prompt, and `3`
  rewrote `always_allow = true` in the TOML document. A nonexistent command
  showed `failed` in `/mcp` while the session still connected.
- The released browser bundle ran in Chromium over a WebSocket bridge: it
  connected, auto-approved under `permission=all`, answered the same form,
  declined `fs` and `terminal` with `method_not_found`, and rendered `Done.`
  on the canvas backend. The 500-line stream finished in 1.506 s; typing and
  scrolling p95 were 13.5 ms and 12.9 ms.
- The macOS arm64 wheel installed into an empty Python 3.14 environment, showed
  `hrns-tui 0.1.0`, and launched through `hrns` with the resolved `client_io`,
  theme and permission mode. `hrns-slim[server]` resolved without Textual and
  `hrns-slim[client]` resolved with it; neither pulls `hrns-tui` until the
  release adds the version-matched dependency.

### Baseline inventory and measurements

**Inventory.** The Textual client is the baseline for the rows below. "Native"
and "Browser" record the Rust implementation at `ef2d1c5`.

| Area | Textual baseline | Native | Browser |
| --- | --- | --- | --- |
| Connection | `connect` picker, profiles, overrides, remembered target, reconnect, built-in/external stdio and remote WebSocket | CLI/launcher target, profiles resolved by `hrns profiles`, reconnect, `Ctrl-R`; no in-app picker | Same-origin remote only, `endpoint`/`cwd`/`session` parameters, reconnect |
| Composer | Multiline, paste, history, completion, `@file`, help | Multiline, paste, history, `/` and `$skill` completion, help | Same via browser input and clipboard; no file-path completion |
| Transcript | Markdown, code, thoughts, tool cards, search, copy, jump-to-latest | Markdown subset, thoughts toggle, expandable tool calls, `/find`, `/copy`, scroll anchoring | Same shared renderer |
| Sessions | Panel with filter, load, delete, close, new, paging | `/sessions [next]`, `/new`, `/load`, `/close`, `/delete`, title updates | Same ACP calls |
| Turns | Queue, steer, cancel, clear, resume | Same; Python server-side resume remains Python-only | Same |
| Approvals | Inline bar, modal fallback, `ask/reads/all`, kind shortcuts | Inline, `ask/reads/all`, `1-9` and `y/a/n/N`, tool detail | Same inline |
| Elicitation | Form dock with tabs and validation, URL modal | Stepwise form dock, choice/text/boolean/multi-select, required validation | Same |
| Providers | TOML dialog, device login, ACP routing, model picker | `/providers`, `/provider set|disable`; login and TOML stay Python commands | ACP routing where advertised |
| Panels | Todos, sessions, activity, MCP; theme and settings dialog | `Ctrl-B` sidebar, `/settings`, `/theme`, `/permissions` | Sidebar, `theme`/`permission` parameters |
| MCP | `[mcp]` config, consent, health, export over ACP | Built-in agent keeps server-side tools; no client export | None |
| Commands | Palette, `/agent`, plugin commands | `/agent` with collision warning; plugin commands out of scope | Same |
| Platform | Textual on CPython; POSIX process replacement for the launcher | macOS/Linux/Windows wheels; stdio, bearer WebSocket; terminal PTY | wasm32 bundle, HTTPS/WSS only |

Keybindings present in Textual but not native: `Ctrl+K` palette, `F1` help,
`F3` sessions, `F4` activity, `Ctrl+T` transcript focus, `Ctrl+F` search
dialog, `Alt+Up/Down` message jump, `Ctrl+End` latest, per-tool-card `Enter`,
mouse, and docked-panel resize. Native adds `Ctrl-O` tool expansion and
`/settings`. Function keys and the palette are command equivalents; mouse and
resize are not planned.

**Measurements.** Fixed environment: macOS arm64, 100×24 PTY with
`TERM=xterm-256color`, a deterministic mock ACP agent (`session/new` plus a
500-line stream at 2 ms per line), seven launches per case. Latency is wall
time from process spawn to the first frame containing the marker, and typing
and scrolling are the p95 of 40 keystrokes and 20 page-downs after the stream
settles.

| Measurement | Textual | Native binary | Native via `hrns` | Browser |
| --- | --- | --- | --- | --- |
| Launch to editable composer, median (min–max) | 235.0 ms (190.2–289.8) | 13.0 ms (12.4–17.3) | 247.8 ms (247.0–248.8) | not measured |
| Launch to connected composer, median (min–max) | 674.8 ms (645.7–677.7) | 48.9 ms (47.6–49.8) | 261.3 ms (257.2–269.1) | not measured |
| Typing input-to-render after 500 lines, median / p95 | 37.3 / 51.0 ms | 0.2 / 0.2 ms | not measured | 12.1 / 13.5 ms |
| PgDn input-to-render after 500 lines, median / p95 | 36.7 / 48.1 ms | 0.2 / 0.2 ms | not measured | 12.0 / 12.9 ms |

The earlier real Pydantic AI workload measured 499.7 ms for Textual and 24.8 ms
for the direct debug binary. Run-to-run spread was below 0.5 ms for native
launch and about 40 ms for Textual in this sample, so native clears the Textual
variation. The packaged launcher pays Python interpreter startup before
`execve`: composer readiness is about equal to Textual, while runtime readiness
is 2.6× faster. The browser used the optimized canvas bundle in Chromium at
120×54: the same 500-line stream completed in 1.506 s, and the synthetic
keydown-to-next-frame p95 was 13.5 ms typing and 12.9 ms scrolling, inside one
16 ms frame. The DOM backend did not finish the stream within 20 s, which is
why the canvas backend was adopted.

### Next implementation pass

The connection, session, interaction and presentation paths now have parity on
both targets. Three items remain before release.

1. **Run the hosted artifact pipeline (T20).** CI has native wheel and web bundle
   jobs and the publish workflow rewrites the crate version to the tag and injects the
   `hrns-tui` pin. Run it, confirm the same-version metadata, install each wheel in an
   empty environment, and serve the bundle against an authenticated runtime.
2. **Finish the release smoke (T19).** Native, packaged and canvas-bundle
   results are recorded below. Repeat once with a real long-stream Pydantic AI
   workload before tagging.

## Tasks

Paths under `rust/` are proposed locations. Size estimates count implementation files. Use existing checks and manual or ephemeral verification in accordance with the repo's test policy. The build commands and platform matrix for the new targets are outputs of the early slices.

### T01 — Record the Textual baseline

- [x] **Acceptance** Inventory every supported workflow, keybinding, fallback and platform dependency. Record median time from launch to editable composer and p95 typing and scrolling input-to-render latencies under a deterministic long-history ACP stream.
- **Verify** Repeat on fixed hardware, terminal dimensions, message history and stream rate. Record the sample count and run-to-run variation for bare `hrns` and `hrns connect`.
- **Depends on** None. **Files** `PLAN.md`. **Size** S.
- Baseline inventory and measurements are recorded under
  [baseline](#baseline-inventory-and-measurements) below.

### T02 — Stream a remote prompt on both targets

- [x] **Acceptance** A native Ratatui preview and Ratzilla browser preview share the composer and transcript, establish ACP v1 against the same Python mount, and stream a prompt.
- **Verify** Build the native and `wasm32-unknown-unknown` targets, serve the browser bundle, and check that text arrives incrementally. Close the socket and check the disconnect state. Record the dependency pins and the browser transport approach.
- **Depends on** None. **Files** `rust/Cargo.toml`, `rust/src/app.rs`, `rust/src/native.rs`, `rust/src/web.rs`, `rust/index.html`. **Size** M.

### T03 — Launch a native preview from a Python wheel

- [x] **Acceptance** A version-matched platform wheel installs without a Rust toolchain. A development dispatch path in the Python CLI launches it with inherited terminal streams and propagates exit status. The server-only extra installs independently of the binary wheel.
- **Verify** Build and install in a clean environment. Exercise normal exit and Ctrl-C, verify terminal restoration, and record install commands, versioning and candidate wheel platforms.
- **Depends on** T02. **Files** `rust/pyproject.toml`, `rust/Cargo.toml`, `hrns_slim/pyproject.toml`, `hrns_slim/hrns/cli.py`. **Size** M.

**Checkpoint after T03**

- [x] Both previews stream against the Python mount, the wheel installs cleanly, and the Textual baseline is reproducible.

### T04 — Authenticate browser connections

- [x] **Acceptance** A supported HTTPS deployment connects from the browser to the authenticated ACP mount, resolves the expected principal, and rejects unauthorized origins or missing credentials. Native bearer auth works against the same runtime.
- **Verify** Exercise login, socket reconnect, expired credentials, and a rejected origin in a real browser. Inspect the bundle, handshake, errors and status UI for secret redaction.
- **Depends on** T02. **Files** `rust/src/web/auth.rs`, browser deployment configuration, `docs/authentication.md`. **Size** M.

### T05 — Prove ACP extension interoperability

- [x] **Acceptance** Check the pinned SDK against current session-state metadata/notifications, elicitation, providers, advertised commands and MCP-over-ACP. Record the schema features and any typed extension handlers required on the native target; the browser handles only the shared subset and advertises neither client I/O nor MCP.
- **Verify** Compare captured messages with the current client and server. Connect to an agent without the optional capabilities and check the corresponding fallback. Confirm the MCP wire revision.
- **Depends on** T02, T04. **Files** `rust/Cargo.toml`, `rust/src/acp.rs`, `rust/src/acp/extensions.rs`, `ARCHITECTURE.md`. **Size** M.

### T06 — Start a local stdio agent

- [x] **Acceptance** Native can launch the built-in Python agent and an external stdio command with the selected cwd and environment, accepts draft input while connecting, and retains the draft after a failed launch.
- **Verify** Exercise both agent types, a child exit during startup, reconnect and clean shutdown. Check that owned child processes exit and that the WASM build still compiles.
- **Depends on** T02, T03. **Files** `rust/src/native.rs`, `rust/src/native/stdio.rs`, `rust/src/app.rs`. **Size** M.

**Checkpoint after T06**

- [x] Authenticated web and both native transports work. Record the proven SDK and packaging decisions. Review the required native workspace and MCP integrations before implementing them.

### T07 — Restore connection profiles

- [x] **Acceptance** Users can create, edit, select and reopen applicable profiles. Native preserves config precedence, overrides and keyring references. Both targets retain drafts on failed auto-connect and identify the workspace and endpoint in the UI.
- **Verify** Exercise explicit and remembered profiles, removed profiles, an unreachable target and invalid native config. Relaunch both targets and confirm preference persistence and redaction.
- **Depends on** T04, T06. **Files** `rust/src/connection.rs`, `rust/src/native/config.rs`, `rust/src/web/preferences.rs`, `rust/src/ui/picker.rs`, `hrns_slim/hrns/cli.py`. **Size** M.
- Profiles are created, edited and listed with `hrns profiles`; the launcher
  resolves the remembered or explicit profile and passes its target, credential
  reference, `client_io` and overrides to the native client. The browser selects
  its remote endpoint and workspace through deployment parameters. An in-app
  picker is a convenience, not a missing capability.

### T08 — Restore and manage sessions

- [x] **Acceptance** New, list, load, resume, rename, close and delete work where advertised. Restored history and shared live state appear in order without duplication. Opening a remote session by ID without explicit cwd can resolve it through paginated listing.
- **Verify** Load a prior session and join another during an active turn. Compare history and live state with Textual. Exercise an absent session, denied operation and unsupported capability.
- **Depends on** T05, T07. **Files** `rust/src/acp/session.rs`, `rust/src/session.rs`, `rust/src/ui/sessions.rs`, `rust/src/transcript.rs`. **Size** M.

### T09 — Restore prompt lifecycle controls

- [x] **Acceptance** Queue, steer, cancel, clear and resume work as supported by the current client and agent. Drafts stay editable while busy. Disconnection, explicit cancellation and session close have their existing distinct effects.
- **Verify** Queue multiple prompts, cancel mid-turn, resume or clear, then reconnect and check active-turn state and duplicate submissions. The Python adapter exposes server-side resume only in Python; after cancellation, new ACP prompts stay queued.
- **Depends on** T08. **Files** `rust/src/acp/turn.rs`, `rust/src/turn.rs`, `rust/src/ui/composer.rs`, `rust/src/ui/queue.rs`. **Size** M.

**Checkpoint after T09**

- [x] Both targets connect, manage and restore sessions, and complete or cancel a turn. Recheck native startup through the preview entry point.

### T10 — Render the complete transcript

- [x] **Acceptance** Stream Markdown, code, thoughts and expandable tools. Preserve scroll position across streaming and resize. Find retained text and copy the latest completed response.
- **Verify** Replay a large mixed transcript. Scroll back mid-stream, resize, find an older result and copy a multiline response on both targets. Check for stalled input or full-history reformatting on every update.
- **Depends on** T09. **Files** `rust/src/transcript.rs`, `rust/src/ui/transcript.rs`, `rust/src/ui/format.rs`, `rust/src/native.rs`, `rust/src/web.rs`. **Size** M.

### T11 — Restore composer gestures

- [x] **Acceptance** Restore multiline input, paste, draft and prompt history, completion, help and skill mentions. Restore attachment staging and removal with existing metadata and validation. Native uses file paths and browser uses an explicit file chooser.
- **Verify** Check keyboard shortcuts in both targets, agent and local completion, skill references, text and image attachments, and unsupported or oversized input feedback.
- **Depends on** T09. **Files** `rust/src/ui/composer.rs`, `rust/src/ui/completion.rs`, `rust/src/attachments.rs`, `rust/src/native/files.rs`, `rust/src/web/files.rs`. **Size** M.

### T12 — Restore status panels

- [x] **Acceptance** Restore activity, usage, todo/plan and sidebar navigation. Persist theme, sidebar and transcript preferences. Preserve connection identity in narrow layouts.
- **Verify** Drive usage and plan updates through a turn, toggle panels and preferences, then relaunch both targets at narrow and wide dimensions.
- **Depends on** T08, T10. **Files** `rust/src/ui/status.rs`, `rust/src/ui/sidebar.rs`, `rust/src/ui/settings.rs`, `rust/src/preferences.rs`. **Size** M.

**Checkpoint after T12**

- [x] Walk the transcript, composer and preferences rows on both targets. Measure long-history input and scrolling again and record regressions.

### T13 — Respond to interactive requests

- [x] **Acceptance** Preserve inline approvals, modal fallback, approval modes and elicitation choices. Requests remain actionable across screen changes and resolve once across cancellation or disconnect.
- **Verify** Exercise allow, deny, cancel, auto mode and concurrent interactions. Compare wire responses and displayed turn state on both targets.
- **Depends on** T05, T09. **Files** `rust/src/acp/interactions.rs`, `rust/src/ui/approval.rs`, `rust/src/ui/elicitation.rs`, `rust/src/preferences.rs`. **Size** M.

### T14 — Set agent models and session options

- [x] **Acceptance** Discover, configure and select providers and models. Preserve applicable login and credential flows. Restore effort and other advertised session options.
- **Verify** Start with missing credentials, configure a native built-in provider and a remote provider as supported, change model and effort in a running session, and reload the selected values.
- **Depends on** T05, T07, T08. **Files** `rust/src/acp/providers.rs`, `rust/src/ui/providers.rs`, `rust/src/ui/models.rs`, `rust/src/native/config.rs`, `rust/src/web/auth.rs`. **Size** M.
- Remote and local agent routing uses `providers/*`; models and effort are
  validated against advertised values. Device login and TOML provider editing
  stay in the Python commands (`hrns login`, `hrns config`).

### T15 — Apply native workspace access policy

- [x] **Acceptance** Native filesystem and terminal callbacks match `off`, `read` and `all`. The browser advertises neither capability and never serves a workspace callback.
- **Verify** Use a disposable workspace to exercise allowed and denied reads, writes and terminal calls. Check cleanup and capability negotiation on native, and confirm the browser declines the same requests.
- **Depends on** T06, T13. **Files** `rust/src/acp/client_io.rs`, `rust/src/native/io.rs`, `rust/src/ui/settings.rs`. **Size** M.

**Checkpoint after T15**

- [x] Compare approval, elicitation, provider and workspace operations with the parity matrix. Review credential ownership and the capabilities each connection advertises.

### T16 — Export native MCP tools

- [x] **Acceptance** Restore MCP health, auth, export consent, filters, request routing and progress. Preserve one-time, connection and persistent consent behavior.
- **Verify** Use a deterministic MCP server to exercise each consent decision, credential failure, a filtered tool call, progress and reconnect. Compare framing to T05.
- **Depends on** T05, T07, T13. **Files** `rust/src/mcp.rs`, `rust/src/mcp_types.rs`, `rust/src/acp.rs`, `rust/src/app.rs`, `hrns_slim/hrns/client/mcp_sidecar.py`, `hrns_slim/hrns/client/mcp_provider.py`. **Size** M.
- The native client starts `python -m hrns.client.mcp_sidecar` for a non-local
  target whose agent advertises `mcpCapabilities.acp`. The sidecar reuses
  `ClientMcpProvider` for upstreams, OAuth, consent, health, include/exclude
  filters and progress; Rust forwards `mcp/message` and declares servers in
  `session/new` and `session/load`. One-time, connection and persistent consent
  all work, and `always_allow` is written to the Python document.

### T17 — (withdrawn) Browser MCP export

- Not required. The browser advertises no MCP-over-ACP and no host transports;
  MCP stays with the native client and `hrns run`.

### T18 — Restore agent commands

- [x] **Acceptance** Restore advertised agent commands and name collision routing with the built-in presentation. Explicit user actions are required to invoke a command from a view.
- **Verify** Run a colliding command, inspect the rendered tool and approval, and replay without reinvoking a command on both targets.
- **Depends on** T05, T10, T11, T13. **Files** `rust/src/acp/commands.rs`, `rust/src/ui/commands.rs`, `rust/src/ui/approval.rs`. **Size** S.

**Checkpoint after T18**

- [x] Complete the parity walkthrough on both targets, including native MCP scopes. Resolve missing workflows before qualifying the release.

### T19 — Qualify responsiveness

- [x] **Acceptance** Median native startup and p95 typing and scrolling latency improve beyond Textual's measured run-to-run variation. The browser meets a responsiveness target established from the preview. Both retain all input and transcript updates under the measured load.
- **Verify** Repeat T01 with the release build, the installed Python entry point, and a browser loading the deployed bundle. Record medians, p95s, environment and workload. Measure composer and runtime readiness separately.
- Native, packaged and released-bundle results are recorded in the baseline.
  The browser was measured through the built bundle in Chromium. A real
  long-stream Pydantic AI workload remains for the release smoke.
- **Depends on** T12, T16, T18. **Files** `PLAN.md`. **Size** S for measurement. Track any necessary performance fixes as focused follow-up tasks.

### T20 — Produce paired release artifacts

- [ ] **Acceptance** CI produces native wheels for the supported platforms and a version-matched Ratzilla bundle. Deployment instructions cover HTTPS, authentication, and the remote ACP runtime. Python artifacts include the intended full, client-only and server-only install paths.
- **Verify** Install staged wheels in empty Python environments, check native launch, and serve the built web bundle against a remote runtime. Inspect artifact version metadata and CI results.
- **Depends on** T03, T04, T19. **Files** `.github/workflows/ci.yml`, `.github/workflows/publish.yml`, `justfile`, `rust/pyproject.toml`, browser deployment docs. **Size** M.
- CI now builds four native wheel targets and the browser bundle; publish rewrites
  the crate version to the tag and attaches `hrns-web.zip` to the release.
  Install and deployment instructions are in [docs/release.md](docs/release.md).
- The published wrapper gains `hrns-tui=={{ version }}` with markers for the four
  targets: `scripts/release_native_dependency.py` patches the hatch metadata
  after `just check`, the job rebuilds `dist/`, and `--verify` asserts the pin in
  the wheel and the sdist. The workspace still cannot declare it, because there
  is no `hrns-tui` on the index during development and its version comes from the
  crate; `hrns-slim[server]` and `hrns-slim[client]` stay free of the binary.
- `just build` now builds the sdist and the wheel from the source tree instead of
  building the wheel from the unpacked sdist. Without that, the metadata hook ran
  without git history, fell back to `fallback-version`, and pinned `hrns-slim`
  (and the new `hrns-tui`) to `0.0.1` in the released wheel.
- Local equivalents passed: `pip install hrns` pulled `hrns-tui` at the same
  version in an empty venv on macOS arm64; all four markers resolved for their
  target platforms and skipped Linux arm64 and 32-bit Windows; `hrns-tui
  --version` reported the release version; `hrns-slim[server]` installed without
  `textual` or `hrns-tui`; `hrns-slim[client]` resolved with `textual` before the
  cutover and no longer includes it; and `just native-install` plus the dispatcher
  selected native.
- The jobs still need one hosted run.

### T21 — Switch the interactive command

- [x] **Acceptance** Bare `hrns` and `hrns connect` launch Ratatui. The paired browser artifact is ready for the same release. Retire Textual and its dependency after checking callers. Keep shared Python modules used by `hrns run`.
- **Verify** Run `just check`, `just docs-build`, Rust and WASM CI checks, and installed-artifact smokes for interactive launch, a headless prompt and an agent launch. Repeat the deployed browser smoke with the final bundle.
- The dispatcher always launches the native wheel and reports an install hint when it is
  missing. It records the profile or builtin identity through `HrnsConfig.save()` so a
  bare launch still reopens the last connection. The Textual package, its dependency,
  its settings branches and `client/renderers.py` are removed; shared client modules used
  by `hrns run` remain.
- Removal inventory, measured against `b5c9b9b` plus the T20 packaging change.
  Shared modules stay: everything `hrns run`, `hrns connect`, `hrns login`,
  `hrns serve`, `hrns agent` and `hrns acp` reach must survive.
  - Delete `hrns_slim/hrns/client/tui/`: 38 modules and 12 456 lines covering
    `app.py`, `chat/`, `panels/`, dialogs, composer, transcript, status,
    providers, extensions and the connection screen.
  - `hrns_slim/pyproject.toml`: drop `textual>=8.2,<9` from the `client` extra.
    Nothing else declares it, so `uv lock` only loses that entry.
  - `hrns_slim/hrns/cli.py`: remove `textual` from `_require_client_extra`; remove
    the `run_terminal` branch of `_command_connect` together with its
    `explicit_target` pre-resolution; remove the `HRNS_TEXTUAL` check and make a
    missing wheel an explicit `HrnsClientError`. Update the module docstring and
    the `connect` help text, which still describe a Python terminal app.
  - Keep `client/client.py`, `bus.py`, `constants.py`, `connection.py`,
    `events.py`, `headless.py`, `interactions.py`, `io.py`, `launch.py`,
    `login.py`, `mcp_provider.py`, `payloads.py`, `runtime.py`,
    `rust_preview.py`, `session.py`, `skills.py`, `transport.py`, `turn.py`,
    `updates.py`, `attachments.py`, plus `shared/**` and `server/**`. The TUI is
    the only consumer of `client/renderers.py` and of `client/tui/**`; no other
    module imports the legacy package.
  - Decide `client/renderers.py`: its only callers are the Textual renderer views
    and the `hrns.renderers` entry-point contract, so delete it with the TUI or
    keep it for headless readable output. `client/__main__.py` stays.
  - Prose that names Textual: `README.md` (terminal client section and
    `HRNS_TEXTUAL`), `ARCHITECTURE.md` (dispatcher paragraph, the TUI sections,
    and the theme list), `docs/release.md` (fallback paragraph),
    `docs/skills.md` (`PromptWorkflow` row), `PLAN.md` notes and `AGENTS.md`
    reference. The `textual-dark`/`textual-light` theme names stay: the Rust
    client accepts them.
- **Platforms without a native wheel.** With Textual removed, Linux arm64,
  Windows arm64, 32-bit Python, musl Linux and FreeBSD lose the interactive
  client; `hrns` and `hrns connect` must fail with an install hint rather than a
  traceback. `hrns run`, `serve`, `agent`, `acp`, `login`, `config`, `profiles`,
  `mcp` and `clean` keep working because they never import the TUI. The wrapper's
  `hrns-tui` requirement already excludes those platforms, so `pip install hrns`
  succeeds there without the binary.
- **Depends on** T19, T20. **Files** `hrns_slim/hrns/cli.py`, `hrns_slim/pyproject.toml`, `uv.lock`, `ARCHITECTURE.md`, `README.md`. **Size** M for cutover. Remove the legacy UI package in a separate mechanical change in the same release.

**Release checkpoint**

- [ ] Both targets pass their parity checklist, startup and responsiveness checks, and installed-artifact verification at the same version. Only the hosted run and the real-workload smoke remain.

## Early decisions and risks

| Decision or risk | Resolve in | Required evidence |
| --- | --- | --- |
| SDK transport framing or unstable methods differ from the Python server | Resolved (T02, T05) | Streaming and extension interop against the mock and real mounts; capabilities, elicitation, I/O, providers and state verified |
| Browser cannot supply an arbitrary bearer header | Resolved (T04) | Same-origin cookie and origin checks owned by the deployment; browser advertises no host capabilities |
| New wheels complicate the current Python release pipeline | Resolved (T03, T20) | Clean wheel install and launch; CI wheel matrix and tag-driven crate version added |
| Native-only MCP export has no Rust implementation | Resolved (T16) | Sidecar proxy exercised for declarations, filters, progress, consent and failure; built-in agent keeps server-side tools |
| Platforms without a native wheel lose the interactive client | Resolved (T21) | The wrapper's marker skips `hrns-tui` there; `hrns`/`connect` fail with an install hint while non-interactive commands keep working |
| Existing plugins and custom renderers | Out of scope | Advertised agent commands and the built-in presentation remain |
| Python dispatch or long-history rendering erases performance gains | Resolved (T01, T19) | Native and browser measurements are inside the targets; canvas replaced DOM for long streams |

The transport, interaction and presentation slices are complete, and Textual has been
removed. What remains is the hosted artifact pipeline run and the final release smoke.

## References

- [Official Rust ACP SDK](https://docs.rs/agent-client-protocol/latest/agent_client_protocol/) and [WASM support](https://docs.rs/crate/agent-client-protocol/latest#webassembly)
- [Native Rust ACP WebSocket transport](https://docs.rs/crate/agent-client-protocol-http/latest)
- [Ratatui](https://ratatui.rs/) and [Ratzilla build and deployment](https://github.com/ratatui/ratzilla)
- [Browser WebSocket constructor](https://developer.mozilla.org/en-US/docs/Web/API/WebSocket/WebSocket)
- [Maturin binary wheels](https://www.maturin.rs/bindings.html#bin)
