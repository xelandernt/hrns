# Local models and providers

Run the developer agent on your machine with a cloud API or a locally served model.
The client launches `harness-local` over ACP; the agent offers filesystem, shell,
planning, repository context, skills, and the existing capability panel.

## Start with DeepSeek

Install the OpenAI extra (also used by DeepSeek):

```sh
uv sync --locked --extra client --extra runtime --extra openai
export DEEPSEEK_API_KEY='your-key'
```

Create `harness.local.toml` in your project:

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
type `/models` in a session. The provider and model fields list the ids known to the
installed Pydantic AI version; both also have optional override fields for a custom
provider or model id. Paste an API key into the masked **API key** field to store it in
your operating system keyring; it is never written to the TOML file. The **API key
environment variable** field takes a variable *name* such as `DEEPSEEK_API_KEY`, not
the key itself. Enter an optional base URL too. **Save and connect** (or **Save and
apply** from a session) persists the non-secret configuration; from the connection
picker it starts a session in the current directory.

If the selected model is already advertised by `harness-local`, `/models` applies it
to the next message without restarting the agent. Changing provider (or entering a new
key) saves the configuration and restarts `harness-local`, then reloads the current
session. Use `/connect` to return to the connection picker, or `/plugins` to toggle
capability bindings in place.

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
Harness does not download models or start an inference server.

Provider and model names are delegated to Pydantic AI without a Harness allowlist.
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
`medium`, `high`, `xhigh`, or `false` to disable it where the model allows). In the
terminal, `/models` offers the ladder after selecting a model that supports reasoning,
and `/effort` reopens it later; changes apply to the next message. Models without
thinking support ignore the setting.

Use environment-based authentication for secrets instead of putting them in
`provider_options`. Unsupported constructor options fail at startup; custom
OpenAI-compatible endpoints should use `openai-chat`.

## Configuration and execution

The shared `harness.config` module uses **pydantic-settings** without importing
the runtime or UI. Precedence is explicit values, `HARNESS_LOCAL_*` environment
variables, the selected TOML file, then defaults. Example environment names:
`HARNESS_LOCAL_PROVIDER`, `HARNESS_LOCAL_MODEL`, `HARNESS_LOCAL_BASE_URL`,
`HARNESS_LOCAL_API_KEY_ENV`, and `HARNESS_LOCAL_API_KEY`. Dictionary settings use JSON
in environment variables. Raw `api_key` values are accepted only from the environment
or Python, never from TOML; use `api_key_env` in a file.

File selection is `--local-config PATH`, then `HARNESS_LOCAL_CONFIG_FILE`, then
`./harness.local.toml` if it exists, otherwise `local.toml` in the platform's Harness
user configuration directory. The UI shows the save path. Files are not merged;
an explicitly selected missing file or an unknown setting is an error.

```sh
uv run hrns connect --local-config /path/to/model.toml --cwd /absolute/project
uv run hrns run --local --allow-all "Run the project tests"
```

Local mode inherits your shell environment for provider authentication. All function
tools request permission through the existing ACP controls, including reads, edits,
and commands. In the terminal, `/permissions` can auto-approve read-only kinds
(`reads`) or every offered allow option (`all`); `ask` remains the safe default.
Headless execution requires `--allow-all`, `--deny-all`, or reports that interaction
is required. The local workspace starts at the selected project; it is **not a
sandbox**. Approved commands execute with your user's permissions.

Sessions are stored in the platform's Harness user data directory by default.
Set `store = "/absolute/path/sessions.db"` to select another SQLite store. Existing
session history and capability settings can be resumed through the normal session UI
or `hrns run --local --resume SESSION_ID ...`.

If the client cannot find `harness-local`, install the runtime in the same environment
or put its executable on `PATH`. Missing provider extras and credentials are reported
as startup errors. Explicit `--stdio` agents and remote profiles remain available.
