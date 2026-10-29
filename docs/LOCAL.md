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

Use `--description "Search repositories, issues, and pull requests"` when adding a
server, or set `description` in its TOML table, to tell the model when to load it.

The first `npx` launch may spend time downloading the package, so give those servers a
generous timeout (`--timeout 60`, or `timeout = 60.0` in the TOML). Later starts reuse the
local npm cache and are near-instant.

```toml
[mcp]
token_store = "file"

[mcp.servers.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest"]
timeout = 20.0

[mcp.servers.github]
url = "https://api.githubcopilot.com/mcp/"
defer_loading = true
description = "Search repositories, issues, and pull requests."

[mcp.servers.github.headers]
Authorization = "Bearer {env:GH_TOKEN}"
```

## Account login (GitHub Copilot)

Subscription providers can authenticate with the account itself instead of an API key.
Local mode supports GitHub Copilot through GitHub's OAuth device flow: the runtime asks
GitHub for a short code, you approve it in the browser, and the token is written to the
same OS keyring entry a pasted key would use.

```sh
hrns login github-copilot
```

The command prints the verification address and code, waits for approval, lists the
model ids this plan can use, and stores the token as
`keyring:hrns.local/github-copilot`. Save the provider once:

```toml
provider = "github-copilot"
model = "claude-sonnet-4.5"
```

In the terminal, press **l** in the connection picker and add an entry named
`github-copilot` (or open `/providers` in a session), then press **Ctrl+G**. Approving the
code closes the login window, and the ids this account can reach are offered by `/models`
from then on. `hrns login` without a provider signs in to the active entry.

Tokens are not refreshed by hrns: if GitHub expires or revokes one, run the login again.
No scopes are requested. The login defaults to GitHub Copilot's public OAuth client
(`Iv1.b507a08c87ecfe98`), because Copilot grants the current model catalog only to
tokens issued to a recognized client; a token from an unrelated OAuth app sees the
legacy model ids and is refused premium ones. To use a different app, set **Copilot
client ID** in the provider form (shown for a `github-copilot` entry) or
`GITHUB_OAUTH_CLIENT_ID`; device flow must be enabled on it.

Copilot serves two APIs, and every id is assigned to one of them: Pydantic AI's
`GitHubCopilotModel` speaks Chat Completions, while the GPT-5.6/6
`luna`/`sol`/`terra`/`astra` line, the Grok ids, and Microsoft's `mai-code-*` are on the
Responses API. Login lists both kinds. The runtime reads the account's catalog once per
process (`GET /models`, cached per token) and sends each id to the transport its
`supported_endpoints` names, so one provider covers both:

```toml
provider = "github-copilot"
model = "gpt-5.6-luna"
```

`github-copilot:gpt-5.6-luna` therefore builds an OpenAI Responses model on Copilot's
client and family profile, while `github-copilot:claude-sonnet-5` stays on Chat
Completions. Responses ids default to `openai_reasoning_summary = "auto"`, because
Copilot returns an empty reasoning summary unless one is requested and the terminal
would then show no Thinking block; set `model_settings.openai_reasoning_summary` in the
TOML to ask for `detailed` or `concise` instead. If the catalog cannot be read, ids stay on Chat Completions. Pydantic AI
does not ship a Copilot Responses transport yet
([pydantic-ai#8230](https://github.com/pydantic/pydantic-ai/issues/8230)).

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

[providers.deepseek]
api_key_env = "DEEPSEEK_API_KEY"
```

The top level selects the active entry and model; `[providers.<name>]` holds how to reach
that provider. An entry whose name is a Pydantic AI provider id needs no `type`, and an
entry with no key of its own reads the provider's usual environment variables.

Then open the terminal:

```sh
uv run hrns
```

Local mode is the default. `uv run hrns connect --local` selects it explicitly, and
`hrns run "..."` uses the same settings when no other target is given.

For UI setup, run `uv run hrns` and press **l**, or type `/providers` in a session. A
provider entry is one endpoint plus its credential: a unique name, the Pydantic AI
provider it uses, an optional base URL, and either a pasted key (kept in your OS keyring,
never written to the TOML file) or the *name* of an environment variable such as
`DEEPSEEK_API_KEY`. The name defaults to the provider id and is unique, so two endpoints
of the same provider can coexist: `openai-work` and `openai-home` both use `openai-chat`
with different URLs and keys. In the table, `a` adds, `e` edits and `d` deletes an entry
(deleting the active one clears the selection), and **Ctrl+G** signs in to a provider that
supports an account login — see [account login](#account-login-github-copilot).

`/models` lists every configured entry's models in one grouped, filterable list, active
entry first. The ids come from the installed Pydantic AI version, so focus the list and
type to filter it. Picking any model — from any provider — switches the session to it
**without restarting the agent**, and the reasoning-effort step follows when that model
can reason. Press `a` there to add a custom model id to the entry you are on: an id the
catalog does not know is remembered in `hrns.local.toml` under that entry only and is
offered from then on.

Edits to a provider entry (a new key, a new endpoint) reach a live session on its next
message, because `hrns-local` re-reads the configuration file rather than being
restarted. Use `/connect` to return to the connection picker, or `/effort` to change
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

[providers.openai-chat]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
```

## Local inference and other providers

For an already running local OpenAI-compatible server:

```toml
provider = "lmstudio"
model = "your-loaded-model-name"

[providers.lmstudio]
type = "openai-chat"
base_url = "http://127.0.0.1:1234/v1"
```

No key is needed for an unauthenticated local endpoint. The entry name is yours to
choose, which is how a second OpenAI-compatible endpoint gets its own URL and key. For
Ollama, use `provider = "ollama"`, the installed model name, and optionally a base URL in
`[providers.ollama]`. hrns does not download models or start an inference server.

Provider and model names are delegated to Pydantic AI without a hrns allowlist.
Install the relevant `pydantic-ai-slim` extra in the runtime environment and supply
the provider's documented credentials. For example, Anthropic requires the
`anthropic` extra and `ANTHROPIC_API_KEY`. Provider capabilities still depend on the
selected model; filesystem and shell use function tools, so choose a tool-capable model.

Use `[providers.<name>.options]` for additional provider constructor arguments (such as
cloud region/project settings), and `[model_settings]` for Pydantic AI model settings:

```toml
provider = "bedrock"
model = "anthropic.claude-sonnet-4-5"

[providers.bedrock.options]
region_name = "eu-west-1"

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
`[providers.<name>.options]`. Unsupported constructor options fail when a message is
sent; custom OpenAI-compatible endpoints should use an entry with `type = "openai-chat"`.

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
uv run hrns connect --local --client-io all
```

Local mode inherits your shell environment for provider authentication. File edits and
shell commands request permission through the existing ACP controls; reads are automatic,
and asking a question is not a workspace change. In the terminal, `/permissions` can
auto-approve read-only kinds (`reads`) or every offered allow option (`all`); `ask`
remains the safe default.
Headless execution requires `--allow-all`, `--deny-all`, or reports that interaction
is required. The local workspace starts at the selected project; it is **not a
sandbox**. Approved commands execute with your user's permissions.

`--client-io` decides what this terminal offers the agent it connects to: `off` (the default)
offers nothing, `read` serves `fs/read_text_file` in the session directory, and `all` also
serves writes and `terminal/*` so an accepting agent can run commands in this terminal
alongside its server shell. Enabling terminal support lets the agent run commands with your permissions
without a further prompt, so it is a per-connection choice; a profile stores it as `clientIo`.
On the agent side the `client.terminal` binding is what routes commands through that terminal,
and it is usable only when the server accepts client capabilities and the connected client
advertises support. Local model mode refuses client capabilities by default, so this binding
remains visible but greyed out in the Plugins tab. See
[client capabilities](ACP.md).

### Windows

Local mode runs natively on Windows. The workspace uses the Win32 filesystem API and runs
shell commands with PowerShell 7 (`pwsh`), which must be installed and on `PATH`; startup
reports a clear error when it is missing, and no fallback to Windows PowerShell 5.1 is
attempted. Because the ripgrep shortcut is a POSIX shell script, `list_files` and `grep` use
the built-in filesystem walk on Windows, and the agent is told to write PowerShell syntax in
shell commands. Approved commands still run with your user's permissions.

Sessions are stored in the platform's hrns user data directory by default.
Set `store = "/absolute/path/sessions.db"` to select another SQLite store. Existing
session history can be resumed through the normal session UI or
`hrns run --local --resume SESSION_ID ...`. The store uses a fresh `acp_sessions`
table; existing databases are opened without conversion.

If the client cannot find `hrns-local`, install the runtime in the same environment
or put its executable on `PATH`. Missing provider extras and credentials are reported
as startup errors. Explicit `--stdio` agents and remote profiles remain available.
