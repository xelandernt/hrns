# Local models and providers

Run the local agent on your machine with a cloud API or a locally served model.
The client launches `hrns-local` over ACP. The bundled `hrns.coder` capability is bound
by the built-in local definition and enabled by default: it provides file tools, content
search, and a shell in the launch directory. Reading the workspace needs no approval; writes,
edits, and commands are approved per call. The bundled `hrns.skills` capability is bound
next to it for Agent Skills, and `hrns.todo` keeps a session-scoped plan visible in the
terminal. Installed plugins remain available to explicit
`AgentDefinition` objects; managed bindings are selected when a session is created and stay
fixed for that conversation.

File mutations use `apply_changes(changes=[...])` for both single-file and multi-file
work. Each entry has a `path` and either `kind = "write"` with complete `content`, or
`kind = "edit"` with ordered `replacements` (`old_text`, `new_text`). Include each
resolved file only once; a batch can contain up to 64 files. `read_file` returns a
content hash that can be passed as `expected_hash` to guard the original content.

The complete batch is validated before any file is written. One approval covers the
listed changes, with per-file previews and results in the terminal. Files are committed
individually: a later execution failure reports which files completed, which failed,
and which were not attempted. A failed write may have partially changed that file;
the agent must read affected files before retrying. Batches are not transactions.

## MCP servers

The local agent can use [Model Context Protocol](https://modelcontextprotocol.io) servers.
Servers live in `hrns.local.toml` under `[mcp.servers.<name>]` and are managed from the
terminal, in the same shape as Codex and OpenCode:

```sh
# stdio: everything after -- is the launcher
hrns mcp add playwright -- npx -y @playwright/mcp@latest

# streamable HTTP / SSE, with a token from the environment
hrns mcp add github --url https://api.githubcopilot.com/mcp/ \
  --header 'Authorization=Bearer {env:GH_TOKEN}'

hrns mcp list
hrns mcp auth github      # OAuth 2.1 with dynamic client registration
hrns mcp logout github
hrns mcp disable github
hrns mcp remove github
```

`{env:NAME}` references are expanded at connection time, so tokens stay out of the TOML
file. OAuth tokens are written to JSON files under the user data directory
(`~/Library/Application Support/hrns/mcp-auth` on macOS); set
`[mcp] token_store = "keyring"` to use the operating-system keychain instead.

In the terminal, `/mcp` opens the **MCP** tab next to **Todos**, **Plugins** and **Sessions**. It shows
each server's connection state, target, and the tools it discovered; **Space** enables or
disables a server for the next turn. Adding, authenticating, and removing servers stay in
the CLI, so the panel never hides filesystem access behind a form.

Every server is one capability binding, enabled for new sessions by default. Its tools are
loaded eagerly; pass `--defer` (or `defer_loading = true` in the TOML) to keep them out of
the prompt until the model asks for them. MCP tool calls follow the same approval flow as
the coder's write and shell tools, and an unreachable server is reported in `/mcp` instead
of failing the whole turn.

The first `npx` launch may spend time downloading the package, so give those servers a
generous timeout (`--timeout 60`, or `timeout = 60.0` in the TOML). Later starts reuse the
local npm cache and are near-instant.```toml
[mcp]
token_store = "file"

[mcp.servers.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest"]
timeout = 20.0

[mcp.servers.github]
url = "https://api.githubcopilot.com/mcp/"
defer_loading = true

[mcp.servers.github.headers]
Authorization = "Bearer {env:GH_TOKEN}"
```

## Start with DeepSeek

Install the base package (DeepSeek and OpenAI-compatible models are included):

```sh
uv sync --locked
export DEEPSEEK_API_KEY='your-key'
```

Create `hrns.local.toml` in your project:

```toml
provider = "deepseek"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"
```

Then open the terminal:

```sh
uv run hrns
```

Local mode is the default. `uv run hrns connect --local` selects it explicitly, and
`hrns run "..."` uses the same settings when no other target is given.

For UI setup, run `uv run hrns` and press **l**, choose **Configure local model…**, or
type `/models` in a session. The provider and model fields offer the ids known to the
installed Pydantic AI version: focus a field to see the full list, and type to filter it.
What you type is the value, so a provider or model id that is not listed works too — the
row marks it `custom` so a typo is visible before saving. Paste an API key into the masked
**API key** field to store it in your operating system keyring; it is never written to the
TOML file. The **API key environment variable** field takes a variable *name* such as
`DEEPSEEK_API_KEY`, not the key itself. Enter an optional base URL too. **Save and connect**
(or **Save and apply** from a session) persists the non-secret configuration; from the
connection picker it starts a session in the current directory.

If the selected model is already advertised by `hrns-local`, `/models` applies it
to the next message without restarting the agent. Changing provider (or entering a new
key) saves the configuration and restarts `hrns-local`, then reloads the current
session. Use `/connect` to return to the connection picker, or `/effort` to change
reasoning effort live. `/plugins` opens the live plugins panel: toggle, configure,
or add bindings; `next_turn` changes apply to the next message, while `new_session`
bindings still require a new session.

Agent Skills are enabled in the local definition. Skills are discovered in
`.agents/skills` under the agent's working directory and in `~/.agents/skills`; set
the skills binding's `directories` in `/plugins` for a different library. Type `$` to
pick a skill and insert a `$skill-name` reference, or `/skills` to open the same picker.
The reference is resolved by the runtime, so it works in pasted prompts and headless
runs too; a name with no matching skill is left as plain text.

The bundled `hrns.todo` capability is enabled in the local definition. The model uses
`write_plan` to maintain a short checklist for multi-step work, replacing the complete
list at meaningful milestones. Progress updates can accompany independent work in the
same response, and completed steps require confirmed tool results. The
first plan of a session opens the **Todos** tab in the right drawer; later updates refresh
it in place. Close the drawer with `Esc` or `Ctrl+B`; `/todo` opens it again. The list is
scoped to the session and is not persisted across a runtime restart.

DeepSeek's native provider uses its OpenAI-compatible Chat Completions API and
provider-specific model behavior. See the [Pydantic AI DeepSeek documentation](https://pydantic.dev/docs/ai/models/deepseek/).
For an explicit compatible endpoint instead:

```toml
provider = "openai-chat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
```

## Local inference and other providers

For an already running local OpenAI-compatible server:

```toml
provider = "openai-chat"
model = "your-loaded-model-name"
base_url = "http://127.0.0.1:1234/v1"
```

No key is needed for an unauthenticated local endpoint. For Ollama, use
`provider = "ollama"`, the installed model name, and optionally its base URL.
hrns does not download models or start an inference server.

Provider and model names are delegated to Pydantic AI without a hrns allowlist.
Install the relevant `pydantic-ai-slim` extra in the runtime environment and supply
the provider's documented credentials. For example, Anthropic requires the
`anthropic` extra and `ANTHROPIC_API_KEY`. Provider capabilities still depend on the
selected model; filesystem and shell use function tools, so choose a tool-capable model.

Use `[provider_options]` for additional provider constructor arguments (such as
cloud region/project settings), and `[model_settings]` for Pydantic AI model settings:

```toml
[model_settings]
temperature = 0.2
thinking = "high"
```

`thinking` is Pydantic AI's unified reasoning-effort setting (`minimal`, `low`,
`medium`, `high`, `xhigh`, or `false` to disable it where the model allows). The initial
value becomes the session default. In the terminal, `/effort` changes it live; the new
value applies to the next request. Models without thinking support do not advertise the
option.

Use environment-based authentication for secrets instead of putting them in
`provider_options`. Unsupported constructor options fail at startup; custom
OpenAI-compatible endpoints should use `openai-chat`.

The local agent also bundles the Ask User capability, so the model can put structured
multiple-choice questions to you mid-run. It is on by default; turn it off for headless
runs, where nobody is there to answer:

```toml
[capabilities]
ask_user = false
```

## Configuration and execution

The shared `hrns.config` module uses **pydantic-settings** without importing
the runtime or UI. Precedence is explicit values, `HRNS_LOCAL_*` environment
variables, the selected TOML file, then defaults. Example environment names:
`HRNS_LOCAL_PROVIDER`, `HRNS_LOCAL_MODEL`, `HRNS_LOCAL_BASE_URL`,
`HRNS_LOCAL_API_KEY_ENV`, and `HRNS_LOCAL_API_KEY`. Dictionary settings use JSON
in environment variables. Raw `api_key` values are accepted only from the environment
or Python, never from TOML; use `api_key_env` in a file.

File selection is `--local-config PATH`, then `HRNS_LOCAL_CONFIG_FILE`, then
`./hrns.local.toml` if it exists, otherwise `local.toml` in the platform's hrns
user configuration directory. The UI shows the save path. Files are not merged;
an explicitly selected missing file or an unknown setting is an error.

```sh
uv run hrns connect --local-config /path/to/model.toml --cwd /absolute/project
uv run hrns run --local --allow-all "Run the project tests"
```

Local mode inherits your shell environment for provider authentication. File edits and
shell commands request permission through the existing ACP controls; reads are automatic,
and asking a question is not a workspace change. In the terminal, `/permissions` can
auto-approve read-only kinds (`reads`) or every offered allow option (`all`); `ask`
remains the safe default.
Headless execution requires `--allow-all`, `--deny-all`, or reports that interaction
is required. The local workspace starts at the selected project; it is **not a
sandbox**. Approved commands execute with your user's permissions.

Sessions are stored in the platform's hrns user data directory by default.
Set `store = "/absolute/path/sessions.db"` to select another SQLite store. Existing
session history can be resumed through the normal session UI or
`hrns run --local --resume SESSION_ID ...`. The store uses a fresh `acp_sessions`
table; existing databases are opened without conversion.

If the client cannot find `hrns-local`, install the runtime in the same environment
or put its executable on `PATH`. Missing provider extras and credentials are reported
as startup errors. Explicit `--stdio` agents and remote profiles remain available.
