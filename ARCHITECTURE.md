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

## ACP dependency

`agent-client-protocol` is pinned to the 1.0 release candidate (`>=1.0.0rc3,<2`).
That release provides the full ACP v1 method table, including `session/delete`.
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
once for each new/restored live session. `RegisteredAgent` provides typed session
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

Programmatic and ACP prompts share FIFO acceptance order. A prompt enters history
only when started. Cancelling an awaiter never cancels execution. Cancel and runtime
errors pause remaining work; Python inspection/resume/clear are the only queue
management API. Accepted prompts survive disconnect, their JSON-RPC associations do
not, and no reconnect resubmits them. TUI-local queues remain separate.

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
contract; all admitted identities have equal server scope. Read-only semantics are
application/OpenAPI metadata, not an invented AG-UI capability; mutations return 405.

Each native run passes through one `AGUIEventStream` converter and encoder, with
server-assigned conversation/native-run IDs. Its native events also feed ACP.
Deferred decisions split a turn into native runs; session-level `STATE_SNAPSHOT`
reports waiting without claiming application-turn completion. No request adapter or
`RunAgentInput` is mounted.

A join atomically captures an AG-UI message baseline plus the current-run converted
prefix and installs its live queue on the event loop. History conversion uses
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
option and an always-on model does not offer `off`. `[model_settings].thinking` remains the
agent-level baseline; the runtime refuses to start if it names a level the policy does not
offer for the startup model, since no client could see or change it.

## Usage reporting

Two channels carry usage, and they answer different questions. `PromptResponse.usage`
is per-turn token totals — the protocol has no field there for a context percentage or a
price, because `acp.schema.Usage` is tokens only. The context gauge is
`session/update` `usage_update`, whose `used`/`size`/`cost` the client renders as
"ctx 12k/200k (6%)" and the session's spend. An agent that sends only `PromptResponse`
leaves the client's status band blank, which is why the adapter emits the gauge at the
end of every prompt.

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
facade: `HrnsConfig.profile` is the map the picker lists, `HrnsConfig.tui` is what the
settings dialog edits, and `HrnsConfig.save()` writes the whole effective document to the
user file (defaults omitted). Project documents are read, never written.

## Reopening the last connection

A bare `hrns` reconnects to the connection this terminal used last instead of always
defaulting to the built-in model. The identity is one string in the document's `[tui]`
section, written by the same `HrnsConfig.save()` that persists theme and permission
mode: `builtin` for the built-in runtime, a profile name for a saved stdio or remote target.
An ad-hoc `--server`/`--stdio` command line is not remembered, because it belongs to no
file and there would be nothing to reopen. The identity never carries a working directory:
a reopened connection starts where the new launch stands, or where its profile says.

Remembering happens once a session exists, so a connection that fails at `session/new`
does not become the next launch's default. Resolution failures — an unconfigured
built-in model, a deleted profile, a dead endpoint — drop into the picker with the reason in its
status line rather than exiting, and a profile deleted from disk is forgotten outright so
its name cannot fail on every launch. The picker's cursor starts on the remembered entry,
which makes Enter a retry of the connection that just failed.

`_remembered_target()` in `cli.py` owns the identity→target mapping and only the terminal
uses it: `hrns run` takes its target from its own arguments every time.

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
