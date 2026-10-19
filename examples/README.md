# Runnable examples

All examples are deterministic and offline unless they explicitly opt into a provider or a
hosted integration. The pinned environment is Python 3.12, `pydantic-ai-slim==2.57.0`,
`pydantic-ai-harness==0.57.0`.

## 1. Deterministic demo (no credentials, no workspace, no network)

The demo definition runs a real native Pydantic AI `FunctionModel` with a managed native
instruction capability (`example.tone`). Turning the binding off removes its instructions from
the next turn; the scripted model echoes which tone reached it, so the toggle is visible.

```sh
# Direct Python session (real SessionManager, real configuration transactions):
python -m harness_runtime.examples
# -> turn 1: offline demo: tone=concise
#    turn 2: offline demo: tone=none
#    turn 3: offline demo: tone=concise

# Machine-readable:
python -m harness_runtime.examples --json

# Through the stdio ACP host (works with any ACP client):
harness-host harness_runtime.examples:demo_definition
```

The demo definition declares **no resources**. `harness-host` loads it from the import
reference and serves the same session controller used by Python.

## 2. Python custom capability

```sh
python examples/custom_capability.py
# -> turn 1 (style on):  custom demo: style=punchy
#    turn 2 (style off): custom demo: style=none
```

It defines a custom `AbstractCapability` subclass, a strict config model, a `PluginDescriptor`,
registers it in a real `PluginRegistry`, and toggles it through `session.update_configuration`.
This is the pattern for local Python plugins; no second behavioral base class is required.

## 3. Developer starter

The developer starter requires an **explicit workspace** resource. Nothing falls back to host
execution. The recommended local binding is the Smol sandbox (`pydantic-ai-harness[smol]` on
Apple-silicon macOS or Linux/KVM); trusted local work can bind the local workspace instead.

```sh
# Manifest form (editable YAML; see examples/starters/developer.yaml):
harness-host --manifest examples/starters/developer.yaml \
  --model test \
  --resource workspace=/path/to/repo

# Python form:
python - <<'PY'
import asyncio
from pydantic_ai.workspaces import LocalWorkspaceBackend
from harness_runtime.examples import demo_registry
from harness_runtime.resources import ResourceSet
from harness_runtime.session import SessionManager
from harness_runtime.starters import developer_definition

async def main():
    definition = developer_definition()
    manager = SessionManager(demo_registry(), definitions=[definition])
    resources = ResourceSet({
        "model.default": "test",
        "workspace": LocalWorkspaceBackend("/path/to/repo"),
    })
    async with manager.open_session(definition, resources=resources) as session:
        result = await session.run("Summarise the repository structure.")
        print(result.output)

asyncio.run(main())
PY
```

With the `test` model every tool is called with generated arguments; use a real provider model
for meaningful work. The manifest's optional bindings (memory, sub-agents, web, MCP, approvals)
are disabled until their resources/extras are bound.

## 4. Research starter

The research starter needs only a model. Search and fetch default on and require a provider
with native web search/fetch, or `pydantic-ai-slim[duckduckgo,web-fetch]`.

```sh
harness-host --manifest examples/starters/research.yaml --model test
```

Offline structural run with only planning and context controls:

```python
from harness_runtime.starters import research_definition
definition = research_definition(web=False)   # no web bindings enabled
```

Optional memory (`memory.store`), delegation (`subagents.agents`), MCP (`pydantic-ai-slim[mcp]`)
and browser capabilities stay disabled in the shipped manifest.

## 5. Editable manifests

`examples/starters/developer.yaml` and `research.yaml` are `DefinitionManifest` schema v1
files. `harness_runtime.starters.load_starter_manifest(path, starter=...)` validates every
plugin id and configuration against the bundled catalog and converts the manifest into an
`AgentDefinition`; unknown plugins and invalid configurations fail before a session opens.

Loading the same files through `harness-host --manifest ...` uses the host's manifest loader.

## Integration prerequisites

| Capability | Requirement |
| --- | --- |
| Smol sandbox | `pydantic-ai-harness[smol]`; macOS Apple silicon or Linux with `/dev/kvm` |
| Modal / E2B / Sprites | matching extra + account credentials |
| MCP (local or hosted) | `pydantic-ai-slim[mcp]`; hosted extras plus vendor token |
| Web search/fetch local fallback | `pydantic-ai-slim[duckduckgo]`, `pydantic-ai-slim[web-fetch]` |
| Researcher | `pydantic-ai-harness[researcher]` (installs both web fallbacks) |
| Exa / You.com / browsers | `[exa]` + `EXA_API_KEY`, `[youdotcom]` + `YDC_API_KEY`, `[browser-use]`, `[playwright]` + Chromium |
| Durable engines | `pydantic-ai-slim[temporal|dbos|prefect]`, `pydantic-ai-harness[aws-lambda|absurd|pgtask|render]` |
| Hosted integrations | the integration extra + exported vendor token (see docs/CATALOG.md) |

Runtime Capability Creation is intentionally absent from all starters; enabling it is an
explicit deployment decision.
