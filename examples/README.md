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

## 2. Existing FastAPI application

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

## 3. Installable plugin example

`examples/plugins/example_status` is a complete package: a native echo tool, independent
user-only `status`/`ask` commands, and optional Textual views installed separately under
`hrns.renderers`.

```sh
uv pip install -e examples/plugins/example_status
uv pip install -e "examples/plugins/example_status[ui]"
```

See [its README](plugins/example_status/README.md) and
[EXTENSIONS.md](../docs/EXTENSIONS.md). No hrns source change is needed to recognize it.

## 4. Editable manifests

Definitions can be authored as `DefinitionManifest` schema v1 JSON/YAML with
`hrns.runtime.parse_manifest_text`, `manifest_from_file`, or `definition_to_manifest`.
Manifests reference plugins by `plugin_id`; every id and configuration is validated
against the plugins registered on the host before a session opens.

The bundled capability catalog was removed for rework. Its former inventory is recorded in
[CAPABILITIES.md](../CAPABILITIES.md); custom and installed plugins remain usable.
