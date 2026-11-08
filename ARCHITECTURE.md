# Architecture

## CLI

All command-line entry points are built with [Cyclopts](https://cyclopts.readthedocs.io/).
Do not add `argparse`, Click, or Typer parsers.

- `hrns` — `src/hrns/cli.py` is the one Cyclopts `App`. It registers the client
  commands (`connect`, `run`, `login`), the server commands (`agent`, `acp`,
  `serve`), and the shared commands (`config`, `profiles`, `mcp`, `clean`). It is
  exposed through `hrns.cli:main`.
- `src/hrns/client/` holds the terminal, headless runner, transports, and the
  built-in agent launcher; it never imports the server package.
- `src/hrns/server/` holds the Pydantic AI agent, capabilities, ACP adapter, and
  FastAPI mount.
- `src/hrns/shared/` holds the configuration document, credentials, MCP-over-ACP
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
`hrns` implements the adapter itself in `src/hrns/server/acp/`, derived from the design
of `pydantic-ai-harness`'s experimental ACP adapter but targeting ACP 1.x directly.

`pydantic-ai-harness`'s `[acp]` extra still declares `agent-client-protocol<0.12`,
so `hrns` depends on the base package and declares ACP directly; the extra
contributes only that one dependency. Harness is still used for the coding
capabilities (`Coder`, `FileSystem`), not for ACP. Remove the split once harness
supports 1.x.

The adapter implements the complete `acp.Agent` protocol. `session/list` and
`session/delete` are backed by `ListableSessionStore`/`DeletableSessionStore`, and
`providers/*` plus the `model` and setting config options are backed by a `ModelResolver`
the caller supplies. MCP-over-ACP and NES are explicitly unsupported and not advertised
until their features exist (see `TODO.md`).

## MCP over ACP

`src/hrns/shared/acp_mcp/` implements the stateless MCP-over-ACP binding for MCP
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
session config and advertises `mcpCapabilities.acp`.

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

The feasibility analysis, work breakdown, and open decisions live in
`docs/mcp-over-acp-analysis.md`; the remaining TODO items are in `TODO.md`.

## ACP models and provider configuration

One object answers everything ACP asks about models: `ModelResolver`
(`src/hrns/server/acp/models.py`). ACP has no model methods — models are a session config
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

`src/hrns/shared/config/settings.py` holds `HrnsConfig`, the single pydantic-settings model both
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

`hrns.server.capabilities.Skills` subclasses `pydantic_ai_skills.SkillsCapability`, the
same way `Coder` subclasses the harness coder. Discovery and instruction rendering stay
upstream: harness turns each `SKILL.md` into a deferred capability, and
`pydantic-ai-skills` adds the bundled-file tools, `${SKILL_DIR}` resolution, and the
programmatic-skill path. The subclass exists for three things upstream does not expose:

1. **A host catalog outside a run.** `catalog()` calls harness's `Skills.load()`, which
   is the documented way to read the same libraries a run would read, and `metadata()`
   maps the result to `SkillInfo`. The adapter advertises that on `session/new`, so the
   terminal can offer `$name` before any prompt exists.
2. **User-invoked instructions.** `instructions()` renders the body with placeholders
   resolved and the bundled-file inventory appended. It mirrors what
   `SkillsCapability._rebuild_leaf` produces for the model's own `load_capability` call.
3. **Project-wins duplicates.** `SkillsCapability` does not expose harness's
   `duplicate_names`, and its default (`'error'`) turns a project skill overriding a
   user-wide one into a failed run. `_build_harness_skills` is a near-copy of upstream's
   private method that passes `duplicate_names='keep_first'`; `_adopt_first_package`
   then re-indexes the bundled files so `read_skill_resource` reads the winning library.
   Both are tied to the pinned `pydantic-ai-skills` minor and must be revisited on an
   upgrade.

**No extension methods.** Standard ACP has no skills concept, but it has `_meta` on every
message, explicitly reserved for implementation-specific payloads. The catalog rides in
the `session/new` and `session/load` response `_meta` (`hrns/skills`), and a prompt
reference rides in the text block's `_meta` (`hrns/skills: [{id, mention}]`). An earlier
design added an `_hrns/skills/list` method; it was dropped because the only thing it
bought was live refresh, which a session snapshot does not need. The trade-off is that a
skill added on disk appears after the client reopens or reloads the session.

**User invocation is user content plus a capability state, not an assistant turn.** When
`$name` resolves, the adapter prepends the rendered instructions to the run input as a
`<skill_content name="…">` block and binds the session's invoked ids around the run.
`Skills.for_run` rebuilds those leaves with `defer_loading=False`, so
`active_capability_ids` includes them: the bundled-file `require_loaded` gate opens, and a
skill the person already invoked leaves the model's deferred catalog. The invoked set
lives on `SessionState` and is saved with the session, so `session/load` restores it.

An earlier version appended a synthetic `load_capability` call/return pair, which also
marks the skill loaded through `parse_loaded_capabilities`. That is not portable: it puts
an assistant message with tool calls and no `reasoning_content` into the history, and
DeepSeek's thinking mode rejects the next request with
``The `reasoning_content` in the thinking mode must be passed back``. The user-content
form is what OpenCode does and works across providers.

The model-initiated path is unchanged: the deferred capability's catalog comes from
Pydantic AI, `load_capability` loads it, and `run_skill_script` is not read-only, so
`require_approval` turns each script run into a permission request. The adapter finds the
capability by walking `Agent.root_capability`, so a custom `build_agent` composition gets
the catalog by adding `Skills` — no adapter parameter to thread.

The catalog is read once per process and cached; the libraries are resolved against the
agent build's `cwd`, matching `SkillsCapability`, which resolves its own relative
libraries at construction rather than per session.

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
