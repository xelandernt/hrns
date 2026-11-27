# Configuration

`hrns` stores profiles, providers, and preferences in one configuration document.
The client and server share its schema and validation rules; invalid settings
report the file and field that need correction.

## Where the file is

| Location | Path |
| --- | --- |
| User document | `config.toml` in the platform config directory (macOS: `~/Library/Application Support/hrns/config.toml`, Linux: `~/.config/hrns/config.toml`, Windows: `%LOCALAPPDATA%\hrns\config.toml`) |
| Explicit document | `--config PATH` or `HRNS_CONFIG_FILE=PATH` |

Harness reads and writes the user document by default. An explicit path selects
the document for both operations.

`hrns profiles path` prints the file in effect.

## Precedence

Highest first:

1. Command-line options (`--model`, `--server`, `--stdio`, …).
2. `HRNS_*` environment variables.
3. The selected configuration file.
4. Built-in defaults.

## A complete example

```toml
# Which model the built-in agent starts with.
provider = "deepseek"
model = "deepseek-flash"

# Where the built-in agent keeps sessions (default: local-sessions.db in the data directory).
store = "~/Library/Application Support/hrns/local-sessions.db"

[providers.deepseek]
type = "deepseek"                      # Provider type; defaults to the entry name
api_key_env = "DEEPSEEK_API_KEY"       # which environment variable holds the key
custom_models = ["deepseek-flash"]     # model ids to offer besides the catalog's
options = { timeout = 60 }             # extra keyword arguments for the provider

[providers.local-gw]
type = "openai"
base_url = "http://127.0.0.1:11434/v1"

[local_model_effort]
"deepseek:deepseek-flash" = "high"

[thinking]
levels = ["low", "medium", "high"]     # which reasoning efforts sessions may select
allow_off = false                      # require reasoning on models with an effort option

[skills]
enabled = true
paths = ["~/shared/skills"]            # extra libraries; defaults are .agents/skills
exclude = ["experimental-release-notes"]

[mcp]
token_store = "file"

[mcp.servers.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest"]
enabled = true

[tui]
theme = "hrns"
auto_expand_tools = false
show_thoughts = false
client_io = "all"                      # off | read | all
sidebar_width = 76
last_connection = "builtin"

[profile.team]
transport = "server"
url = "wss://agents.example.com/acp/agent"
token = "env:TEAM_TOKEN"
remote_cwd = "/workspace"
default = true

[profile.helper]
transport = "stdio"
command = "my-agent"
args = ["--acp"]
cwd = "~/src/agent"
env = { MY_AGENT_MODE = "fast" }
client_io = "read"
notes = "scratch agent"
```

## Client reference

### `[tui]`

Terminal defaults and the last connection. `theme` and `client_io`
seed the native client when it has no stored value; the client keeps later changes in its
own state file. `auto_expand_tools`, `show_thoughts` and `sidebar_width` are accepted for
older clients; the native client stores those toggles in its state file instead.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `theme` | string | `"hrns"` | `hrns`, `textual-dark`, `textual-light`, `ansi-dark`, or `ansi-light` |
| `auto_expand_tools` | bool | `false` | Render every tool result expanded |
| `show_thoughts` | bool | `false` | Stream the agent's reasoning expanded; collapsed reasoning headers remain visible when false |
| `client_io` | `off` \| `read` \| `all` | `"all"` | What this terminal offers the agent: nothing, filesystem reads, or reads plus writes and terminal commands |
| `sidebar_width` | int | `52` | Side panel width in columns, clamped to 32–76 |
| `last_connection` | string | `""` | `builtin` or a profile name: the connection a bare `hrns` reopens. Written automatically on connect |

New sessions start in Ask. Use `/permissions ask`, `/permissions reads` (auto-approve
read-only tools), or `/permissions all` (auto-approve eligible tool calls) in the
session. `/yolo` is an alias for `/permissions all`. When you reopen the session,
the TUI restores the mode saved on this device.

`client_io` advertises ACP client capabilities; it does not by itself give the agent
tools. The agent must install `acp_filesystem` / `acp_terminal` through
`attachment_config` (see [Mount ACP](mount-acp.md#client-filesystem-and-terminal)).
The shipped `hrns agent` does not, so `client_io` has no effect against it. An agent
that installs the toolsets, such as `examples/fastapi_app.py`, exposes `read_file`,
`write_file`, and `run_command` when the client uses `"all"`. With `"read"`, writes
fall back to the server's own filesystem instead of the client's.

On a `server` profile, `remote_cwd` is the workspace path sent to the agent. The
current `client_io` implementation also uses that string as its local root, so a
remote path that does not exist on the client makes `fs/*` requests fail. Until the
root is decoupled from `remote_cwd`, omit it or set it to a path that also exists
locally.

### `[profile.<name>]`

A saved connection. The table key is the profile's name.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `transport` | `stdio` \| `server` | `"stdio"` | Launch a subprocess, or connect to a WebSocket endpoint |
| `command` | string | `""` | Executable to launch (stdio) |
| `args` | list of strings | `[]` | Arguments for `command` |
| `cwd` | string | `""` | Working directory for the agent (stdio); defaults to the launch directory |
| `env` | table of strings | `{}` | Environment for the subprocess. Values may be literal or a secret reference |
| `url` | string | `""` | ACP WebSocket endpoint (server); `wss://` for remote hosts, `ws://` only for loopback |
| `agent` | string | `""` | Optional agent label shown beside a remote profile |
| `token` | string | `""` | Bearer token or secret reference (server) |
| `remote_cwd` | string | `""` | Workspace path passed directly to a remote agent |
| `auth_method` | string | `""` | Agent-advertised auth method id to try automatically |
| `client_io` | `off` \| `read` \| `all` | unset | Overrides `[tui].client_io` for this connection |
| `default` | bool | `false` | Candidate for a launch that names no profile |
| `notes` | string | `""` | Free-form text carried with the profile |

## Agent reference

The built-in agent (`hrns agent`, `hrns agent --catalog`, and any `hrns.server` entry point
that builds the model) reads these.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `provider` | string | `""` | Name of the active `[providers]` entry |
| `model` | string | `""` | Bare model id for that entry; the provider lives in `provider` |
| `store` | path | `local-sessions.db` in the platform data directory | Session database for the built-in agent |

### `[providers.<name>]`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | string | entry name | Provider type (`openai-compatible`, `openai`, `anthropic`, `deepseek`, …). Empty reuses the entry name |
| `base_url` | string | `""` | `http(s)` endpoint for an OpenAI-compatible server. Credentials, query strings and fragments are rejected |
| `api_key_env` | string | `""` | Environment variable holding this entry's key |
| `copilot_client_id` | string | `""` | GitHub OAuth app for the Copilot device login; empty uses GitHub's public client |
| `custom_models` | list of strings | `[]` | Model ids to offer besides the catalog's; use bare ids separated from the provider name |
| `options` | table | `{}` | Extra keyword arguments for the provider constructor |

Two entries may use the same `type`, which is how `openai-work` and `openai-home` hold
different endpoints and keys under one provider implementation.

### `[model_settings]`

The built-in agent forwards ordinary request settings such as `max_tokens` to each
run. See the [Pydantic AI model settings reference](https://ai.pydantic.dev/api/settings/).

### Saved reasoning effort

`/effort` applies the choice to the current session. The terminal remembers it in
`[local_model_effort]` for Local mode or `[profile.<name>.model_effort]` for a named
connection. Use the model ID advertised over ACP. The built-in agent uses
`provider-entry:model` IDs.

```toml
[local_model_effort]
"openai:gpt-5.2" = "high"

[profile.work.model_effort]
"openai:gpt-5.2" = "low"
```

The terminal restores a saved choice in new sessions and after a model switch when
the server still offers it. A loaded session uses its stored ACP choice if available.

### `[thinking]`

The reasoning-effort policy controls which verified levels `hrns agent` advertises to
ACP clients. The model and its provider route determine the supported levels. A route
without verified effort levels uses the provider default. Explicit unsupported choices
receive a validation error with the valid values.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `levels` | list | `minimal`, `low`, `medium`, `high`, `xhigh`, `max` | Allowed effort levels, in display order |
| `allow_off` | bool | `true` | Offer `off` on models whose reasoning can be disabled |

Whenever the effort option is available, `default` lets the provider choose the
effort. An empty `levels` disables the effort option.

```toml
[thinking]
levels = ["low", "medium", "high"]
allow_off = false
```

`[model_settings].thinking` can set a server default effort. It accepts the same
values as the ACP option, including `default` and `off`. The server validates the
effective choice for the session's model before inference.

### `[mcp]`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `token_store` | `file` \| `keyring` | `"file"` | Where OAuth tokens for HTTP servers live |
| `token_store_path` | string | `""` | Directory for the `file` store; defaults to `mcp-auth` in the data directory |

Each server is a `[mcp.servers.<name>]` table. Exactly one of `command` (stdio) or `url`
(HTTP/SSE) is set.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `command` | string | `""` | Executable for a stdio server |
| `args` | list of strings | `[]` | Arguments for `command` |
| `env` | table of strings | `{}` | Environment for the subprocess; `{env:NAME}` is expanded |
| `cwd` | string | `""` | Working directory (stdio only) |
| `url` | string | `""` | `http(s)` endpoint (HTTP only) |
| `headers` | table of strings | `{}` | Request headers; `{env:NAME}` is expanded |
| `enabled` | bool | `true` | Whether the server is loaded for new sessions |
| `always_allow` | bool | `false` | Skip the client consent prompt when exporting over ACP |
| `description` | string | `""` | Description of this server |
| `include_tools` | list of strings | `[]` | Only these tools (mutually exclusive with `exclude_tools`) |
| `exclude_tools` | list of strings | `[]` | All tools except these |
| `timeout` | float | `10.0` | Seconds before a request to the server fails |
| `oauth` | bool or table | `true` | `true` uses OAuth with dynamic registration; `false` disables OAuth; a table sets `client_id`, `client_secret`, `scopes`, and `callback_port` |

The client offers enabled servers to local and remote agents that advertise
`mcpCapabilities.acp`. Native sessions use stdio or Streamable HTTP upstreams
that support MCP 2026-07-28. Configure HTTP credentials in `headers`.
The Rust terminal and Python client honor the token store and OAuth fields.
With the file store, native credentials live in the `native/` subdirectory
of the token store. With the keyring store, the native service is
`hrns.mcp.native`. The Linux keyring backend uses Secret Service. On Windows,
entries larger than 2,560 bytes require the file backend. For a
pre-registered native client, register the loopback redirect URI
`http://127.0.0.1:<callback_port>/callback` with the OAuth provider.

Exporting is still gated by the client's user. At session start an interactive client
asks whether the agent may use the server: allow once, allow for this connection, always
allow, or deny. Approval gates server startup and inclusion in the session declaration. `always_allow = true` skips the prompt and
persists the grant in this document; headless clients (`hrns run`) cannot ask, so
they only export servers that are pre-approved this way.

### `[skills]`

Agent Skills, described in full on the [Skills](skills.md) page. The default libraries
are `<workspace>/.agents/skills` and `~/.agents/skills` are read when they
exist.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Expose skills to the agent and to `$name` invocation |
| `paths` | list of paths | `[]` | Extra libraries, read before the defaults. Relative paths resolve against the working directory; `~` is expanded. Each explicit path must be an existing directory |
| `include` | list of strings | `[]` | Exact skill ids to expose; empty exposes every discovered skill |
| `exclude` | list of strings | `[]` | Exact skill ids to hide; use either `include` or `exclude` |

```toml
[skills]
paths = ["~/shared/skills"]
exclude = ["experimental-release-notes"]
```

Libraries are read in this order, highest precedence first: `paths` as written,
`<workspace>/.agents/skills`, `~/.agents/skills`. A duplicate skill name keeps the first
library's skill and bundled files. `enabled = false` removes the capability and the ACP
catalog entirely.

## Local provider setup

1. Open `/providers` and press **a** to add an entry. `/provider add` opens the same form.
2. Enter a name and provider type. Supply a base URL and custom model IDs for a
   custom endpoint. For Bedrock, set an AWS profile and region if you need to
   override boto3's defaults.
3. Save, highlight the entry, and press **Enter** to select it. Run `/login` and
   choose a sign-in method. With no selection, `/login` opens `/providers`.
4. Use `/models` to choose a model for the session.

| Provider type | Sign-in method |
| --- | --- |
| `bedrock` | Discover credentials through boto3 or start an AWS SSO session with AWS CLI v2. AWS manages the SSO cache. |
| `github-copilot` | Open the GitHub device verification URL and enter the code. |
| `openai-codex` | Open the browser authorization URL. The browser returns to Harness through localhost port 1455. |
| `deepseek` | Enter a DeepSeek API key. |
| `openai-compatible` | Enter an API key for the endpoint's Chat Completions API. |

In the provider list, press **e** to edit or **d** to delete the highlighted entry.
A Bedrock model ID can include a colon, such as `amazon.nova-pro-v1:0`.
The built-in local agent supports editing and sign-in. Remote agents can expose
routes through ACP's optional provider-routing capability.

## Secrets

`/login` stores API keys and account tokens in the OS keyring. The keyring
account is scoped to the provider entry, type, and configured base URL.
For environment-based setup, `api_key_env` names the variable the entry reads.

Connection profiles can resolve credentials from `env:NAME` or
`keyring:service/user`. Use literal values for non-secret settings such as `PATH`.

## Environment variables

Environment variables override runtime settings. The prefix is
`HRNS_`, and `__` reaches into a section:

```sh
HRNS_PROVIDER=deepseek \
HRNS_MODEL=deepseek-flash \
DEEPSEEK_API_KEY=... \
hrns agent --env-only
```

`HRNS_CONFIG_FILE` selects one document, `HRNS_API_KEY` supplies a key
directly (the client passes a keyring reference this way), and `HRNS_STORE`
overrides the session database. `HRNS_MODEL` and `HRNS_STORE` are read by the
terminal, the built-in agent, and `hrns serve`.

## Reading, writing, and validation

- The document is validated on every load; an unknown key, a wrong type, or a bad value
  (an invalid `base_url`, a model id containing `:`) stops both programs with the file and
  field named.
- `hrns` saves what changes: a profile edit, a new provider, the remembered connection,
  or an MCP `always_allow` consent. Values equal to their defaults are omitted, so the file stays
  close to what a person would write.
- Saving goes to the user document unless `--config` selected another file. A project
  document supplies read-only overrides.
- The client passes the same document to the built-in agent it spawns, so a model selected
  in the terminal is the one `hrns agent` runs.

## Managing the file from the CLI

```sh
hrns config                      # list the config subcommands
hrns config show                 # sources and the effective configuration
hrns config path                 # source files, one per line
hrns config get provider         # one dotted key, e.g. mcp.servers.playwright.url
hrns config keys                 # keyring accounts and environment sources
hrns profiles list               # saved connections from the document
hrns profiles add team --server wss://host/acp --token env:TEAM_TOKEN
hrns profiles remove team
hrns mcp add project-tools -- project-tools
hrns mcp add project-tools --always-allow -- project-tools
hrns mcp list
hrns login github-copilot
```

`hrns config show` prints the effective settings and identifies the document.
`--json` prints the same data as JSON, `--all`
includes defaults, and `--reveal` prints literal secrets that are otherwise shown as
`***`. `hrns config keys` lists each provider's keyring service/account and environment
variable name, plus whether a value is present.

`hrns clean --config` removes the document; `hrns clean` removes session history.
