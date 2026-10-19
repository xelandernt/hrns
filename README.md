# Harness

**Define an agent once. Run it in Python, open it in a terminal, or host it remotely.**

Harness combines native Pydantic AI capabilities with policy-controlled sessions, an independent ACP terminal, and authenticated remote hosting. Agent behavior stays in ordinary Python; the terminal can also connect to standard ACP agents that do not use this runtime.

## Start developing

Requires **Python 3.11+**, macOS or Linux, and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked --all-packages
uv run pytest -q
```

The checked-in lockfile pins the tested upstream versions. The offline tests and demo require no model API key.

## Open the terminal

```sh
uv run harness connect --demo
```

The offline agent uses a real Pydantic AI `FunctionModel` and a managed native capability. Open the capability panel to disable its tone instructions; the next turn reports the changed composition. `uv run harness` opens the connection picker, and any independent ACP agent can be launched with:

```sh
uv run harness connect --stdio -- /path/to/agent --acp
```

Run the Python example to see the same capability enabled, disabled and re-enabled:

```sh
uv run python -m harness_runtime.examples
uv run python examples/custom_capability.py
```

With `just` installed, `just demo` opens the terminal and `just serve-demo` starts a loopback-only hosted demo. In another terminal, connect the independent client through the authenticated WebSocket transport:

```sh
export HARNESS_DEMO_TOKEN=harness-local-demo
uv run harness profiles add demo --server http://127.0.0.1:8080 \
  --agent demo.v1 --token env:HARNESS_DEMO_TOKEN
uv run harness agents list --server demo
uv run harness run --server demo --agent demo.v1 "Say hello through the host"
```

See [client usage](docs/CLIENT_API.md) for headless execution, profiles, approvals and the bridge.

## Packages

| Distribution | Purpose |
| --- | --- |
| `harness-protocol` | Versioned extension schemas and transport contracts |
| `harness-runtime` | Native agent definitions, plugin management, sessions and `harness-host` |
| `harness-client` | Textual terminal, scripted ACP runner and stdio bridge |
| `harness-server` | Authenticated deployment catalog and WebSocket worker hosting |

The client does not depend on Pydantic AI or installed agent plugins. Local agents run in a separate process. Hosted deployments launch the executable from their own resolved Python environment.

## Design essentials

- **Native composition:** enabled bindings contribute their complete Pydantic AI behavior—tools, instructions, hooks and settings.
- **Server-enforced policy:** availability, directional enable/disable permission, editable fields, dependencies and lifecycle apply to Python and protocol callers.
- **Turn-boundary changes:** selected configuration is persisted immediately; an active turn keeps its captured configuration.
- **Host-owned state:** sessions pin their deployment, retain history and configuration, and never silently retry an uncertain prompt.
- **Explicit resources:** workspace and provider bindings belong to the host. Requested sandbox failures never imply host execution.
- **Standard ACP first:** generic streaming content, tools and approvals work without project-specific extensions.

## Documentation

- [Implementation plan](PLAN.md) and [task checklist](tasks/todo.md)
- [Architecture](docs/ARCHITECTURE.md) and [shared contracts](docs/CONTRACTS.md)
- [Extension protocol and wire examples](docs/PROTOCOL.md)
- [Runtime Python API](docs/RUNTIME_API.md)
- [ACP host](docs/ACP.md) and [client usage](docs/CLIENT_API.md)
- [Remote server configuration](docs/SERVER.md)
- [Capability catalog and integration coverage](docs/CATALOG.md)
- [Workspace lifecycle](docs/WORKSPACES.md)
- [Security model](docs/SECURITY.md)
- [Concept compliance matrix](docs/COMPLIANCE.md)
- [Verification and support boundaries](docs/VERIFICATION.md)

## Checks and distribution builds

```sh
uv run ruff check .
uv run pytest -q
uv build --all-packages
```

Or run `just check` with [just](https://just.systems/) installed. CI exercises the same checks on macOS/Linux and Python 3.11–3.13. Wheels are written to `dist/` and can be installed separately with their declared dependencies.

External provider, sandbox and durability integrations need their upstream extras and credentials. See the coverage matrix for managed, locally verified and deployment-only distinctions.

## License

MIT. See [LICENSE](LICENSE).
