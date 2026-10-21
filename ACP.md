# Serving a Pydantic AI agent with custom capabilities over ACP

This note explains how the harness serves an agent over ACP, and gives a complete
programmatic example: a custom Pydantic AI capability, registered and served without
authoring a YAML manifest.

It complements the existing [docs/ACP.md](docs/ACP.md), which documents the full ACP host
surface (the `_harness/*` extension, configuration transactions, session replay, and hosted
workers). This file focuses on the "build it in Python and serve it" path.

- Upstream ACP adapter docs: <https://pydantic.dev/docs/ai/harness/acp/>
- Runtime contract: [docs/ACP_RUNTIME_CONTRACT.md](docs/ACP_RUNTIME_CONTRACT.md)

## How the server side is put together

There are two layers in this repository:

| Layer | Location | Responsibility |
| --- | --- | --- |
| Upstream ACP adapter | `pydantic_ai_harness.experimental.acp` | Serves one Pydantic AI `Agent` over stdio; maps generic ACP prompts, streaming, tools, approvals, and sessions. |
| Harness ACP host | `src/harness/runtime/host.py` | Adds `AgentDefinition`, managed plugin bindings, policy, durable sessions, the `_harness/*` extension, and configuration transactions. |

The host is deliberately thin. It does not compose capabilities itself:

```
ACP client / editor
        │  newline-delimited JSON-RPC over stdio
        ▼
HarnessHostAgent                 host.py
        │
        ▼
SessionManager                   session.py
        │
        ▼
Session (one per ACP session)    session.py
        │
        ├── definition.factory() ──► Agent (fixed behavior)
        │                              └── fixed_capabilities
        │
        └── enabled DefinitionBinding
                │
                ▼
            PluginDescriptor.factory(config, binding_context)
                │
                ▼
            native pydantic_ai.capabilities.AbstractCapability
                │
                ▼
            merged into Agent.run(capabilities=[...])
```

Key pieces:

- `definitions.py` — `AgentDefinition` holds the fixed agent factory/spec and the managed
  `DefinitionBinding` list.
- `registry.py` — `PluginRegistry` maps each `plugin_id` to a `PluginDescriptor` whose
  factory returns a native Pydantic AI capability.
- `session.py` — for every turn, builds a fresh agent from the definition and instantiates
  every enabled binding exactly once. Disabled bindings contribute nothing.
- `acp.py` — pure translation between runtime events and ACP schema. It holds no session
  state.
- `host.py` — ACP session lifecycle, permissions, standard config options, and the optional
  `_harness/*` extension. `serve_agent()` runs the stdio JSON-RPC loop.

Because the runtime reconstructs capabilities from metadata on each turn, "disable" means
"omit the capability entirely", not "call a no-op wrapper".

## Fixed vs managed capabilities

Both are native Pydantic AI capabilities; the difference is whether ACP clients can see and
change them.

| | Fixed capability | Managed capability |
| --- | --- | --- |
| Where it lives | Inside the definition factory, or `AgentDefinition.fixed_capabilities` | A `DefinitionBinding` referencing a registered `PluginDescriptor` |
| Composed from validated config | No; whatever the factory returns | Yes, re-instantiated from the binding's config |
| Advertised as a config option | No | Yes; `binding:<binding_id>` standard option, plus `_harness/capabilities/list` |
| Toggleable / configurable by a client | No | Yes, subject to `CapabilityPolicy` |
| Good for | Behavior that is part of the agent's identity | Optional tools, integrations, policies, house style, etc. |

If you only need a custom capability that is always on, put it directly in
`Agent(capabilities=[...])`. If you want the ACP client (or the Python session API) to
enable, disable, or configure it, register it as a plugin and bind it in the definition.

## Complete programmatic example

The following is one Python file. It defines a custom capability, registers it as a managed
plugin, builds the agent definition in code, and serves the harness ACP host over stdio.

```python
#!/usr/bin/env python3
"""Serve a Pydantic AI agent with a custom capability over ACP."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from harness.runtime import (  # noqa: E402
    AgentDefinition,
    BindingContext,
    CapabilityPolicy,
    DefinitionBinding,
    PluginDescriptor,
    PluginRegistry,
    SessionManager,
)
from harness.runtime.host import (  # noqa: E402
    HarnessHostAgent,
    HostBundle,
    HostMetadataStore,
    HostPolicy,
    serve_agent,
)
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.capabilities import AbstractCapability  # noqa: E402
from pydantic_ai.toolsets import FunctionToolset  # noqa: E402

PLUGIN_ID = "acme.house_style"
BINDING_ID = "house_style"
STORE_PATH = Path(__file__).with_name("acp-agent.sqlite3")


# --- 1. The custom capability ----------------------------------------------


class HouseStyle(AbstractCapability[None]):
    """Contributes instructions and a tool to every model request."""

    id = PLUGIN_ID
    description = "Adds a house writing style and a tool that reports it."

    def __init__(self, *, style: str) -> None:
        self.style = style

    def get_instructions(self) -> str:
        return f"Write in a {self.style} house style."

    def get_toolset(self) -> FunctionToolset[None]:
        style = self.style

        def current_house_style() -> str:
            """Return the configured house writing style."""
            return style

        return FunctionToolset([current_house_style])


# --- 2. Validated, serializable configuration ------------------------------


class HouseStyleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: str = Field(default="punchy", pattern="^(punchy|formal)$")


def build_house_style(config: HouseStyleConfig, _binding: BindingContext) -> HouseStyle:
    return HouseStyle(style=config.style)


# --- 3. Management metadata ------------------------------------------------


def house_style_plugin() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        description="Example custom capability with editable style configuration.",
        config_model=HouseStyleConfig,
        factory=build_house_style,
        change_mode="next_turn",
        state_mode="run",
        presentation=[PLUGIN_ID],
        default_policy=CapabilityPolicy(
            available=True,
            enabled_by_default=True,
            client_can_toggle=True,
            editable_fields={"style"},
            change_mode="next_turn",
        ),
    )


# --- 4. The agent factory --------------------------------------------------


def create_agent(*, managed_capabilities: tuple[object, ...] = ()) -> Agent[None, str]:
    # Replace with any Pydantic AI model id, e.g. "anthropic:claude-sonnet-5".
    # Declaring `managed_capabilities` binds them at Agent construction time;
    # otherwise the runtime passes them to Agent.run(capabilities=[...]).
    return Agent(
        "anthropic:claude-sonnet-5",
        name="custom-capability-acp",
        capabilities=[*managed_capabilities],
    )


# --- 5. Definition + registry ----------------------------------------------


def build_bundle() -> HostBundle:
    registry = PluginRegistry()
    registry.register(house_style_plugin())

    definition = AgentDefinition(
        agent_id="acme.custom_capability",
        name="Custom capability ACP agent",
        definition_version="1.0.0",
        factory=create_agent,
        bindings=[
            DefinitionBinding(
                binding_id=BINDING_ID,
                plugin_id=PLUGIN_ID,
                config={"style": "punchy"},
                enabled=True,
            )
        ],
    )

    manager = SessionManager(
        registry,
        store=str(STORE_PATH),  # omit or use ":memory:" for an ephemeral store
        definitions=[definition],
        allow_local_binding_creation=False,
    )
    return HostBundle(
        definition=definition,
        registry=registry,
        manager=manager,
        store_path=str(STORE_PATH),
        meta=HostMetadataStore(STORE_PATH),
    )


# --- 6. Serve ---------------------------------------------------------------


async def main() -> None:
    bundle = build_bundle()
    host_agent = HarnessHostAgent(
        bundle,
        HostPolicy(
            principal="local",
            deployment_id="local",
            allow_local_binding_creation=False,
        ),
    )
    try:
        await serve_agent(host_agent)  # blocks until the ACP client disconnects
    finally:
        await host_agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
```

Nothing above reads a YAML or JSON agent manifest. The definition, plugin metadata, and
configuration schema are all ordinary Python objects. A store file (`acp-agent.sqlite3`) is
created next to the script so `session/load`, `session/list`, and `session/delete` work.
Use `:memory:` or pass no store for an ephemeral host.

### What the ACP client sees

After `initialize`, `session/new` returns a normal ACP session with a standard capability
option derived from the binding:

```json
{
  "sessionId": "s_…",
  "configOptions": [
    {
      "id": "binding:house_style",
      "name": "house_style",
      "type": "select",
      "currentValue": "on",
      "options": [
        {"value": "on", "name": "On"},
        {"value": "off", "name": "Off"}
      ],
      "category": "capability"
    }
  ]
}
```

`session/set_config_option` with `configId: "binding:house_style"` and `value: "off"`
disables the binding for subsequent turns. The `_harness/configuration/update` extension can
change the `style` field (`{"style": "formal"}`); it is advertised only to clients that
negotiated the extension.

### Register the agent in an editor

The editor launches your script as a subprocess. For Zed, this is client-side JSON — not an
agent behavior manifest:

```json
{
  "agent_servers": {
    "Custom Capability Agent": {
      "type": "custom",
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/acp_agent.py"],
      "env": {
        "ANTHROPIC_API_KEY": "..."
      }
    }
  }
}
```

Any ACP v1 client can launch it the same way: point the client at
`python /absolute/path/to/acp_agent.py`.

## Shorter alternative: the upstream adapter only

If you do not need managed bindings, durable harness sessions, or the `_harness/*`
extension, use the upstream adapter directly. A fixed custom capability is just an argument
to `Agent`:

```python
from pydantic_ai import Agent
from pydantic_ai_harness.experimental.acp import run_acp_stdio_sync

from my_capabilities import HouseStyle


def build_agent() -> Agent[None, str]:
    return Agent(
        "anthropic:claude-sonnet-5",
        instructions="You are a coding assistant.",
        capabilities=[HouseStyle(style="punchy")],
    )


if __name__ == "__main__":
    run_acp_stdio_sync(build_agent())
```

If the capability must be derived from the client's session (for example, rooted at the
editor's `cwd`), return it from `session_config`:

```python
from pydantic_ai_harness.experimental.acp import (
    AcpSession,
    AcpSessionConfig,
    run_acp_stdio_sync,
)


def session_config(session: AcpSession) -> AcpSessionConfig[None]:
    return AcpSessionConfig(
        deps=None,
        capabilities=[HouseStyle(style="punchy")],
    )


run_acp_stdio_sync(build_agent(), session_config=session_config)
```

Use `capabilities`, not `toolsets`, for capabilities that contribute hooks, instructions,
ordering constraints, or capability-owned events. `toolsets` is for bare toolsets such as
MCP connections.

The trade-off is deliberate: the upstream adapter has no plugin registry, no
enable/disable policy, and no configuration snapshots. When you outgrow those omissions,
move the same capability behind a `PluginDescriptor` and serve it with the harness host as
shown above.

## Will the harness work with any ACP provider?

The word "provider" is ambiguous here, so it is worth separating the three roles:

### 1. Any ACP-compatible client or editor — yes, with ACP v1

`harness-host` speaks standard ACP v1 over stdio. Any client that implements the standard
session and prompt methods can drive it: Zed's external agents, Toad, editor plugins, or a
small custom client. The core interaction does not depend on this project:

- `initialize`, `session/new`, `session/prompt`, `session/cancel`
- streamed `agent_message_chunk` / `agent_thought_chunk`
- `tool_call` / `tool_call_update`
- `session/request_permission`
- `session/load`, `session/list`, `session/delete`, `session/close`
- `session/set_config_option`

The `_harness/*` extension is additive. A client that ignores `_meta.harness` still gets a
normal ACP agent; it simply cannot inspect the plugin inventory, edit binding configuration,
or receive `_harness/configuration/changed`. Local binding creation (`add`) is also hidden
unless the host enables it and the client negotiates the extension: set
`allow_local_binding_creation=True` in `SessionManager` and `HostPolicy` if you want clients
to add bindings from the registered catalog. The example keeps it off, so the deployment's
bindings are fixed but still toggleable.

Two client-specific details affect what you see, not whether the agent works:

- If the client supports boolean config options, bindings appear as booleans; otherwise the
  host advertises equivalent `on`/`off` selects, as ACP v1 requires.
- Rich diffs, clickable file locations, and editor-native filesystem/terminal tools depend on
  what the client advertises. Without them, tools still run and render as generic tool calls.

A future ACP major version is not guaranteed to work; the host negotiates version 1 and
falls back to the SDK's supported version.

### 2. An arbitrary ACP agent/server — no, that is the reverse direction

`harness-host` is an ACP **agent/server**. It serves an agent to a client; it does not
connect to another ACP agent. If you have an independent ACP agent (Claude Code, another
framework's ACP server, or a wrapper that exposes one), drive it with the harness client
instead:

```sh
hrns connect --stdio -- /path/to/other-agent --acp
```

The harness terminal and client are the consumer side; the host is the producer side. They
use the same protocol but are not interchangeable.

### 3. Any model provider — yes, within Pydantic AI's provider support

ACP is not a model-provider protocol. The model is whatever Pydantic AI accepts in
`Agent(...)`: Anthropic, OpenAI, Google, DeepSeek, an OpenAI-compatible endpoint, or a
custom `Model` implementation. Ensure the provider package extra is installed and the
credential is visible to the subprocess the editor launches. GUI editors often do not
inherit your shell environment, so pass API keys explicitly in the client's `env` block.

## Practical notes

- **Approvals.** Mark a tool `requires_approval=True` (including tools contributed by a
  capability) and the host translates the pause into `session/request_permission`. The
  definition's agent should include `DeferredToolRequests` in its output types for approval
  flows; see the gap note in [docs/ACP_RUNTIME_CONTRACT.md](docs/ACP_RUNTIME_CONTRACT.md).
- **Workspace.** The client's `cwd` becomes the session working directory. Bind workspace
  tools through a `workspace` resource, or fall back to `LocalWorkspaceBackend(session.cwd)`
  in the upstream adapter's `session_config`. The host validates and canonicalizes `cwd`;
  hosted workers restrict it to configured roots.
- **Persistence.** `SessionManager(store=...)` persists conversation, configuration, and
  transcript. `HostMetadataStore` keeps the ACP-only sidecar (working directory and selected
  model). Use a real path for both if the editor should reopen sessions after a restart.
- **Model selection.** Advertise models with `HostPolicy(models=(...), default_model=...)`;
  the host exposes them as the standard `model` option and applies the selection per run.
- **Custom tool rendering.** Pass `tool_presenter=` to `HarnessHostAgent` (or
  `run_acp_stdio_sync`) to render your tool calls as `read`/`edit`/`execute` with locations
  and diffs instead of opaque JSON.
- **Lifecycle.** `serve_agent()` blocks until EOF. Always call `host_agent.shutdown()` in a
  `finally` block so live sessions and the store are released for another process.
