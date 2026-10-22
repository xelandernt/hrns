# Runnable examples

Examples are deterministic and offline unless they explicitly opt into a provider. The
pinned environment uses `pydantic-ai-slim==2.57.0`, `pydantic-ai-harness==0.57.0`, and the
ACP SDK.

## 1. Python custom capability

```sh
python examples/custom_capability.py
# -> style on:  custom example: style=punchy
#    style off: custom example: style=none
```

It defines a native `AbstractCapability`, a strict config model, a `PluginDescriptor`, and a
`PluginRegistry`. The example binds the plugin directly with
`DefinitionBinding.from_plugin(plugin, config=HouseStyleConfig(...))`, so the config argument
is type-checked against the plugin's model. Two compositions demonstrate different initial
selections. In an ACP session, `next_turn` bindings such as this one can also be toggled or
configured live through the capability panel.

## 2. Developer starter

The developer starter requires an explicit workspace resource. It uses a local path in this
revision; there is no host fallback and no sandbox lease.

```sh
# Manifest form (editable YAML; see examples/starters/developer.yaml):
harness-host --manifest examples/starters/developer.yaml \
  --model test \
  --resource workspace=/path/to/repo
```

Python composition:

```python
import asyncio
from harness.runtime import ResourceSet
from harness.runtime.catalog import build_catalog_registry
from harness.runtime.composition import compose_definition
from harness.runtime.starters import developer_definition


async def main():
    definition = developer_definition()
    registry = build_catalog_registry()
    composition = await compose_definition(
        definition,
        registry,
        resources=ResourceSet(
            {
                "model.default": "test",
                "workspace": "/path/to/repo",
            }
        ),
        session_id="example",
    )
    result = await composition.agent.run("Summarise the repository structure.")
    print(result.output)


asyncio.run(main())
```

With the `test` model every tool is called with generated arguments; use a real provider for
meaningful work. Optional bindings (memory, sub-agents, web, MCP, approvals) are disabled
until their resources/extras are bound.

## 3. Research starter

```sh
harness-host --manifest examples/starters/research.yaml --model test
```

Offline structural composition with only planning and context controls:

```python
from harness.runtime.starters import research_definition

definition = research_definition(web=False)
```

## 4. Editable manifests

`examples/starters/developer.yaml` and `research.yaml` are `DefinitionManifest` schema v1
files. `harness.runtime.starters.load_starter_manifest(path, starter=...)` validates every
plugin id and configuration against the bundled catalog and converts it to an
`AgentDefinition`. Unknown plugins and invalid configurations fail before a session opens.

## 5. Existing FastAPI application

`examples/fastapi_app.py` registers an agent with a custom native capability, mounts network
ACP in an existing FastAPI app, and shares an explicit SQLite store:

```sh
uv sync --locked --extra server --extra client --extra openai
export OPENAI_API_KEY='your-key'
uv run uvicorn examples.fastapi_app:app --host 127.0.0.1 --port 8080 --ws-max-size 16777216
uv run hrns connect --url http://127.0.0.1:8080 --agent example.house_style
```

`EXAMPLE_MODEL=test` selects the offline structural model. Authentication and PostgreSQL
are Python dependencies described in [SERVER.md](../docs/SERVER.md).

## Integration prerequisites

| Capability | Requirement |
| --- | --- |
| Modal / E2B / Sprites / SSH | matching extra + credentials/service |
| MCP (local or hosted) | `pydantic-ai-slim[mcp]`; hosted extras plus vendor token |
| Web search/fetch local fallback | `pydantic-ai-slim[duckduckgo]`, `pydantic-ai-slim[web-fetch]` |
| Researcher | `pydantic-ai-harness[researcher]` |
| Exa / You.com / browsers | matching extras and API keys |
| Durable engines | matching upstream extras |
| Hosted integrations | integration extra + exported vendor token (see docs/CATALOG.md) |

Runtime Capability Creation is intentionally absent from all starters.
