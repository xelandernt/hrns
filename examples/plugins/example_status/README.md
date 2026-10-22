# Example Harness plugin

This package is the smallest complete extension: a native Pydantic AI tool, two
user-only commands, and optional Textual views. Harness needs no source changes to
recognize it.

## Install

```sh
uv pip install -e examples/plugins/example_status
uv pip install -e "examples/plugins/example_status[ui]"
```

The runtime descriptor provider is `harness.capabilities:example_status`. The local widget
provider is registered for `example.git.status@1` and `example.echo@1` under
`harness.renderers`.

## Use

Register the plugin in an agent definition and bind it as `example`:

```python
from __future__ import annotations

from harness.runtime import AgentDefinition, DefinitionBinding, PluginRegistry

registry = PluginRegistry()
registry.discover()

definition = AgentDefinition(
    agent_id="example",
    name="Example",
    bindings=[DefinitionBinding(plugin_id="example_status", binding_id="example")],
)
```

- `/example:status` prints a user-only inline status block. It makes no model request,
  adds no tool event, and does not enter model history.
- `/example:ask` explicitly requests one normal agent turn.
- The `example_echo` agent tool shows a separate tool body; its UI-only payload stays in
  tool metadata and is not model-facing.

Install the package on the client for custom widgets. Without it (or without the `ui`
extra) the same commands and tools show their readable fallback text.
