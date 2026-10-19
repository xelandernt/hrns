# Harness

**Define an agent once. Run it in Python, open it in a terminal, or host it remotely.**

Harness combines native Pydantic AI capabilities with policy-controlled sessions, an independent ACP terminal, and authenticated remote hosting. Agent behavior stays in ordinary Python; the terminal can also connect to standard ACP agents that do not use this runtime.

## Start developing

Requires **Python 3.11+**, macOS or Linux, and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked --all-packages
uv run ruff check .
```

The checked-in lockfile pins the tested upstream versions.

## Use a local agent with your model

The local developer agent is the default connection mode. Install the provider
extra and set the API key for the provider you use:

```sh
uv sync --locked --all-packages --extra openai
export DEEPSEEK_API_KEY='your-key'
uv run hrns
```

Press **l** to configure a provider and model, or create `harness.local.toml`
in the project:

```toml
provider = "deepseek"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"
```

`uv run hrns` starts a session in the current directory using that model. The agent
works on files and runs commands with approval prompts. Configuration uses
pydantic-settings. See [local setup](docs/LOCAL.md) for OpenAI-compatible endpoints,
local inference, other Pydantic AI providers, and headless execution.

Any independent ACP agent can still be launched explicitly:

```sh
uv run hrns connect --stdio -- /path/to/agent --acp
```

See [client usage](docs/CLIENT_API.md) for headless execution, profiles, approvals
and the bridge.

## Packages

| Distribution | Purpose |
| --- | --- |
| `harness-protocol` | Versioned extension schemas and transport contracts |
| `harness-config` | Shared local settings using pydantic-settings, without AI or UI dependencies |
| `harness-runtime` | Native agent definitions, plugin management, sessions, `harness-host` and `harness-local` |
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
uv build --all-packages
```

Or run `just check` with [just](https://just.systems/) installed. CI exercises the same checks on macOS/Linux and Python 3.11–3.13. Wheels are written to `dist/` and can be installed separately with their declared dependencies.

External provider, sandbox and durability integrations need their upstream extras and credentials. See the coverage matrix for managed, locally verified and deployment-only distinctions.

## License

MIT. See [LICENSE](LICENSE).
