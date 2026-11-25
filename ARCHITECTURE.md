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
control, `.hrns`, `.env`, secret and key paths are read-only. A terminal retains
the latest output within its byte limit and runs in its own process group on
Unix. Native client I/O holds one workspace root for the active ACP session.
Switching or dropping the connection releases its terminals. Exit waits run
alongside other requests and honor request cancellation. Both targets treat
unsupported client requests as `method_not_found`, and the browser's absent
capabilities are never advertised.

The Python dispatcher always launches the installed native wheel; the release
installs it with the Python client. A source checkout without the wheel reports an
install hint instead of opening a Python terminal. The dispatcher resolves the target
and existing credential references before replacing the Python process on POSIX. It also resolves `client_io` with the
documented precedence (CLI, profile, `[tui]`) and passes the TOML theme and
permission mode as launch defaults. The native process
inherits the terminal and owns terminal restoration and the ACP connection.
The Rust state file is authoritative only for values the user changes in the
native UI; unchanged values keep following the Python document. The default uv
development group resolves `hrns-tui` from `rust/`. Maturin builds the local
wheel. uv tracks the Cargo manifests and Rust sources when deciding whether to
rebuild it. The local binary reports the crate's preview version.

The published `hrns` wheel declares `hrns-tui==<version>` with markers for Linux
x64, macOS arm64/x64 and Windows x64, so `pip install hrns` installs the native
client at the same version while `hrns-slim[server]` and `hrns-slim[client]`
stay Python-only. Source installs use the local development dependency.
The publish workflow sets the release pin in the wrapper metadata with
`scripts/release_native_dependency.py` after `just check`, rebuilds `dist/`, and
asserts the result. See
[the release guide](docs/release.md#version-matched-native-dependency).

The approval panel takes the composer's place while a tool approval is in
focus. It shows the tool kind and a scrollable preview above numbered choices.
Up and Down move the selection, Enter confirms it, and `1`–`9` select by
position. Page Up and Page Down scroll details. `Esc` cancels the request. Modes `ask`,
`reads` and `all` auto-approve nothing, read/search/think calls, or any request
that offers an allow option; the decision uses the kind reported by the agent,
never a tool name. Pending approvals and forms are cleared when their turn ends
or the connection drops, so a request resolves exactly once.

The transcript marks a pending manual approval with `◈` in the approval
panel's warning color. A solid diamond marks approval and a hollow diamond
marks rejection. Expanded calls show the approval state in words. Automatic
approvals and cancelled requests carry no diamond.

### Native MCP export

The native TUI owns the `[mcp.servers.*]` configuration, consent, and upstream
connections for local and remote agents. It opens configured stdio and
Streamable HTTP servers with the Rust MCP SDK. HTTP headers and subprocess
environment values resolve on the client. Approved servers are registered
before `session/new` or `session/load` and declared as `type: "acp"`. Agents
must advertise `mcpCapabilities.acp`. They use standard `mcp/message` on the
existing ACP connection. The Rust provider uses ACP's cancellation marker,
correlates progress with the originating request, and reports startup and
protocol errors through the MCP health view.

The native upstream transport negotiates MCP 2026-07-28. HTTP credentials
can be passed through configured headers. The native client asks before
starting an enabled server unless its configuration already grants consent.
`always allow` updates the local TOML file. The built-in agent can launch
locally through ACP stdio and consumes the same declarations as a remote
agent. Browser sessions do not provide client-configured MCP servers.

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
queued prompts survive a reconnect to the same session. The next `Ready`
refreshes the session, workspace and agent capabilities after the client
discards connection-scoped state, approvals and in-flight turns. The client
sends queued prompts once the session is idle. Submitted prompts are tracked
by the server.

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
emphasis, links, headings and GitHub Flavored Markdown tables. Table cells wrap
within aligned columns and use labeled rows when the viewport is too narrow.
User and note entries stay literal. ACP tool
calls are keyed by `tool_call_id`, merged from start and progress updates, and
shown as compact rows with status markers and disclosure arrows. Click a tool
header to expand or collapse its output. `Ctrl-O` toggles details in bulk for
the current transcript. New tool calls start collapsed. Shell, read, list,
search, and failed calls show up to two output rows and an indicator when
more is available. Expanding shows all details received from the agent.
The transcript tracks
the visible entry and byte offset through streaming, resize and expansion.
The wheel scrolls by rows, Page Up and Page Down move by the viewport height,
and scrolling down to the latest output resumes follow mode. A one-column
scrollbar sits in the composer's right-border column when rows overflow; its
thumb shows the visible share, lights up under the pointer, and a press or drag
on it moves the viewport, as OpenCode's scrollbar does. While the reader is
away from the tail, a centred, clickable `↓ Back to bottom` label takes a
reserved row above the composer, as in Codex. It reverses under the pointer,
names new activity, disappears once the transcript follows the tail again, and
`Esc` returns there when no turn is running.

The transcript and optional sidebar sit above a rounded, full-width composer
and a single-row status bar. Prompt and reply text share the composer's typing
column, including wrapped lines. The `User` and `Agent` labels share that
column. User prompts use the theme's surface background, and tool calls and
reasoning are indented within the transcript.
`Alt+Up` and `Alt+Down` select earlier and later user prompts without
moving the composer cursor. Incoming output stays below the selected prompt
or scrolled viewport and shows a footer notice until the reader returns to the
bottom or presses `Ctrl+End`.

Composer completion reads the draft. A slash in the first character starts a
command. Recognized names use the theme accent. Partial and unknown names stay
in the composer on Enter, including when used as the first token after
`/queue`. Slashes elsewhere in a message are literal. The first token offers
local command completions, while a `$token` offers user-invocable skills from the agent's
`hrns/skills` metadata. `Tab` accepts the selected row, Up and Down move the
selection while it is open, and Escape dismisses it before cancelling a turn.
On native stdio connections, `@` at the start of the token under the cursor
starts a file search in the active session workspace. A background walk indexes
regular files, respects ignore rules, and skips hidden entries and symlinks.
Up and Down browse the ranked paths. Tab or Enter replaces the active token
with an `@` reference to a workspace-relative path, quoting the path when
needed. The composer uses separate theme colors for recognized commands,
exact file matches, and user-invocable skill mentions.
The reference travels as ordinary prompt text so the agent can read the file on
demand. WebSocket sessions accept manually entered paths.
`/skills` uses the shared picker to browse the user-invocable catalog with Up and
Down. The detail area shows the selected skill's description and the directory
from its ACP `path` on stdio connections. WebSocket connections show a remote
source label. Enter inserts the `$id` mention into the composer for the next prompt.
Submitting a draft that names a catalog skill attaches the skill id and mention
span to the first text block's `_meta`, leaving the typed text unchanged for
the agent to expand. Paste, draft history and attachment staging stay shared;
native reads paths while the browser opens its file chooser.

`Ctrl-B` opens a sidebar with the connection identity, client I/O mode,
session title, model, mode, permission mode, usage, cost, local queue,
skills and recent activity. `Ctrl-O` expands tool details and the sidebar
records tool transitions and turn outcomes. Both frontends persist sidebar,
width, reasoning visibility, theme and permission mode without
touching connection configuration: native writes JSON under
`$XDG_STATE_HOME/hrns/tui.json` (or `$HRNS_TUI_STATE`) and the browser uses
local storage. The activity sidebar is suppressed below 96 columns. The status
bar shows the connection and contextual keyboard hints. When space allows it
also shows the session, model, context usage, cost, effort and approvals.
`/theme`, `/permissions`, `/settings` and the `theme` and
`permission` page parameters expose the same values. The built-in themes are
`hrns`, `textual-dark`, `textual-light`, `ansi-dark` and `ansi-light`.

ACP plan snapshots populate the Todos panel with completion counts, item
status and priority. The first nonempty snapshot for a session opens Todos.
`/todo` focuses it for scrolling, and narrow layouts render it as a drawer.

Form elicitation uses a question dock with a tab per field and a confirmation
view for multiple questions. Harness choice fields and their `_text` companions
share a tab and submit an ordinary ACP form response.

The native client opens its local provider editor with `/providers` or
`/provider`. The popup owns add, edit, delete, and selection. The explicit
selection is client state for `/login`. Models and effort are ACP session
config options. The browser client and remote connections use ACP session
options to present the agent's available models. Agents that advertise the ACP
provider-routing capability expose their routes through `providers/list`,
`providers/set`, and `providers/disable`.

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

The built-in server supplies session titles through `session_info_update`.
`/agent` runs slash commands advertised by the connected agent.
The built-in server omits `available_commands_update` and `session/resume`.
`session/delete` returning `-32002` is treated as success. Listing defaults to
the current workspace.

### Terminal command workflows

`/connect` switches the active ACP transport. Native accepts a WebSocket URL
or `local` and an optional workspace. The browser accepts a WebSocket URL.
WebSocket connections use TLS outside loopback. Native startup bearer
credentials stay scoped to the startup endpoint. Switching creates a session
and clears local prompts queued for the previous session.

`/sessions` searches paged ACP session summaries by title, ID and workspace.
Tab switches between the current workspace and all accessible workspaces.
The picker shows the latest activity timestamp and opens the selected session
through `session/load`. The built-in stdio agent uses the configured SQLite
store so a new process can restore a session. The ACP adapter reports local
database failures with a client-visible message. Server registrations can
supply their own store.

`/models` searches the advertised model choices and marks the current model.
In the built-in terminal session, submitting a draft before model setup opens
the provider picker. The composer restores the draft after provider editing,
and Enter submits it once a model is ready. The ACP prompt endpoint rejects
missing setup and reports credential errors to clients.
The reasoning picker opens after model selection when the agent advertises
effort choices. The composer completes `/model` and `/effort` arguments from
the current ACP config options, and offers built-in options for `/theme`,
`/permissions` and `/provider`. The status bar displays the reasoning effort
advertised for the session and the client's current approval mode.

Tool entries use ACP kind, location and arguments to summarize each operation.
The built-in tools show the primary input in their collapsed headers and
render arguments as labeled fields, file previews, or lists on expansion.
Approval prompts use the same input formatting. Ctrl+O reveals formatted
arguments, results, diffs, and terminal references. Shell entries show the command and expose
multiline output on expansion. The shell's exit record supplies the failure
status when available. Reasoning headers are visible by default. Click a header
to expand its text. `/thoughts` hides the reasoning headers or shows them in
their collapsed state during streaming and replay. The transcript uses titled
panels for command output such as `/help` and `/settings`.

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

## Built-in coding agent

`build_agent()` uses `Coder(sub_agents=False)` by default. The ACP prompt
runtime handles deferred tool approvals for the parent run, and the delegated
run has no way to request those approvals. Callers can supply a custom
`Coder` when they manage delegation and approvals themselves.

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

Cancelling an awaiter never cancels execution. Cancellation waits for the active
turn to settle, and the worker starts pending turns after cancellation or an
error. Python provides inspection and clearing for separate queued turns.
Accepted prompts survive disconnect, their JSON-RPC associations do not, and no
reconnect resubmits them. The TUI sends concurrent prompts without cancelling
earlier request workers. `/queue <message>` holds a follow-up and staged attachments
in the TUI until idle. A bounded preview above the composer shows queued
message text in order until each prompt is submitted. `/edit-queue` or
`Shift+Left` from an empty composer moves the last pending message and its
attachments into a `/queue` draft for editing.

`RegisteredAgent.cancel_session(session_id)` resolves a session through `get_session`
and delegates to `Session.cancel()`. It shares lookup/restoration and unknown-ID
errors with `get_session` and cancels the active turn. Any pending prompt can
start in the same session as soon as the active turn settles.

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

The Python headless client's `ClientMcpProvider` reads enabled `[mcp]` servers,
launches the stdio command or connects to the HTTP/SSE URL,
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

The client asks for consent before starting a server and including its declaration
in session setup. The native TUI offers allow once, allow for the connection,
always allow, and deny. The headless client (`hrns run`) uses only servers
configured with `always_allow = true`.


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

ACP model options carry `_meta.providerId` when the resolver supplies a provider
association. The model picker uses that metadata as context for the advertised
display name and value.

The built-in stdio agent advertises `hrns/providers/manage`. The native
client enables it for the built-in stdio launch. The private
`_hrns/providers/*` requests list and edit local TOML entries and manage one
sign-in attempt. Saving provider settings and authenticating are separate operations.
Edits and successful sign-ins refresh ACP model options for open sessions.
The built-in agent fetches the Copilot account's catalog on startup and after
sign-in. It keeps each model's Chat Completions or Responses endpoint in memory
for its provider entry. The model picker uses ACP model option metadata to group
models by provider and switches tabs with Tab and Shift+Tab.
The ACP runtime reports the latest provider-supplied reasoning token count at
model-response boundaries, including when the model's context window is unknown.
The built-in stdio agent remembers explicit model selections in its local session
database, scoped to the active configuration file. Fresh sessions prefer that
selection when it is available. Saved sessions restore their own model, and
environment model overrides govern startup.

API keys and Copilot tokens are stored in the OS keyring. The Codex provider
loads and saves its rotating credentials through the same keyring. Accounts
are scoped to the provider entry, type, and base URL. A login attempt runs in
the runtime and exposes progress and public browser or device instructions to
the client. Bedrock uses boto3's credential chain and the AWS CLI's SSO cache.

## Session settings

Settings are the same object's second surface, because they have the same owner as models
and routing. `list_options(model_id)` advertises the non-model options a deployment allows
for a model — the shipped effort ladder is a `thought_level` select built by
`RuntimeModelResolver` — and `resolve_settings(model_id, setting_selections)` turns session
selections into a `ModelSettings` **delta**. The adapter merges that delta over
`ResolvedModel.settings` and the agent's own settings on every model request: passing a
callable `model_settings` is what lets `session/set_config_option` land on the next request
of a running turn. A model change or a route change that alters the available options takes
effect with the next model resolution. The delta shape also keeps the client out of
credential-bearing keys such as `extra_headers`, which routing uses.

Selections live in `SessionState.setting_selections`, are persisted in `StoredSession`, and
are revalidated whenever the model or a provider route changes; a value that disappeared is
dropped so the option falls back to its `default`. `providers/set` and `providers/disable`
snapshot the options before mutating resolver state and send `config_option_update` to any
resident session whose list changed, because nothing else tells such a client its choices
shrank.

`hrns agent` intersects its `[thinking]` policy with verified effort settings for the
effective provider route and model. Copilot supplies its effort levels through
`capabilities.supports.reasoning_effort` on the authenticated model catalog. Both
context tiers use the catalog entry for their shared model ID. Native routes use
provider-specific levels and Pydantic AI settings so the selected effort reaches the
provider with its intended value. Pydantic AI's unified thinking setting can map
different input levels to the same provider value. Routes without verified levels use the provider
default. The same allowed values drive advertisement and validation. An explicit
session choice takes precedence over `[model_settings].thinking`, and the adapter
validates the effective setting before inference.

The terminal saves the selected effort in `[local_model_effort]` for Local mode and
`[profile.<name>.model_effort]` for named connections. Each key is the model id advertised
by the agent, such as `"work-openai:gpt-5.2"` for the built-in runtime. The terminal
restores a saved value through ACP after creating a session or switching models if the
agent still offers that value. A built-in model switch without an applicable preference
uses the provider default. Loaded sessions retain their stored ACP selections, and
external agents supply the session's effort when there is no applicable saved preference.
The Python launcher supplies the resolved profile and its configuration file to the
native client. Rust updates the profile's TOML table after a successful ACP option
response. An in-app switch to a different transport has no saved profile.

## Usage reporting

Two channels carry usage, and they answer different questions. `PromptResponse.usage`
is per-turn token totals — the protocol has no field there for a context percentage or a
price, because `acp.schema.Usage` is tokens only. The context gauge is
`session/update` `usage_update`, whose `used` and `size` the native client renders
as `ctx 12000/200000` alongside the estimated session cost, when available.

It also emits it as the turn progresses. A request that ends in a tool call has already
produced its tokens, so the adapter sends the gauge when the tool call starts: the readout
follows the work instead of appearing only when the prompt returns, which for a long
tool-using turn is minutes after the person started watching it. `used` is that request's
tokens, and `cost` is the committed session estimate plus the run so far. A gauge identical
to the one already sent is dropped rather than repeated for every parallel tool call.

`used` is the last request's normalized input plus output tokens. Input already
includes cached prompt tokens. `size` comes from the resolved model's profile
`context_window`, which for locally configured Copilot models can come from the
authenticated catalog. With no known size, the client shows the turn's token total,
and the adapter can send the cost separately in `session_info_update` metadata
under `hrns/usage/cost_without_context`.

The local Copilot resolver reads prompt limits from the versioned catalog's
default and long-context tiers. When both limits are available and differ, it
advertises two model config values with a shared `hrns/model/context_group`
and per-value `hrns/model/context_size`. The native picker groups the values
into one row with left and right tier selection. The session and startup model
selection persist the selected value. Both values use the same Copilot model
ID for inference. Hosted Copilot requests select the API version for their tier, and
the resolved profile uses the tier's prompt limit plus its output allowance,
capped by the catalog's full window. The picker follows Copilot's size labels
for Carbon by showing the default prompt limit and the extended full window.

The estimated cost sums priced requests in USD through `RunUsage.cost`. The
official Copilot endpoint uses GitHub's published Gemini 4 Carbon rates, including
the UTC December 1, 2026 transition from promotional to standard rates. Other
available token rates come from Pydantic AI's pricing integration. The adapter
labels the estimate via `hrns/usage/estimated_cost` on standard usage updates.

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
these notifications to `PlanChanged` and renders them in the sidebar Plan section.

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
