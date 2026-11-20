# Architecture

## Distribution packages

`hrns` is the default distribution: a metadata-only wrapper depending on the same
version of `hrns-slim[client,server]`. Its `ag-ui` extra forwards to `hrns-slim[ag-ui]`.
`hrns-slim`, defined in `hrns_slim/pyproject.toml`, owns all code in `hrns_slim/hrns` and retains
the `client`, `server`, and `ag-ui` extras. Both distributions expose the `hrns` command;
Python imports remain `hrns`. Runtime version reporting reads `hrns-slim` metadata.

Both packages use Hatchling with the `uv-dynamic-versioning` version source and
metadata hook, following Pydantic AI's full/slim package structure. Versioning and
same-version dependency pins are declared in TOML. The tag pattern accepts this
repository's unprefixed version tags, with a `0.0.1` fallback when Git is unavailable.
The uv workspace resolves the local slim package during development;
`just build` builds both distributions for one release.

## CLI

All command-line entry points are built with [Cyclopts](https://cyclopts.readthedocs.io/).
Do not add `argparse`, Click, or Typer parsers.

- `hrns` — `hrns_slim/hrns/cli.py` is the one Cyclopts `App`. It registers the client
  commands (`connect`, `run`, `login`), the server commands (`agent`, `acp`,
  `serve`), and the shared commands (`config`, `profiles`, `mcp`, `clean`). It is
  exposed through `hrns.cli:main`.
- `hrns_slim/hrns/client/` holds the terminal, headless runner, transports, and the
  built-in agent launcher; it never imports the server package.
- `hrns_slim/hrns/server/` holds the Pydantic AI agent, capabilities, ACP adapter, and
  FastAPI mount.
- `hrns_slim/hrns/shared/` holds the configuration document, credentials, MCP-over-ACP
  binding, errors, and URL validation used by both sides.
- `hrns.server.runtime` also exposes its own Cyclopts `App` with `run` as the
  default command, so `python -m hrns.server.runtime` remains usable.

Commands are plain functions with typed keyword arguments; Cyclopts derives
options, types, defaults, and help text from those signatures. Parameter help is
declared inline with `Annotated[..., Parameter(help="...")]`. Each module exposes
a `main()` that calls its `App`, which keeps the `[project.scripts]` targets
unchanged.

`hrns.cli` uses `types.SimpleNamespace` as an internal argument container
for its command implementations, so the codebase has no `argparse` dependency at
all.

## Rust terminal preview

The preview in `rust/` shares its composer, transcript and ACP v1 client across
native Ratatui and Ratzilla. The protocol uses the official Rust ACP SDK. Native
launches stdio agents using the SDK and connects to remote agents with
Tungstenite. The browser uses its WebSocket API through `ws_stream_wasm`.
Both WebSocket adapters send one SDK JSON-RPC frame per text message through
the Python ACP mount. Host arguments and browser services stay outside the
shared application state. ACP work is driven on a native worker thread or
a browser task so the composer renders independently of connection setup.

Build the terminal with `cargo build --locked --manifest-path rust/Cargo.toml`
and the browser with `trunk build --locked --release` from `rust/` with
`wasm32-unknown-unknown` installed. The browser renders through Ratzilla's
canvas backend. The DOM backend stayed above 20 seconds for a 500-line stream;
canvas completed the same stream in 1.5 seconds, and synthetic keydown-to-next-
frame p95 was 13.5 ms for typing and 12.9 ms for scrolling. Canvas has no text
selection; `/copy` remains the clipboard path. The preview expects a remote ACP mount at
the same origin under `/acp/agent`. For local development, set the `endpoint`
and `cwd` page query parameters to a WebSocket URL and remote workspace.
Put any deployment authentication upstream of the browser connection. The
browser advertises no client filesystem, terminal or MCP capability and serves
no workspace callback; only its own file chooser and clipboard act on user
input. Deployments that need those capabilities use the native client.
The shared `/attach` command stages an embedded ACP resource when the runtime
advertises embedded context. The native frontend reads the supplied path away
from the event loop. The browser opens its file chooser, and the prompt carries
the chosen content rather than a path to the browser device.

Native advertises `clientCapabilities` from one mode: `off` offers nothing,
`read` serves `fs/read_text_file`, and `all` also serves writes and the
`terminal/*` lifecycle. Elicitation form support is always advertised on both
targets. Reads and writes are confined to the session workspace; version
control, `.hrns`, `.env`, secret and key paths are read-only. A terminal is
spawned in its own process group, its output is capped, and release or
disconnect kills the group. Both targets treat unsupported client requests as
`method_not_found`, and the browser's absent capabilities are never advertised.

The Python dispatcher always launches the installed native wheel; the release
installs it with the Python client. A source checkout without the wheel reports an
install hint instead of opening a Python terminal. The dispatcher resolves the target
and existing credential references before replacing the Python process on POSIX. It also resolves `client_io` with the
documented precedence (CLI, profile, `[tui]`) and passes the TOML theme and
permission mode as launch defaults. The native process
inherits the terminal and owns terminal restoration and the ACP connection.
The Rust state file is authoritative only for values the user changes in the
native UI; unchanged values keep following the Python document. `just
native-install` builds a development wheel with Maturin and installs it into the
project environment; it reports the Rust crate's preview version because the
release, not the tree, rewrites that version to the tag.

The published `hrns` wheel declares `hrns-tui==<version>` with markers for Linux
x64, macOS arm64/x64 and Windows x64, so `pip install hrns` installs the native
client at the same version while `hrns-slim[server]` and `hrns-slim[client]`
stay Python-only. The workspace cannot hold that requirement: there is no
`hrns-tui` on the index during development and its version comes from the crate.
The publish workflow patches the wrapper metadata with
`scripts/release_native_dependency.py` after `just check`, rebuilds `dist/`, and
asserts the result. See
[the release guide](docs/release.md#version-matched-native-dependency).

Approvals stay inline above the composer and show the tool kind, rendered
input, diff or terminal detail, and the available options. `1`-`9` select by
position; `y`, `a`, `n` and `N` map to the allow-once, allow-always,
reject-once and reject-always kinds. `Esc` cancels the request. Modes `ask`,
`reads` and `all` auto-approve nothing, read/search/think calls, or any request
that offers an allow option; the decision uses the kind reported by the agent,
never a tool name. Pending approvals and forms are cleared when their turn ends
or the connection drops, so a request resolves exactly once.

### Native MCP export

A non-local native target can offer configured MCP servers to an agent that
advertises `agentCapabilities.mcpCapabilities.acp`. The Rust client does not
speak MCP itself: it starts `python -m hrns.client.mcp_sidecar` with the resolved
configuration, and the sidecar reuses `ClientMcpProvider` to own the stdio or
HTTP upstreams, OAuth token store, consent policy, health, include/exclude
filters and progress. The two processes exchange newline-delimited JSON on
stdin/stdout; stdout is protocol-only and logs go to stderr. Rust declares the
approved servers in `session/new` and `session/load`, forwards `mcp/message`
requests to the sidecar, forwards request-scoped notifications back to the
agent, and shows consent and health in the UI.

Consent options are allow once, allow for this connection, always allow and
deny. The application keeps connection grants across reconnects and new
sessions; `always allow` writes `always_allow = true` through the Python
configuration document. A server that fails to start is reported by `/mcp` and
the sidebar, and the session continues without it. The built-in local agent
already loads `[mcp]` server-side, so the native client does not export to it;
the browser advertises no MCP capability at all. The flags are `--mcp-sidecar
PYTHON` and optional `--mcp-config PATH`; the Python launcher adds them for
non-local targets.

Agent forms (`elicitation/create`) render as a dock above the composer. A form
walks its fields in order: typing edits text and number fields, `1`-`6` choose,
toggle or advance a choice field, `y`/`n` answer booleans, `Enter` moves on and
submits the last field, and `Esc` declines. Multi-select fields toggle without
advancing. Required fields that are unanswered block submission with an inline
error. A form the client cannot render is declined rather than ignored, so the
agent receives a result instead of waiting forever.

Both frontends keep one action queue for the process lifetime and can retry the
transport after a disconnect. `Ctrl-R`, or the `/reconnect` command, starts
another attempt with the same transport options and connection identity.
`Ctrl-R` edits nothing, so the draft survives; `/reconnect` is an ordinary
command and therefore consumes the current draft line. The transcript and
queued prompts survive the attempt; connection-scoped session state, approvals
and in-flight turns are discarded, and the next `Ready` refreshes the session,
workspace and agent capabilities. Reconnecting never replays a submitted
prompt.

The same supervisor replaces a live connection when the composer asks for a new
or saved session. `/new` reconnects with the current workspace and `/load <id>`
reconnects with the saved session ID, which the client resolves through
paginated `session/list` when no workspace is supplied. `/close` and `/delete`
use the capabilities advertised at initialize; closing or deleting the current
session starts a new one, while acting on another session leaves the current
one attached. A switch opens a replay window: the client keeps the old
transcript until the new connection's `Ready`, then drops the old prefix so
restored history is not duplicated. A failed load rolls back to the old
prefix, and its error is reported after the restored transcript. New sessions
clear the transcript rather than replay it.

Transcript entries keep their plain text for search and copy. Agent and thought
entries render a small Markdown subset at draw time: fenced code, inline code,
emphasis, links and headings. User and note entries stay literal. ACP tool
calls are keyed by `tool_call_id`, merged from start and progress updates, and
shown with their current status. `Ctrl-O` expands or collapses their content,
diffs, terminal references and raw input and output. Rows are measured lazily.
Scrolling anchors to the entry and byte offset, so streaming, a width change or
toggling tool details keeps the reader near the same text; reaching the bottom
resumes follow mode.

Composer completion reads the draft. A first token starting with `/` offers the
local commands, while a `$token` offers user-invocable skills from the agent's
`hrns/skills` metadata. `Tab` accepts the selected row, Up and Down move the
selection while it is open, and Escape dismisses it before cancelling a turn.
Submitting a draft that names a catalog skill attaches the skill id and mention
span to the first text block's `_meta`, leaving the typed text unchanged for
the agent to expand. Paste, draft history and attachment staging stay shared;
native reads paths while the browser opens its file chooser.

`Ctrl-B` opens a sidebar with the connection identity, client I/O mode,
session title, model, mode, permission mode, usage, cost, plan, local queue,
skills and recent activity. `Ctrl-O` expands tool details and the sidebar
records tool transitions and turn outcomes. Both frontends persist sidebar,
width, tool expansion, reasoning visibility, theme and permission mode without
touching connection configuration: native writes JSON under
`$XDG_STATE_HOME/hrns/tui.json` (or `$HRNS_TUI_STATE`) and the browser uses
local storage. The sidebar is suppressed below 96 columns; the header keeps the
destination, status, title or workspace, model, usage and cost visible on
narrow layouts. `/theme`, `/permissions`, `/settings` and the `theme` and
`permission` page parameters expose the same values. The built-in themes are
`hrns`, `textual-dark`, `textual-light`, `ansi-dark` and `ansi-light`.

Remote provider routing uses the `providers/list`, `providers/set` and
`providers/disable` methods. `/providers` reads the live advertisement and
`/provider set` or `/provider disable` applies a change for the connection.
Models and effort remain session config options; a value must be one of the
advertised choices. Device login and TOML provider editing remain Python
commands (`hrns login`, `hrns config`) rather than being duplicated in Rust.

### ACP SDK pin and extension surface

The Rust client pins `agent-client-protocol = 5.4.0` (schema 1.19.0) and uses
the stable v1 types. It sends `clientCapabilities` and reads the agent's
`loadSession`, prompt, MCP and session capabilities at initialize. It handles
`session/update` for user, agent and thought chunks (including non-text blocks
as labels), tool calls and updates, plans, session titles, current mode, config
options, available commands and usage with cost. It reads `_hrns/session/state`
as a notification and from the `session/new` or `session/load` `_meta`, along
with `hrns/skills`. It sends `hrns/skills` mentions on the first prompt text
block. It answers `session/request_permission`, `elicitation/create`, `fs/*` and
`terminal/*` through the active session stream, which is where the SDK routes
session-scoped requests. Provider methods and extension requests use the SDK's
untyped message type because the stable schema does not expose them.

The server advertises no `available_commands_update` and no `session/resume` or
rename method. `/agent` therefore stays empty unless an agent advertises
commands, and `/rename` reports that standard ACP has no rename operation.
`session/delete` returning `-32002` is treated as success. Listing scopes to
the current workspace. Titles arrive through `session_info_update` and the
server's derived title.

## ACP dependency

Both the client and server extras require the stable 1.x `agent-client-protocol`
SDK (`>=1.0.0,<2`), which provides the full ACP v1 method table, including
`session/delete`.
`hrns` implements the adapter itself in `hrns_slim/hrns/server/acp/`, derived from the design
of `pydantic-ai-harness`'s experimental ACP adapter but targeting ACP 1.x directly.

`pydantic-ai-harness`'s `[acp]` extra still declares `agent-client-protocol<0.12`,
so `hrns` depends on the base package and declares ACP directly; the extra
contributes only that one dependency. Harness is still used for the coding
capabilities (`Coder`, `FileSystem`), not for ACP. Remove the split once harness
supports 1.x.

The adapter implements the complete `acp.Agent` protocol. `session/list` and
`session/delete` are backed by `ListableSessionStore`/`DeletableSessionStore`, and
`providers/*` plus the `model` and setting config options are backed by a `ModelResolver`
the caller supplies. MCP-over-ACP is opt-in through attachment configuration; NES is unsupported.
ACP's own `fs/*` and `terminal/*` are the same kind of opt-in: the client's
`client_io` only advertises them, and an agent installs `acp_filesystem` /
`acp_terminal` through `attachment_config` to expose `read_file`, `write_file`, and
`run_command`. `examples/fastapi_app.py` wires both; the default `hrns agent` does
not, so its `Coder` works on the server workspace instead of the client's.

## Server-owned sessions

`AcpServer` (`server/acp/host.py`) owns factory-only registrations. A factory runs
once for each new/restored live session, receives `SessionContext(agent_id,
session_id, cwd)`, and returns an `AbstractAgent` directly or through an awaitable.
The registration assigns new session IDs before invoking the factory; restoration
uses the stored ID and cwd. Factory resolution precedes `session_config`.
`RegisteredAgent` provides typed session
handles; each `Session` owns its agent runtime, shared FIFO, turn handles, history,
configuration, and pending decisions. Registration freezes at startup. Explicit
`async with server` owns shutdown: reject new work, cancel/join active work, resolve
queued handles, close attachment delivery and observers.

`SessionContext`/`AcpSessionConfig` configure server dependencies, capabilities,
toolsets, and workspace once, without a client. `AcpSession`/`AcpAttachmentConfig`
configure only connection-owned toolsets on each ACP attachment. The wire adapter
in `acp/adapter.py` delegates to the session runtime in `acp/runtime.py`; stdio,
network ACP, and programmatic submission use this same execution path.

`server.mount_acp(app, prefix=...)` mounts `WS {prefix}/{agent_id}`. There is one
active ACP attachment per session. Its owner may reload and replay on the same
connection; another connection remains rejected. Reload serializes attachment setup
and retires the old delivery task before replay, retaining the owner's connection generation
and pending interaction requests while refreshing its declared toolsets. Load reuses a live session or restores it once
under the registration lock, validates stored cwd, and installs a delivery
queue before replaying completed/current-turn updates. No network I/O is performed
under the registration lock. Disconnect removes the attachment, documents, and
client toolsets; accepted work survives. Close explicitly cancels and closes the
session. The default store is separate per registration; the same store instance
cannot be registered twice. Shared persistence requires explicit agent partitions.

`Session.submit()` queues a separate turn. `Session.steer()` and existing ACP
`session/prompt` requests join an active turn at the next model request. A per-run
`Hooks.before_node_run` hook forwards pending content before `ModelRequestNode`
through Pydantic AI's native `RunContext.enqueue()` with its default `asap`
priority. Ongoing tools finish before that boundary; native delivery stamps and
persists the injected messages. Consumed steering handles share the active turn's
result, usage, cancellation, or error.
Messages left unconsumed at turn completion become separate follow-ups ahead of
explicitly queued work. Idle steering starts a normal turn. ACP wire types and
methods are unchanged.

Cancelling an awaiter never cancels execution. Cancel and runtime errors pause
remaining work; Python inspection/resume/clear manage separate queued turns.
Accepted prompts survive disconnect, their JSON-RPC associations do not, and no
reconnect resubmits them. The TUI sends concurrent prompts without cancelling
earlier request workers. `/queue <message>` holds a follow-up and staged attachments
in the TUI until idle; the command and queue controls are entirely client-side.

`RegisteredAgent.cancel_session(session_id)` resolves a session through `get_session`
and delegates to `Session.cancel()`. It shares lookup/restoration and unknown-ID
errors with `get_session`, cancels the active turn, and pauses queued work without
closing the session or clearing pending prompts.

`RegisteredAgent.close_session(session_id)` resolves through `get_session` and
delegates to `Session.close()`, sharing its lookup/restoration and unknown-ID errors.
Closing cancels active work, clears queued prompts, detaches delivery, closes observers,
and removes the live session and view. Stored history remains available for restoration.

`interactions.py` retains unanswered decisions while disconnected, reissues after
attachment replay, and ignores responses from departed generations. A connected
client without form support gets the existing unavailable-question result. Client
toolsets are selected through upstream `DynamicToolset` at each model step;
`AttachmentToolset` rejects dispatch against an old attachment. An uncertain lost
result is reported rather than automatically replaying side effects. Provider
resolver mutations apply to the registration and notify every affected live session.
A resolver instance cannot be reused by two registrations.

Initialize advertises `_meta["hrns/session/state"]`. Load metadata and the
`_hrns/session/state` notification carry revision, active turn, running/waiting/idle,
last outcome, and last usage. The existing client uses them for server-started turns
and suppresses duplicate lifecycle events from its own prompt response. They do not
expose queue management. Successful close/delete clears that session's client revision
and active-turn caches, so restoring its saved state can begin a new revision sequence.

## Read-only AG-UI

`HrnsServer(AcpServer)` composes `server/ag_ui.py` observation with the native runtime.
The base server has no AG-UI imports. The `ag-ui` extra installs Pydantic AI's public
AG-UI APIs (minimum 2.57.0, exercised offline at that version).

`mount_ag_ui` independently mounts GET snapshot and SSE routes under
`{prefix}/{agent_id}/sessions/{session_id}`. They observe resident sessions and never
start execution. HTTP and WebSocket admission use the same `HTTPConnection` callback
contract; AG-UI handlers accept `HTTPConnection` for authentication. Read-only semantics are
application/OpenAPI metadata, not an invented AG-UI capability; mutations return 405.

Authentication resolves an application-owned principal once per ACP connection and
once per AG-UI HTTP request. Session access and visibility belong to the registration's
store: `create`, `load`, `list_sessions`, and `delete` receive the principal directly.
`AcpServer[PrincipalT]`, registrations, adapters, and `SessionStore[PrincipalT]`
preserve its type. Store inputs use `PrincipalT | None` to model anonymous admission;
authentication returns `PrincipalT | None`, with `None` rejecting a configured callback.
The runtime depends only on the trusted `SessionPersistence.save` protocol.
`load` also receives a `SessionAccess` operation, allowing separate permissions for
loading, prompting, cancellation, settings, deletion, document changes, and observation.
It runs before resident-session lookup as well as restoration, so the in-memory cache
cannot bypass the application's permissions. Session creation passes the assigned ID,
resolved cwd, and principal to `store.create` before invoking the agent factory.

Listing delegates to the principal-aware store, which filters before pagination and
returns its own cursor. Harness performs no per-result authorization pass. `save` is
trusted runtime persistence without a caller principal: accepted work and persistence
outlive ACP connections. Direct Python session handles are trusted server objects;
registration creation/lookup and explicit ACP adapters can supply a principal. Stdio
also accepts a host-provided principal. The default in-memory store permits access
for every principal; ownership, roles, and database queries belong to the application.

A store `PermissionError` returns Harness ACP error -32003 or HTTP 403. Returning
`None` from `load` hides a session as unavailable (ACP invalid parameters / HTTP 404).
Failed HTTP authentication returns 401; failed WebSocket authentication closes with
1008 before ACP starts. Denial responses omit exception details. Permissions are checked
when each operation is accepted; accepted prompts and open observation streams continue
if permissions later change. Unsupported operations stay unsupported, and provider
routing retains registration scope. See `docs/authentication.md` for the public contract.

Each native run passes through one `AGUIEventStream` converter and encoder, with
server-assigned conversation/native-run IDs. Its native events also feed ACP.
Deferred decisions split a turn into native runs; session-level `STATE_SNAPSHOT`
reports waiting without claiming application-turn completion. No request adapter or
`RunAgentInput` is mounted.

A join atomically captures the current-run converted prefix and installs its live
queue on the event loop. Each native run starts with `RUN_STARTED`, then its message
baseline and session state, followed by converted events. The final reconciled message
snapshot precedes `RUN_FINISHED` or `RUN_ERROR`; no snapshots escape that run boundary.
Active subscribers replay this same sequence before reading their live queue. Idle
joins receive messages and state within a short observation-only `RUN_STARTED` /
`RUN_FINISHED` envelope with a fresh run ID. Idle state updates use the same envelopes;
these protocol runs hydrate observers and never execute an agent.

History conversion uses
`AGUIAdapter.dump_messages`; observer-only response-order/tool-call identity tables
reconcile live IDs with dumped messages. Completed display history is cached. Native
cancellation snapshots preserve partial display without committing it to model
history or persistence. No custom partial-delta reducer is implemented.

The current native-run encoded prefix grows with event volume and is released at
completion. Subscriber queues hold 256 events; overflow closes the subscription for
snapshot/replay reconnect instead of dropping deltas or blocking execution. ACP delivery uses a separate queue of transcript-update references. Idle SSE emits keep-alive comments.
Server/session shutdown closes subscribers and pending deliveries.

Deployment is one process/worker per runtime. Stores may recover committed state
after restart; live tasks, queues, pending decisions, partial display history, and
subscriptions are not durable. Cross-worker ownership/fan-out is outside this API.

## MCP over ACP

`hrns_slim/hrns/shared/acp_mcp/` implements the stateless MCP-over-ACP binding for MCP
2026-07-28. It targets the current ACP RFD, not the Python SDK's older
`mcp/connect` / `connectionId` prototype: every `mcp/message` carries a
`serverId` and a logical `requestId`, and each request carries its MCP version
and client capabilities in inner `_meta`. The binding is standard ACP; no
`_hrns/*` extension is used.

The module has two roles. `McpOverAcpProvider` is the client side: it owns
`serverId` registrations, validates envelopes and inner metadata, dispatches
requests, tracks active IDs, maps inner MCP errors to the response carrier and
binding failures to outer ACP errors, and forwards request-scoped
notifications. `McpOverAcpCaller` is the agent side: it sends requests, adds
the required per-request metadata, routes notifications by `(serverId,
requestId)`, and turns cancellation into `$/cancel_request`.
`from_mcp_server` adapts an official `mcp` SDK server (`MCPServer` or the
low-level `Server`) through `mcp.server.runner.serve_one`, so the MCP SDK owns
discovery, capability checks, tool/resource/prompt handlers, and
input-required results.

The Python SDK's generated schema still has the stateful prototype and
validates `connectionId` before application handlers run. `router.py` is the
narrow compatibility shim: it replaces the two `mcp/message` routes on the
SDK's `MessageRouter` and sends raw stateless frames through the same
connection. It is isolated so it can be removed when the Python SDK catches up
with the RFD.

On the client, `ClientMcpProvider` reads the `[mcp]` servers with
`export = true`, launches the stdio command or connects to the HTTP/SSE URL,
and serves the upstream session through `from_mcp_server`; the client includes
the resulting declarations in `session/new`. On the agent, `acp_mcp_toolsets`
turns every client declaration into an ordinary `MCPToolset`: standard
stdio/HTTP/SSE entries connect directly, while `type: "acp"` entries use
`AcpMcpClientTransport` over the session's caller. `hrns agent` installs that
attachment config and advertises `mcpCapabilities.acp`.

Upstream revision is best-effort, not a second supported binding.
`build_client` connects with `mode="auto"`, so FastMCP probes
`server/discover` and falls back to the `initialize` handshake; a
handshake-era upstream keeps working for the methods the proxy translates.
The proxy mirrors the upstream's advertised capabilities, so a server without
resources, prompts, or completions is not advertised with them downstream, and
the negotiated revision is logged (legacy with a warning). Reverse requests
(`sampling/createMessage`, `elicitation/create`, `roots/list`) are not
bridged: they are not advertised, and the MCP SDK answers them with
`INVALID_REQUEST` because MCP 2026-07-28 replaced them with MRTR.

Export is also gated by the client's user, not only by configuration.
`export = true` makes a server eligible; at session start the client asks whether
the agent may use it (allow once, allow for the connection, always allow, deny)
before the declaration goes into `session/new`, `session/load`, or
`session/resume`. A denied server is left out of the declaration, and an
unapproved server is never started. `always_allow = true` skips the prompt and
is the only way a non-interactive client (`hrns run`) exports, because it cannot
ask and therefore denies. The TUI renders the prompt through `ChoiceDialog`; the
CLI leaves the consent handler unset.

Binding internals and subscription requirements are documented in
`docs/mcp-over-acp-analysis.md`; remaining work is tracked in `TODO.md`.

## ACP models and provider configuration

One object answers everything ACP asks about models: `ModelResolver`
(`hrns_slim/hrns/server/acp/models.py`). ACP has no model methods — models are a session config
option — and provider routing is three methods that reroute them, so the two live
together. `list_models()` supplies the `model` option and the ids a client may select,
`resolve()` turns a selection into a runnable model and the settings that choice needs,
and `list_providers()`/`set_provider()`/`disable_provider()` back `providers/list`,
`providers/set`, and `providers/disable`. `resolve` reads the same state `set_provider`
writes; a resolver that did not would make a configured route a lie. `ResolvedModel`
carries the model plus model settings because client-supplied headers reach a request as
`extra_headers` rather than provider constructor arguments.

The adapter advertises both the model option and the `providers` capability exactly when a
resolver is supplied. `ProviderModelResolver` is the base a resolver subclasses: it owns
the provider state (`InMemoryProviderRegistry`) and implements `resolve` so the model's own
provider config is what builds it, leaving `list_models` and `build_model` to the subclass.
`RuntimeModelResolver` in `hrns agent` seeds it from the TOML provider entries, derives model
ids from `settings.model_targets()`, and raises `ProviderDisabled` instead of silently
falling back to a configured endpoint.

ACP's provider payload reports routing endpoints, not the models behind them, so the
model→provider link travels separately: each `ModelChoice.provider_id` becomes
`_meta.providerId` on the model option. `hrns agent` derives it from the `entry:model` id;
the client's `/providers` table groups the option's values by that tag, and `/models`
appends it to any model name that does not already read `provider:model`.

The terminal can open Local mode before a provider and model are selected. It launches
the built-in ACP runtime in setup mode with a model-free, in-memory session. The chat
composer accepts `/provider` (also `/providers`) and `/models` to configure the agent.
Selecting a model saves the configuration and starts the configured runtime with a new
session. Headless prompts still require a configured model.

The local and ACP model pickers bound the dialog to the terminal height and give
the option list the remaining rows. The list owns vertical scrolling so the
header, local filter, and keyboard hints stay visible with long model catalogs.

## Session settings

Settings are the same object's second surface, because they have the same owner as models
and routing. `list_options(model_id)` advertises the non-model options a deployment allows
for a model — the shipped effort ladder is a `thought_level` select built by
`RuntimeModelResolver` — and `resolve_settings(model_id, setting_selections)` turns session
selections into a `ModelSettings` **delta**. The adapter merges that delta over
`ResolvedModel.settings` and the agent's own settings on every model request: passing a
callable `model_settings` is what lets `session/set_config_option` land on the next request
of a running turn, not only on the next turn. The delta shape also keeps the client out of
credential-bearing keys such as `extra_headers`, which routing uses.

Selections live in `SessionState.setting_selections`, are persisted in `StoredSession`, and
are revalidated whenever the model or a provider route changes; a value that disappeared is
dropped so the option falls back to its `default`. `providers/set` and `providers/disable`
snapshot the options before mutating resolver state and send `config_option_update` to any
resident session whose list changed, because nothing else tells such a client its choices
shrank.

`hrns agent` reads the policy from `[thinking]` (`levels`, `allow_off`) and intersects it
with the model's profile via `infer_model_profile`, so a model that cannot reason gets no
option and an always-on model does not offer `off`. The built-in runtime uses the
provider's default effort until the session selects a level.

The terminal saves the selected effort in `[local_model_effort]` for Local mode and
`[profile.<name>.model_effort]` for named connections. Each key is the model id advertised
by the agent, such as `"work-openai:gpt-5.2"` for the built-in runtime. The terminal
restores a saved value through ACP after creating a session or switching models if the
agent still offers that value. A built-in model switch without an applicable preference
uses the provider default. Loaded sessions retain their stored ACP selections, and
external agents supply the session's effort when there is no applicable saved preference.

## Usage reporting

Two channels carry usage, and they answer different questions. `PromptResponse.usage`
is per-turn token totals — the protocol has no field there for a context percentage or a
price, because `acp.schema.Usage` is tokens only. The context gauge is
`session/update` `usage_update`, whose `used`/`size`/`cost` the client renders as
"ctx 12k/200k (6%)" and the session's spend. An agent that sends only `PromptResponse`
leaves the client's status band blank, which is why the adapter emits the gauge at the
end of every prompt.

It also emits it as the turn progresses. A request that ends in a tool call has already
produced its tokens, so the adapter sends the gauge when the tool call starts: the readout
follows the work instead of appearing only when the prompt returns, which for a long
tool-using turn is minutes after the person started watching it. `used` is that request's
tokens, and `cost` is the committed session total plus the run so far, so the spend moves
while the turn runs and is corrected to the exact total when it commits. A gauge identical
to the one already sent is dropped rather than repeated for every parallel tool call.

`used` is the last request's input, cache-read, cache-write, and output tokens: cache
tokens are part of the prompt, and the response becomes context for the next request.
`size` comes from the resolved model's profile `context_window`; when the profile has
none (arbitrary OpenAI-compatible endpoints usually do not), no gauge is sent rather than
a bogus percentage, and the client falls back to the turn's token totals. `cost` is
session-cumulative and priced by `genai-prices` through `RunUsage.cost`, so it is USD and
absent for models pricing data does not know.

The gauge is sent straight to the connection rather than recorded in the session
transcript: replaying a stale percentage into a loaded session would describe context
that session does not have.

## Session titles

A session's name is derived, never stored separately: `title_from_updates` takes the first
user text in its transcript, collapses its whitespace, and truncates it to 80 characters.
`session/list` reports that derivation for stored sessions, and the runtime announces the
same one with `session/update` `session_info_update` once a turn commits, so a client's
session list and its status band cannot disagree about what a session is called. A session
whose transcript holds no user text — before the first prompt, or one that carried only
attachments — is untitled in both places rather than named after its id.

Like the usage gauge, the title is sent to the connection and not recorded in the
transcript: it is a function of the transcript, so a restoring runtime derives it again and
re-announces it right after the replay `session/load` and reconnects perform. A title that
has not changed is not repeated on later turns.

## Connection identity in the TUI

The bottom band names the far side of the connection so a remote session can never be
mistaken for a local one. `ConnectionTarget.location()` reports the remote *workspace*
under `cwd` and the endpoint under `server`, and the band's left context deliberately
keeps showing the workspace: that is what makes a remote path legible as remote. The
endpoint therefore lives in the pinned right cluster (`StatusBar.show(server=..., remote=...)`),
where it cannot be displaced by the activity label mid-turn.

The endpoint degrades rather than disappearing, in this order: full URL, scheme
stripped, `host:port`, then dropped. Query strings and fragments are removed before any
shortening so a token in the URL never reaches the screen. When the cluster still
overflows, fields drop in `StatusBar._right`'s order, and the endpoint outlives
everything but the model name. Local sessions put the profile name in the same slot, so
the slot never implies a remote connection that is not there.

## One configuration document

`hrns_slim/hrns/shared/config/settings.py` holds `HrnsConfig`, the single pydantic-settings model both
sides of the project read. The alternative — a client model for `profiles.toml` and a
runtime model for `hrns.toml` — meant two schemas, two loaders, and two answers to
"what is configured?"; a provider entry a client could not see, or a profile the runtime
rejected as an unknown key. One document removes that class of bug: a profile and a
provider are validated by the same `extra="forbid"` model, so a typo fails both programs
with the file and field named.

Sections carry ownership rather than a second file: `[profile.<name>]` and `[tui]` are the
client's, `provider`/`model`/`[providers.<name>]`/`[model_settings]`/`[mcp]`/`store` are
the runtime's. Neither side reads the other's sections, but both parse them, because a
model with unknown keys is a typo detector.

Precedence is expressed as pydantic-settings source order — init, environment, project
document, user document, defaults — which deep-merges each source over the next, so a
project `hrns.toml` overrides exactly the keys it names and leaves the rest of the user
document intact. `--config PATH` (or `HRNS_CONFIG_FILE`) collapses that to a single
file and is a contract: a selected file that does not exist is an error, not a silent fall
back to defaults. The environment prefix is `HRNS_`; `HRNS_MODEL` and `HRNS_STORE`
are read by every entry point, so the same variable means the same thing in the
terminal, the built-in agent, and the HTTP server.

`Profile` and `TuiSettings` are plain models inside the same document, so `hrns` needs no
facade: `HrnsConfig.profile` is the profile map the dispatcher resolves, `HrnsConfig.tui`
seeds the terminal's theme, permission mode and client I/O, and `HrnsConfig.save()` writes
the whole effective document to the user file (defaults omitted). The native client keeps
changes it makes in its own state file; MCP `always_allow` consent is written through
`HrnsConfig.save()`. Project documents are read, never written.

## Launching the terminal

Bare `hrns`, `hrns connect` and the connection flags enter the Python dispatcher. It
resolves the target and existing credential reference, records the connection identity,
and replaces itself with the installed `hrns-tui` executable on POSIX. The native process
owns the terminal, the composer, the ACP connection and terminal restoration. A missing
wheel is an install error: there is no Python terminal fallback. Platforms without a
native wheel keep every non-interactive command but cannot open the full-screen client.

The native client draws an editable composer immediately and connects on its worker
thread. The draft stays in place while startup is in progress and moves into the session
when it opens. A failed auto-connect leaves the draft and the disconnected state visible;
`Ctrl-R` retries with the same target. The browser runs the same shared state against an
authenticated remote mount.

A bare `hrns` reconnects to the connection this terminal used last instead of always
defaulting to the built-in model. The identity is one string in the document's `[tui]`
section, written by the Python dispatcher through `HrnsConfig.save()`: `builtin` for the
built-in runtime, a profile name for a saved stdio or remote target. An ad-hoc
`--server`/`--stdio` command line is not remembered, because it belongs to no file and
there would be nothing to reopen. The identity never carries a working directory: a
reopened connection starts where the new launch stands, or where its profile says.

A missing remembered profile or an unreachable endpoint opens the native client with the
reason in the transcript. A profile deleted from disk is forgotten. `_remembered_target()`
in `cli.py` owns the identity→target mapping and only the interactive command uses it;
`hrns run` takes its target from its own arguments every time.

When `hrns connect --session ID` opens a remote session without `--cwd`, the native client
finds the session's stored cwd through unfiltered, paginated `session/list` before
loading its history. This cwd also becomes the connection's displayed workspace.
An explicit `--cwd` goes directly to `session/load`, where the server validates it
against the stored cwd. If listing is unavailable or the session is absent from
the listing, the client asks for `--cwd`. New sessions retain profile and local
cwd defaults.

## Todos

`hrns.server.capabilities.Todo` is a standalone Pydantic AI capability with two tools:
`read_todos` reads the ordered list; `write_todos` replaces it completely (an empty
list clears it). `TodoItem` has content, priority (`high`, `medium`, `low`), and
status (`pending`, `in_progress`, `completed`). Items are immutable snapshots.
Static instructions explain the workflow; there is no model-request reminder injection.

```python
from pydantic_ai import Agent
from hrns.server.capabilities import Todo, TodoStore

# Fresh state per standalone run:
agent = Agent("provider:model", capabilities=[Todo()])

# Explicitly share state across standalone runs in one conversation:
store = TodoStore()
conversation_agent = Agent("provider:model", capabilities=[Todo(store=store)])
```

Writes emit a typed `TodoUpdated` capability event through `RunContext.emit`.
Consumers of `agent.run_stream_events()` can inspect `event.items` without ACP.
The ACP adapter translates these events into standard `AgentPlanUpdate` full
snapshots through its existing transcript/update path. The client already maps
these notifications to `PlanChanged` and renders them in the Todos panel.

The adapter binds a session-owned `TodoStore` during each run using task-local
context, without mutating the shared agent. That binding takes precedence over
an explicitly supplied standalone store. Nested runs use separate state. Each
session retains todos across prompts; `StoredSession.todos` preserves them across
load/reconnect when a session store is configured. Published changes from an
interrupted turn are saved without committing its incomplete model history.
Durability across process restarts depends on the chosen session-store backend;
the example's `InMemorySessionStore` is process-local.

`build_agent()` and `examples/fastapi_app.py` enable `Todo()`. The default approval
policy permits both todo tools because they only change planning state. Agents
without the capability receive no todo tools. The client displays the list;
the agent controls it.

## Skills

`hrns.server.capabilities.Skills` supplies a host catalog, rendered user invocations,
and ordered library precedence. `Skills(skills=...)` accepts Python-defined skills,
using the [SkillsCapability API](https://dougtrajano.github.io/pydantic-ai-skills/api/capability/)
keyword. The capability uses the same discovery and bundled-file machinery for model
loading and host invocation.

1. **Host catalog.** `catalog()` reads configured libraries through a workspace backend
   and caches the result on the capability instance. `metadata()` maps the definitions
   and Python skills to `SkillInfo`. The adapter advertises these records on session
   creation and load so clients can offer `$name` completion.
2. **Rendered instructions.** `instructions()` renders the body, substitutes directory
   placeholders, and appends the bundled-file inventory.
3. **Library precedence.** Explicit paths come first, then project and user libraries.
   `_build_harness_skills` selects `duplicate_names='keep_first'` and
   `_adopt_first_package` assigns the winning library's bundled files. Review these
   integration methods when upgrading the skills dependency.

The catalog and prompt references use ACP `_meta["hrns/skills"]`. The client sends a
skill ID; the server validates it against the session catalog and prepends instructions
as user content in a `<skill_content name="…">` block. The catalog is a session snapshot;
new sessions use fresh capability instances, while loading a resident session retains
its cached catalog.

`Skills.for_run` marks user-invoked leaves active with `defer_loading=False`.
`active_capability_ids` enables their bundled-file tools. The invoked set is held on
session state and saved for restoration. Model invocation uses `load_capability`.
The default approval policy permits resource reads and requests permission for script
execution. The adapter locates the capability through `Agent.root_capability`.

## Ask user

`hrns.server.capabilities.AskUser` wraps the harness capability and adds one default: the
tool is `ask_user_question` and every call is deferred. It takes one to ten
questions, each with a short header, the question text, two to six options, and a
`multi_select` flag. The harness owns the schema and validation; this repository
owns the transport.

`build_agent` uses `AskUser()`; the subclass defaults `answerer=None`, so every call
is deferred: the tool raises `CallDeferred` and the run ends with
`DeferredToolRequests.calls`.
`PydanticAIACPAgent._resolve_calls` handles that batch in the same turn loop it
already uses for approvals. For an `ask_user_question` call it rebuilds the request
with `AskUserRequest.from_tool_call`, sends ACP form elicitation
(`elicitation/create`) only when the client advertised `elicitation.form`, and feeds
the answer back through `DeferredToolResults.calls`. `ask_user_result` validates the
response against the request, so a client cannot answer a question that was not
asked or pick a label that was not offered.

One question maps to one form property: a single-select is a string with `oneOf`,
a multi-select is an array with titled items, and a typed answer is a companion
string field (`q0_text`). The schema is standard ACP: no marker and no vendor
fields, so any client that implements form elicitation can render it. The
`qN`/`qN_text` names are private to the adapter, which uses them to put the answer
back together; a client only sees ordinary form properties.

The TUI folds the `qN`/`qN_text` pair back into one question for display: each
question is a vertical radio or checkbox list and the typed-answer input is its last
row. The list receives focus as soon as the bar mounts, Up/Down move the option
cursor, Space toggles a multi-select option, and Enter submits a single question.
For a multi-select the custom field is an alternative, not an extra label: choosing
or typing it clears the picked labels, because the ACP answer model forbids a custom
answer and selections together. When there is more than one question, each is a tab
and a final Confirm tab reviews the answers; Enter advances to the next question,
Tab/Shift+Tab move forward and back, and only Enter on Confirm submits. The pairing
is only a presentation concern; the adapter still receives ordinary `qN` and
`qN_text` properties, so other clients may render the two fields separately.

`UiInteractionPolicy` sends every form elicitation to the active `ChatScreen`, which
mounts an `ElicitationBar` above the composer and removes it when the user accepts or
declines. URL elicitations, and forms that arrive with no chat screen on top, still
use the modal `ElicitationDialog`.

A decline, a cancel, an empty form, or a client without form support returns a
plain string telling the model to proceed and state its assumption. None of them is
a tool error. `ask_user_question` is in `READ_ONLY_TOOLS`: the question is the
interaction, not a side effect, so it is never wrapped in a permission prompt.

## Documentation branding

`docs/assets/hrns.svg` is the shared logo for the documentation and both package
READMEs. The READMEs use its raw GitHub URL so the image also renders on package
indexes. Zensical uses the same SVG for the header and favicon; the home page
includes a larger version. `zensical.toml` defines light and dark palettes that
follow the system preference by default and offer a system/light/dark toggle.
`docs/stylesheets/extra.css` maps the logo's purple, lavender, and near-black colors
to theme variables, with darker links on light surfaces and lighter links on dark
surfaces.

Zensical's native `llmstxt` support generates `llms.txt`, Markdown copies of the
public documentation pages, and `llms-full.txt` during the normal docs build.
The section lists in `zensical.toml` select the public pages and their ordering;
links use the configured documentation site URL.
