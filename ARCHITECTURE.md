# Architecture

## CLI

All command-line entry points are built with [Cyclopts](https://cyclopts.readthedocs.io/).
Do not add `argparse`, Click, or Typer parsers.

- `hrns` — `src/hrns/client/cli.py` is the main Cyclopts `App` (commands such as
  `connect`, `run`, `profiles`, and `mcp`). It is exposed through
  `hrns.client.cli:main`.
- `hrns-app` — `src/hrns/app/__main__.py` registers `acp` and `serve` as
  `@app.command` functions. It is exposed through `hrns.app.__main__:main`.
- `hrns-local` — `src/hrns/app/local.py` uses a Cyclopts `App` with `run` as its
  default command. It is exposed through `hrns.app.local:main`.

Commands are plain functions with typed keyword arguments; Cyclopts derives
options, types, defaults, and help text from those signatures. Parameter help is
declared inline with `Annotated[..., Parameter(help="...")]`. Each module exposes
a `main()` that calls its `App`, which keeps the `[project.scripts]` targets
unchanged.

`hrns.client.cli` uses `types.SimpleNamespace` as an internal argument container
for its command implementations, so the codebase has no `argparse` dependency at
all.

## ACP dependency

`agent-client-protocol` is pinned to the 1.0 release candidate (`>=1.0.0rc3,<2`).
That release provides the full ACP v1 method table, including `session/delete`.
`hrns` implements the adapter itself in `src/hrns/app/acp/`, derived from the design
of `pydantic-ai-harness`'s experimental ACP adapter but targeting ACP 1.x directly.

`pydantic-ai-harness`'s `[acp]` extra still declares `agent-client-protocol<0.12`,
so `hrns` depends on the base package and declares ACP directly; the extra
contributes only that one dependency. Harness is still used for the coding
capabilities (`Coder`, `FileSystem`), not for ACP. Remove the split once harness
supports 1.x.

The adapter implements the complete `acp.Agent` protocol. `session/list` and
`session/delete` are backed by `ListableSessionStore`/`DeletableSessionStore`, and
`providers/*` plus the `model` config option are backed by a `ModelResolver` the caller
supplies. MCP-over-ACP and NES are explicitly unsupported and not advertised until their
features exist (see `TODO.md`).

## ACP models and provider configuration

One object answers everything ACP asks about models: `ModelResolver`
(`src/hrns/app/acp/models.py`). ACP has no model methods — models are a session config
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
`LocalModelResolver` in `hrns-local` seeds it from the TOML provider entries, derives model
ids from `settings.model_targets()`, and raises `ProviderDisabled` instead of silently
falling back to a configured endpoint.

ACP's provider payload reports routing endpoints, not the models behind them, so the
model→provider link travels separately: each `ModelChoice.provider_id` becomes
`_meta.providerId` on the model option. `hrns-local` derives it from the `entry:model` id;
the client's `/providers` table groups the option's values by that tag, and `/models`
appends it to any model name that does not already read `provider:model`.

## Todos

`hrns.capabilities.Todo` is a standalone Pydantic AI capability with two tools:
`read_todos` reads the ordered list; `write_todos` replaces it completely (an empty
list clears it). `TodoItem` has content, priority (`high`, `medium`, `low`), and
status (`pending`, `in_progress`, `completed`). Items are immutable snapshots.
Static instructions explain the workflow; there is no model-request reminder injection.

```python
from pydantic_ai import Agent
from hrns.capabilities import Todo, TodoStore

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
