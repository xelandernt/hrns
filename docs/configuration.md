# Configuration

`hrns` keeps one document. The terminal client and the local runtime both read it, so a
profile, a provider, and a preference live in the same file and are validated by the same
pydantic-settings model. A typo anywhere fails both programs the same way instead of
being silently ignored by one of them.

## Where the file is

| Location | Path |
| --- | --- |
| User document | `config.toml` in the platform config directory (macOS: `~/Library/Application Support/hrns/config.toml`, Linux: `~/.config/hrns/config.toml`, Windows: `%LOCALAPPDATA%\hrns\config.toml`) |
| Project document | `hrns.toml` in the working directory |
| Explicit document | `--config PATH` or `HRNS_LOCAL_CONFIG_FILE=PATH` |

The user document is the one `hrns` writes. A project document only overrides the keys it
names, so a repository can add one profile or provider without copying the rest.
`--config` reads exactly that file and disables the merge; it is what `hrns-local --config`
receives when a profile must pin one file.

`hrns profiles path` prints the file in effect.

## Precedence

Highest first:

1. Command-line options (`--model`, `--server`, `--stdio`, …).
2. `HRNS_LOCAL_*` environment variables.
3. The project document (`./hrns.toml`).
4. The user document (`config.toml`).
5. Built-in defaults.

Files merge key by key: setting `provider = "local-gw"` in a project document keeps the
user document's providers that the project does not mention. Sections merge the same way,
so a project file can add `[profile.repo]` next to the user's `[profile.team]`.

## A complete example

```toml
# Which local model the runtime starts with.
provider = "deepseek"
model = "deepseek-flash"

# Where the runtime keeps sessions (default: local-sessions.db in the data directory).
store = "~/Library/Application Support/hrns/local-sessions.db"

[providers.deepseek]
type = "deepseek"                      # Pydantic AI provider id; defaults to the entry name
api_key_env = "DEEPSEEK_API_KEY"       # which environment variable holds the key
custom_models = ["deepseek-flash"]     # model ids to offer besides the catalog's
options = { timeout = 60 }             # extra keyword arguments for the provider

[providers.local-gw]
type = "openai"
base_url = "http://127.0.0.1:11434/v1"

[model_settings]
thinking = "high"                      # forwarded to Pydantic AI ModelSettings

[thinking]
levels = ["low", "medium", "high"]     # which reasoning efforts sessions may select
allow_off = false                      # do not offer "off" on models that can disable it

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
permission_mode = "all"                # ask | reads | all
client_io = "all"                      # off | read | all
sidebar_width = 76
last_connection = "local"

[profile.team]
transport = "server"
url = "wss://agents.example.com/acp"
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

Terminal preferences. The client writes this section; the runtime ignores it.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `theme` | string | `"hrns"` | `hrns`, `textual-dark`, `textual-light`, `ansi-dark`, or `ansi-light` |
| `auto_expand_tools` | bool | `false` | Render every tool result expanded |
| `show_thoughts` | bool | `false` | Render the agent's reasoning |
| `permission_mode` | `ask` \| `reads` \| `all` | `"ask"` | `ask` surfaces every approval; `reads` auto-answers read-only tools; `all` auto-approves any call the agent offered an allow option for (not a sandbox) |
| `client_io` | `off` \| `read` \| `all` | `"all"` | What this terminal offers the agent: nothing, filesystem reads, or reads plus writes and terminal commands |
| `sidebar_width` | int | `52` | Side panel width in columns, clamped to 32–76 |
| `last_connection` | string | `""` | `local` or a profile name: the connection a bare `hrns` reopens. Written on connect, not meant to be hand-edited |

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
| `remote_cwd` | string | `""` | Workspace path sent to a remote agent; never rewritten locally |
| `auth_method` | string | `""` | Agent-advertised auth method id to try automatically |
| `client_io` | `off` \| `read` \| `all` | unset | Overrides `[tui].client_io` for this connection |
| `default` | bool | `false` | Candidate for a launch that names no profile |
| `notes` | string | `""` | Free-form text carried with the profile |

## Runtime reference

The local runtime (`hrns-local`, `hrns-local --catalog`, and any `hrns.app` entry point
that builds the local model) reads these.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `provider` | string | `""` | Name of the active `[providers]` entry |
| `model` | string | `""` | Bare model id for that entry; the provider lives in `provider` |
| `store` | path | `local-sessions.db` in the platform data directory | Session database for the runtime |

### `[providers.<name>]`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | string | entry name | Pydantic AI provider id (`openai`, `anthropic`, `deepseek`, `gateway/openai`, …). Empty reuses the entry name |
| `base_url` | string | `""` | `http(s)` endpoint for an OpenAI-compatible server. Credentials, query strings and fragments are rejected |
| `api_key_env` | string | `""` | Environment variable holding this entry's key |
| `copilot_client_id` | string | `""` | GitHub OAuth app for the Copilot device login; empty uses GitHub's public client |
| `custom_models` | list of strings | `[]` | Model ids to offer besides the catalog's; must not contain `:` |
| `options` | table | `{}` | Extra keyword arguments for the provider constructor |

Two entries may use the same `type`, which is how `openai-work` and `openai-home` hold
different endpoints and keys under one provider implementation.

### `[model_settings]`

Forwarded as `ModelSettings` to every run. `thinking` is special: a level
(`minimal`, `low`, `medium`, `high`, `xhigh`) enables reasoning, `false` disables it, and
omitting it keeps the provider default. The TUI's `/effort` writes this key when connected
to `hrns-local`; for any other agent it changes the session's `effort` config option
instead, because only the server that owns this file can persist it.

This is the baseline every session starts from. A client that changes reasoning effort
over ACP overrides it for its own session.

### `[thinking]`

The reasoning-effort policy `hrns-local` advertises to ACP clients. It is a server-side
allowlist: the runtime intersects it with the model's own profile, so a level the model
cannot use is never offered, and a value outside the list is rejected rather than clamped.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `levels` | list | all five levels | Values the `effort` option offers, in display order |
| `allow_off` | bool | `true` | Offer `off` on models whose reasoning can be disabled |

`default` is always offered and means "send no `thinking` setting". An empty `levels`
removes the effort option entirely. Listing a value that is not one of `minimal`, `low`,
`medium`, `high`, `xhigh` is a configuration error.

```toml
[thinking]
levels = ["low", "medium", "high"]
allow_off = false
```

If `[model_settings].thinking` names a level the policy does not offer for the startup
model, `hrns-local` refuses to start rather than running with an effort no client can see.

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
| `defer_loading` | bool | `false` | Advertise the server's tools only after the model asks for them |
| `description` | string | `""` | When the model should load a deferred server |
| `include_tools` | list of strings | `[]` | Only these tools (mutually exclusive with `exclude_tools`) |
| `exclude_tools` | list of strings | `[]` | All tools except these |
| `timeout` | float | `10.0` | Seconds before a request to the server fails |
| `oauth` | bool or table | `true` | `true` uses OAuth with dynamic registration; `false` never tries it; a table sets `client_id`, `client_secret`, `scopes`, and `callback_port` |

## Secrets

Never write a key into the document. Use a reference:

- `env:NAME` — read the process environment.
- `keyring:service/user` — read the OS keyring; `hrns login` stores account tokens under
  `hrns.local/<entry>`.
- Anything else is a literal, which is only appropriate for non-secrets such as `PATH`.

`api_key_env` names the variable a provider entry reads; the keyring wins over pasted keys
for the same entry, so a login and a saved key cannot disagree.

## Environment variables

Runtime settings can be overridden without editing the file. The prefix is
`HRNS_LOCAL_`, and `__` reaches into a section:

```sh
HRNS_LOCAL_PROVIDER=deepseek \
HRNS_LOCAL_MODEL=deepseek-flash \
DEEPSEEK_API_KEY=... \
hrns-local --env-only
```

`HRNS_LOCAL_CONFIG_FILE` selects one document, `HRNS_LOCAL_API_KEY` supplies a key
directly (the client passes a keyring reference this way), and `HRNS_LOCAL_STORE`
overrides the session database. `HRNS_MODEL` and `HRNS_STORE` belong to the other entry
points (`hrns.app serve`, the FastAPI mount) and are deliberately not read here.

## Reading, writing, and validation

- The document is validated on every load; an unknown key, a wrong type, or a bad value
  (an invalid `base_url`, a model id containing `:`) stops both programs with the file and
  field named.
- `hrns` saves what changes: a profile edit, a new provider, a TUI preference, or the
  remembered connection. Values equal to their defaults are omitted, so the file stays
  close to what a person would write.
- Saving goes to the user document unless `--config` selected another file. A project
  document is read, never written.
- The client passes the same document to the runtime it spawns, so a local model selected
  in the TUI is the one `hrns-local` runs.

## Managing the file from the CLI

```sh
hrns profiles list                  # saved connections from the document
hrns profiles add team --server wss://host/acp --token env:TEAM_TOKEN
hrns profiles remove team
hrns mcp add playwright -- npx -y @playwright/mcp@latest
hrns mcp list
hrns login github-copilot
```

`hrns clean --config` removes the document; `hrns clean` removes session history.
