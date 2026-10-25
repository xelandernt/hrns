# Runtime API

The runtime is a small composition and persistence layer. It does not execute ACP turns;
that belongs to `pydantic_ai_harness.experimental.acp.PydanticAIACPAgent`.

```python
from harness.runtime import (
    AgentDefinition,
    BoundDefinition,
    DefinitionBinding,
    AgentSpec,
    PluginRegistry,
    PluginDescriptor,
    ResourceRequirement,
    ResourceSet,
    SQLiteSessionStore,
    SessionStore,
    ScopedSessionStore,
    Composition,
    compose_definition,
    HarnessError,
)
```

`harness.runtime` imports no client, FastAPI, or Textual modules. Importing
`harness.runtime.host` pulls in the upstream ACP adapter.

## Compose a session

```python
from harness.runtime import MODEL, WORKSPACE, ResourceSet

composition = await compose_definition(
    definition,
    registry,
    resources=ResourceSet({MODEL: model, WORKSPACE: "/path/to/project"}),
    session_id="session-id",
    principal="user-1",
    deployment_id='["agent-id","1.0.0"]',
    initial_configuration=[EnableChange(binding_id="style", enabled=True)],
    model_settings={"temperature": 0.2},
    effort="high",
    effort_support="supported",
)
```

Bindings are typed slots: `MODEL`, `WORKSPACE`, `ANSWERS`, `PLAN`, or a slot a plugin
registers with `harness.runtime.resources.SLOTS.register`. A `{name: value}` mapping is also
accepted — the name resolves to its slot — so values loaded from configuration still work.
A definition that never declares a slot receives no value for it, even when the deployment
bound one.

The result is a native agent plus an upstream `AcpSessionConfig`:

| Field | Meaning |
| --- | --- |
| `composition.agent` | the native Pydantic AI agent, with `deps_type=HarnessDeps` when Harness builds it |
| `composition.config` | `deps`, capabilities, toolsets, and workspace for every run |
| `composition.catalog` | live `PolicyCatalog` for the session |
| `composition.snapshot` | current `ConfigurationSnapshot` |
| `composition.resources` | resolved `ResourceSet` |
| `composition.deps` | `HarnessDeps` for the run: `workspace`, `model`, `answers`, `plan`, `custom` |
| `composition.effort` | mutable reasoning-effort state |
| `composition.workspace` | native workspace backend, or `None` |

`composition.deps` is what tools, instructions and capabilities read as `ctx.deps` — the
framework's own dependency injection, typed as `AbstractCapability[HarnessDeps]`. Pass
`deps=` to `compose_definition` to supply a deployment's own object instead; Harness then
leaves `deps_type` alone, because an application that declares its own dependencies must keep
them.

`compose_definition` is async because Python factories may be sync or async. It applies
`initial_configuration` (creation) or `saved_bindings` (reload/edit), never both. A factory
that declares `managed_capabilities` receives them at `Agent` construction; otherwise they
are applied per run through `AcpSessionConfig.capabilities`. The ACP host re-runs composition
after a capability edit and replaces the live composition's contents in place, so existing
`SessionDeps` references continue to work.

Run the native agent directly when not using ACP:

```python
result = await composition.agent.run("Hello")
```

## Definitions and plugins

`AgentDefinition` holds:

- `spec` (upstream `AgentSpec`) or `factory` returning a native agent;
- `fixed_capabilities` bound to every session;
- managed `bindings`, each a named, configured instance of a registered plugin;
- `resources` requirements and an optional fallback model.

Requirements for descriptor-backed bindings are derived from the plugin's `needs`, so a coder
binding adds the workspace requirement automatically. Derivation covers every bound plugin,
including one disabled by default, so enabling it next turn cannot reveal a missing resource.
`resources` remains for host choices such as `MODEL` and for custom slots.
`definition.resolve(registry)` attaches descriptors to `plugin_id` bindings; manifests need
this because they carry ids rather than Python objects.

`definition.bind(...)` validates the deployment's values against every requirement and returns
a `BoundDefinition`. Hosts register that validated value, moving a missing workspace from
`session/new` to registration:

```python
from harness.runtime import MODEL, WORKSPACE

bound = definition.bind(
    {MODEL: "openai:gpt-5-mini", WORKSPACE: "/path/to/project"},
    registry=registry,  # when bindings still carry only plugin ids
)
agents.add_agent(bound)
```

`BoundDefinition` is the typed hand-off between definition and deployment. Its
`resources` are a normal `ResourceSet`, so `bound.resources.get(WORKSPACE)` is still
narrowed to the workspace input type.

`PluginRegistry` resolves plugin descriptors and instantiates native capabilities from
validated configuration. `PolicyCatalog` validates initial selections against configuration
schemas, availability, toggle policy, lifecycle, requirements, conflicts, and cycles.

A Python definition can bind a descriptor directly instead of registering it and naming it by
`plugin_id`:

```python
from harness.runtime import DefinitionBinding, PluginDescriptor

plugin: PluginDescriptor[HouseStyleConfig] = PluginDescriptor(
    plugin_id="example.house_style",
    config_model=HouseStyleConfig,
    factory=build_house_style,
)
binding = DefinitionBinding.from_plugin(
    plugin,
    binding_id="style",
    config=HouseStyleConfig(style="formal"),  # type-checked against the descriptor
)
```

`from_plugin` accepts either the typed config instance or a mapping (validated at runtime),
and derives the stable `plugin_id`. Inline descriptors bypass registry lookup for that binding,
but still require a stable plugin id for persistence and runtime-added binding reload. Manifests
remain `plugin_id`-only; exporting a definition with an inline descriptor fails explicitly.
Descriptors attached by `resolve` are registry-backed, so a resolved definition still exports
by id.

Registration remains lazy; a definition manifest references plugins by `plugin_id`.

## Persistence

`SQLiteSessionStore` and `PostgresSessionStore` implement the same contract:

```python
store = SQLiteSessionStore("/path/sessions.sqlite3")
await store.initialize()
scoped = store.scoped(principal="user-1", deployment_id=deployment_id)

record = await scoped.get(session_id)
page = await scoped.list(cwd="/workspace", limit=50)
await scoped.update(session_id, title="Name", effort="high", touch=True)
await scoped.delete(session_id)
await store.close()
```

`ScopedSessionStore` also implements the upstream `SessionStore` protocol
(`save(session_id, StoredSession)` and `load(session_id)`) for one principal and deployment.
The adapter writes committed turns through it; `session/load` restores native messages,
transcript, and selected model.

`list()` returns a `SessionPage` with metadata-only `records` and an opaque `next_cursor`.
Filtering by `cwd`, ordering by `(last_active_at DESC, session_id ASC)`, and keyset pagination
run in the store query, so listing a page does not load stored conversation bodies.

`SessionRecord` carries host metadata: `cwd`, `title`, `effort`, `model`, selected `bindings`,
usage totals, and timestamps. The current `ConfigurationSnapshot` and binding descriptors are
Commands contributed by a binding are defined with `Command`/`CommandContext` from
`harness.runtime` (see [EXTENSIONS.md](EXTENSIONS.md)). They are user-only application
actions: the runtime validates arguments and the active revision, then runs the handler under
the session's existing turn lock without creating a model request or tool event.

served through the `_harness` extension; updates are applied in memory and persisted through
the `bindings` column.

Durability follows upstream semantics: save errors are logged, not fatal; there is no
transactional turn commit or cross-connection lock. See [POSTGRES.md](POSTGRES.md),
[SERVER.md](SERVER.md), and [ACP_RUNTIME_CONTRACT.md](ACP_RUNTIME_CONTRACT.md).
