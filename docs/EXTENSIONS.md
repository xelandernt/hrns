# Plugin extensions: commands and inline views

A Harness plugin packages three independent contributions:

1. a native Pydantic AI capability (tools, instructions, hooks),
2. optional user commands such as `/git:status`,
3. optional local Textual views for command results and tool bodies.

Commands are **user actions**, not agent tools. A direct command runs its own handler and
returns inline output. It makes no model request and adds nothing to model history unless it
explicitly returns a `StartTurn` conversation action.

The protocol package imports neither Pydantic AI nor Textual. Runtime code imports no UI
library. Widget code runs only on the client.

## 1. Descriptor provider

Installed packages register one zero-argument provider under `harness.capabilities`:

```toml
[project.entry-points."harness.capabilities"]
example_status = "example_status:descriptor"
```

```python
from harness.runtime import Command, PluginDescriptor


def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="example_status",
        version="0.1.0",
        config_model=ExampleConfig,
        factory="example_status:factory",  # lazy module:attr reference
        commands=[Command(name="status", handler=status, description="Show status")],
    )
```

Discovery reads entry-point metadata only. The provider is imported when its descriptor is
first needed; the native factory may remain a lazy import reference. The provider module
must not import Textual or an optional provider SDK.

Programmatic `PluginRegistry.register(PluginDescriptor(...))` is the same contract.

## 2. Commands

```python
from harness.protocol import PresentationPayload
from harness.protocol.extensions import CommandResult
from harness.runtime import Command, CommandContext, TextArguments


async def status(context: CommandContext, arguments: TextArguments) -> CommandResult:
    return CommandResult(
        presentation=PresentationPayload(
            renderer_id="example.git.status",
            schema_version=1,
            data={"label": context.config.label, "text": arguments.text},
            fallback=f"{context.config.label}: {arguments.text}".strip(),
        )
    )
```

- The namespace is the configured **binding id**: `/example:status`. Two instances of one
  plugin have independent namespaces. Command names may not contain `:` or whitespace.
- `context.config` is the binding's validated configuration model.
- `context.history()` returns a deep read-only snapshot of committed native messages.
- Handlers are async. They never receive a fake `RunContext`, mutable adapter state, or a
  connection object.
- Arguments default to `TextArguments.text`. Supply `args_model` and an optional `parser`
  from slash-tail text to a mapping; structured widget callbacks validate against the same
  model.

### Explicit conversation actions

Only `StartTurn` is currently supported:

```python
from harness.protocol.extensions import CommandResult, StartTurn


async def ask(context: CommandContext, arguments: TextArguments) -> CommandResult:
    return CommandResult(conversation=StartTurn(prompt=f"Review: {arguments.text}"))
```

The client consumes this action once through its normal prompt path. Display output remains
outside model history. Clear/compact/edit history operations are **not supported** on the
current upstream pins; a command declaring them is rejected before its handler runs. No
unsupported action classes are advertised.

## 3. Tool presentation

A native tool declares UI-only presentation data in tool metadata:

```python
from pydantic_ai.messages import ToolReturn
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset


async def echo(ctx, text: str):
    return ToolReturn(
        f"echo: {text}",  # model-facing
        metadata={
            "harness": {
                "presentation": {
                    "output": {
                        "rendererId": "example.echo",
                        "schemaVersion": 1,
                        "data": {"text": text},
                        "fallback": f"echo: {text}",
                    }
                }
            }
        },
    )


toolset = FunctionToolset(
    tools=[Tool(echo, name="example_echo", takes_ctx=True)],
    metadata={
        "harness": {
            "presentation": {
                "bindingId": "example",
                "input": {
                    "rendererId": "example.echo",
                    "schemaVersion": 1,
                    "data": {"region": "input"},
                    "fallback": "echo input",
                },
            }
        }
    },
)
```

A host-owned observer forwards validated snapshots over the negotiated
`_harness/presentation/updated` notification and stores final input/output presentation in
`ToolReturn.metadata`. UI-only data must never be placed in `return_value` or `content`,
which can reach the model.

Optional progress uses public `ctx.emit`:

```python
from harness.runtime.presentation import ToolPresentationProgressEvent

await ctx.emit(
    ToolPresentationProgressEvent(
        presentation={
            "rendererId": "example.echo",
            "schemaVersion": 1,
            "data": {"done": 1, "total": 3},
            "fallback": "echo 1/3",
        }
    )
)
```

Progress is live-only. Final input/output presentation is reconstructed passively after
`session/load` from committed native metadata. No tool, command, or model is re-run.

## 4. Local Textual views

Register a provider under `harness.renderers` as `<rendererId>@<schemaVersion>`:

```toml
[project.entry-points."harness.renderers"]
"example.git.status@1" = "example_status.tui:provider"
"example.echo@1" = "example_status.tui:provider"
```

```python
from textual.widgets import Static

from harness.client.tui.extensions import CommandViewState, ToolViewState, ViewContext, ViewHandle


def command_view(state: CommandViewState, context: ViewContext) -> ViewHandle:
    widget = Static(state.fallback)
    return ViewHandle(widget=widget, apply=lambda next_state: widget.update(next_state.fallback))


class Provider:
    command_view = staticmethod(command_view)
    # tool_view is independent; omit it and tool calls use the generic body.
```

- `command_view` and `tool_view` are independent factories for the exact renderer id and
  schema version. A command never falls back to a tool view.
- Widgets are ordinary Textual widgets. Plugins own children, CSS, focus, messages and
  workers. Harness mounts the widget inline and applies successive immutable snapshots to the
  same instance.
- `ViewContext.invoke_command(binding_id, command_name, arguments)` submits a structured
  plugin command through the same host route as slash invocation. Mounting, replaying and
  applying state never submits actions.
- Widget interactions are rechecked against the current session, enabled binding and active
  revision. A stale or replayed widget cannot target another session.
- Missing provider/version/factory/`apply` failures degrade only that block to sanitized
  fallback text. Installed widgets run in-process; this is not a security sandbox, and
  plugin-owned event handlers remain the author's responsibility.

## Supported boundaries

- Inline output only: no plugin screens, dialogs, marketplace or auto-installer.
- Direct command output is retained only in the current bounded UI session. `session/load`
  replays the upstream transcript, not a command journal.
- Read-only history inspection and `StartTurn` are available. Writable history operations
  (clear/compact/edit) require a public upstream ACP replacement operation that these pins do
  not provide.
- Standard ACP agent-advertised slash commands still use `session/prompt`. That is protocol
  interoperability, not a compatibility path for Harness plugin commands.
- No remote code loading: remote peers send JSON identifiers and data only.

See [examples/plugins/example_status](../examples/plugins/example_status) for a complete
installable plugin.
