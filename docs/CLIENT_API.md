# Client usage

`hrns.client` is a standalone ACP v1 client module. It does **not** import `hrns.runtime`, Pydantic AI, or any installed agent plugin, so the same installation can drive a project host, an editor-style third-party agent, or a hosted deployment.

- [Terminal](#terminal)
- [CLI](#cli)
- [Local developer agent](#local-developer-agent)
- [Profiles and secrets](#profiles-and-secrets)
- [Python API](#python-api)
- [Interactions in headless mode](#interactions-in-headless-mode)
- [Local renderers](#local-renderers)
- [Bridge and SSH](#bridge-and-ssh)
- [Failure behavior](#failure-behavior)

## Terminal

```sh
hrns                                      # configured local model (default)
hrns connect --local-config model.toml    # explicit local settings file
hrns connect --stdio -- my-agent --acp    # any stdio ACP agent
hrns connect --server team --agent research-assistant
```

The full-screen application has a streaming transcript, multiline composer, session list, plugin/configuration drawer, activity drawer, inline approval bar, structured elicitation dialog, and an inline Ask User dock above the composer (tabs per question, arrow/number selection, multi-select, and a typed custom answer) that keeps the transcript visible. `Ctrl+Q` or `Ctrl+C` quits. The side panel is closed by default; `/plugins`, `/sessions`, `F2`/`F3`/`F4`, or `Ctrl+B` opens it as a drawer over the right edge without shrinking the transcript, and `Esc` closes it and returns focus to the composer. Unsupported ACP features are absent, not shown as broken controls. In every enumerated control, `Left`/`Right` changes the value in place: binary controls toggle, longer lists step and wrap, and pick lists move the highlight. Text fields keep the arrows for cursor movement.

Tool approvals are non-modal: a bar above the composer shows the tool call and the agent's options while the transcript stays visible. `y` allows once, `a` always allows that exact call, `n` rejects once, and `Esc` stops the turn and returns focus to the composer so the next message can redirect the agent. An agent's `allow_always` is scoped to the same tool and arguments; it is narrower than a command-prefix rule. `/permissions` selects `ask` (every request), `reads` (auto-approve read, search and think; ask for everything else), or `all` (auto-approve any offered allow option; this is **not** a sandbox). The status bar always shows the active mode; an automatic decision adds no transcript banner, because the tool card already names the call that ran.

Stage explicit file attachments with `/attach PATH`; `/attach list` shows staged files and `/attach clear` removes them. They are sent as content with the next message and the transcript labels the attachment names. `/attach` never grants the agent a local filesystem path.

The terminal can also serve the agent's own file and command calls. `/settings` → **Client-side tools** chooses `off` (default: nothing advertised), `read` (`fs/read_text_file` inside the session directory), or `all` (adds `fs/write_text_file` and `terminal/*`, so an accepting agent can run commands here alongside the host's shell). The choice is advertised on `initialize`, so it applies to the **next connection**: the completion notice says so, and the status bar shows `io read` or `io all` when something is offered. Precedence is `--client-io` on the command line, then `clientIo` in the connection profile, then the `/settings` value. An agent reaches these endpoints through its own binding (`client.terminal`), which the plugins drawer lists under Client plugins, separately from Server plugins, and greys out with a reason whenever the server refuses client capabilities or the connected client does not advertise support. Local model mode refuses client capabilities by default. Serving commands means the agent runs them as you with no ACP approval prompt, which is why the default is `off` and why the agent still approves the call first. The grant covers whatever machine the terminal runs on: for a `--server` connection the agent is remote while the files and commands are local, so enable it only for deployments you would let touch this machine.

Type `@` in the composer to fuzzy-find and attach a client-side workspace file. Local connections use the session workspace; remote connections use the client's working directory. File discovery honors ripgrep ignore rules when available and is capped at 10,000 entries. Staged files remain visible with size and media type; select one to remove it. Relative `/attach` paths use the same client-side workspace.

Submitting while a turn runs queues a follow-up with its own attachments (up to 20 pending messages). Use **Edit** or **Remove** on the selected queue item. Successful turns send pending messages in order; cancellation, errors, and restored sessions pause the queue until **Resume queue** is selected. Editing requires an empty composer so it cannot overwrite a draft. Prompts, drafts, attachments, and pending queues are kept per session in memory for the lifetime of the app; `Up`/`Down` at editor boundaries recall submitted prompts and restore the unfinished draft.

Scrolling upward pauses transcript following. The **New output** button or `Ctrl+End` jumps to the latest output and resumes following. `Alt+Up`/`Alt+Down` jump between user messages. `Ctrl+F` or `/find QUERY` searches retained messages, tool details, and reasoning; selecting a result jumps to its block. `/copy` copies the latest completed assistant response; every completed response also fades in a right-aligned **Copy response** action. Clipboard copying uses the terminal's clipboard support. In the sessions panel, `/` focuses the title/workspace filter and `Enter` returns to the results; `Esc` closes the panel. The current session is marked with `●`, and timestamps show relative ages.

The plugins drawer lists offered bindings with their on/off state and surfaces locked reasons in the status line. A hrns host advertises revision-checked configuration updates: `Space` toggles an offered binding, `e` edits allowed fields, and successful edits apply at the next turn boundary. Model and reasoning-effort changes use standard ACP configuration options. `Ctrl+B` shows or hides the side panel, `F2`/`F3`/`F4` jump to plugins, sessions, or activity, `Tab`/`Shift+Tab` cycle those panels while focus is inside the drawer, and `Esc` closes it.

In the plugins panel, `a` adds a binding only when the peer advertises `_hrns/plugins/list` and local binding creation. Local stdio hosts advertise both; embedded hosted connections disable creation. A plugin's `new_session` lifecycle can only be chosen at session creation.

Command palette and slash commands: `/connect`, `/providers`, `/models`, `/plugins`, `/sessions`, `/find`, `/copy`, `/permissions`, `/settings`, `/attach`, `/help`. `/model` and `/capabilities` remain aliases. Typing `/` opens an inline filtered picker; agent-advertised commands appear alongside local ones, and collision-safe invocation is `/agent <command>`. Slash commands never substitute prompt instructions for an unsupported capability edit.

## CLI

```sh
# One prompt, human-readable or stable JSON output
hrns run "Summarize this repository"
hrns run --stdio -- /path/to/agent -- "Review this repository"
hrns run --server team --agent research-assistant --json "Compare A and B"

# Attachments are an explicit content upload, never implicit filesystem access
hrns run --attach ./notes.md --attach ./diagram.png "Review these"

# Continue a saved session; never automatically resends an uncertain prompt
hrns run --server team --agent research-assistant --resume ses_123 "Continue"

# Deployment discovery
hrns agents list --server team
hrns agents list --server team --all --json
hrns agents show --server team research-assistant

# Profile management
hrns profiles list
hrns profiles add local --stdio -- /usr/bin/my-agent --acp
hrns profiles add team --server https://agents.example --agent research-assistant --token env:HRNS_TOKEN

# Sign in to a local provider with an account login (device flow)
hrns login github-copilot

# Delete local session history (asks first; --dry-run previews, --yes skips)
hrns clean

# Delete session history and the local provider config, keys included
hrns clean --yes --config --keys
```

`hrns clean` removes the local developer agent's session database and the host
runtime's default SQLite store, together with their WAL sidecars. It never
touches saved connection profiles or sessions hosted on a remote server, and it
leaves the provider configuration alone unless you select it. The command lists
everything it can delete, then asks per target: session history, the provider
configuration (the project `hrns.local.toml` and the user `local.toml`), and —
when that configuration declares entries whose keys are in the OS keyring — the
stored credentials. Use `--dry-run` to only list. In scripts, `--yes` skips the
session prompt and `--config` selects the configuration; it is the only way to
delete it without a terminal, and `--keys` also clears the keyring secrets of the
entries in those files. A PostgreSQL `HRNS_STORE` is left untouched.

`hrns run` accepts multiple text prompt words. An explicit `--stdio` target uses
`-- CMD ARGS -- PROMPT`; local-model and remote targets treat the remainder as the
prompt. Exit codes are `0` success, `1` failure, `2` usage, and `3` interaction
required.

## Profiles and secrets

Profiles are TOML in the platform user-config directory. `--profiles PATH` or `HRNS_PROFILES` loads explicit files; CLI flags override connection fields. A profile never stores a plaintext token by default:

```toml
[profiles.team]
transport = "server"
url = "https://agents.example"
agent = "research-assistant"
token = "env:HRNS_TOKEN"

[profiles.local]
transport = "stdio"
command = "/opt/agents/bin/my-agent"
args = ["--acp"]
cwd = "/work/project"
# Environment names map to secret references; values are resolved at launch.
env = { OPENAI_API_KEY = "keyring:hrns/openai" }
```

Secret references are `env:NAME` and `keyring:service[/username]`. A missing secret fails before the agent starts, with the missing reference named. Stdio commands launch directly with an argv array and **no shell interpolation**. Stdout is protocol-only; client diagnostics remain off the wire.

*For automation only*, a literal token can be supplied with `--token`; it is visible to the process environment and shell history, so prefer a reference. Neither the terminal nor logs print resolved secrets.

### Attachments

`--attach PATH` (repeatable) embeds regular files in the prompt as ACP resources. Text files are sent as UTF-8 text resources; other files are base64 binary resources. Each file is capped at 8 MiB and the aggregate at the transport's 16 MiB frame budget. Directories, missing files and oversized files fail before connecting. The client refuses image, audio or embedded-resource blocks the agent did not advertise during `initialize`. Attachments never grant the agent access to the client filesystem; paths are display metadata, and hosted agents receive only the embedded bytes.

## Embedded network agents

Network agents are registered in an existing FastAPI app through `AgentServer`; see
[SERVER.md](SERVER.md). The client uses the same catalog and WebSocket endpoints.
Application authentication is optional; omit `--token` for a server without it.

```sh
hrns connect --url http://127.0.0.1:8080 --agent research
hrns connect --url http://127.0.0.1:8080 --agent research --token env:HRNS_TOKEN
```

## Python API

```python
import asyncio

from hrns.client import HrnsAcpClient, Profile, StdioAgentTransport


async def main():
    profile = Profile(name="local", transport="stdio", command="my-agent", args=["--acp"])
    client = HrnsAcpClient(
        lambda: StdioAgentTransport(command=profile.command, args=profile.args, cwd="."),
        # advertise_extension=True is default; third-party peers may not support it
    )
    events = []
    client.bus.subscribe(events.append)
    try:
        await client.connect()
        session = await client.new_session(cwd=".")
        await client.prompt(session.session_id, "Hello")
        await client.bus.drain()
        for event in events:
            print(event)
    finally:
        await client.disconnect()


asyncio.run(main())
```

`SessionProxy` wraps the common session operations:

```python
proxy = SessionProxy(client, session.session_id, cwd=".")
await proxy.prompt("Hello")
snapshot = await proxy.configuration()
await proxy.update_configuration(
    [{"type": "enable", "bindingId": "web_search", "enabled": False}],
    expected_revision=snapshot.selected_revision,
    request_id="client-edit-1",
)
await proxy.set_config_option("effort", "high")  # standard ACP option
await proxy.cancel()
await proxy.close()
```

Important behavior:

- `prompt()` returns after the ACP prompt response and drains updates that were already dispatched. Event handlers can run concurrently with the response, so UI code should update from events rather than assuming all notifications arrived before the response.
- `connect()` negotiates initialization and records the agent's advertised capabilities. `supports_load()` is false when the peer does not advertise `session/load`. If a profile sets `auth_method`, the client verifies that the agent advertised it and performs the standard `authenticate` request before opening a session; an unknown method fails before work starts.
- `load_session()` restores authoritative history from the agent. `resume_session()` is used only when advertised and the client has a verified complete cache; this terminal does not retain one, so its session switch always uses authoritative `session/load` when available and otherwise explains why it will not resume.
- Configuration updates are available only when the peer advertises `_hrns/configuration/update`; retries reuse the same `request_id`, and a changed payload under that ID is rejected. hrns hosts advertise this method for `next_turn` bindings; `new_session` changes fail with `NEW_SESSION_REQUIRED`.
- `WebSocketAgentTransport` adds bearer authentication, `hrns.acp.v1`, a 16 MiB frame limit and bounded framing. It refuses plaintext `ws://` to a non-loopback host so a bearer token cannot cross the network unencrypted; loopback development may use `ws://`. The catalog client handles `GET /v1/agents` and `GET /v1/agents/{agentId}`.

## Interactions in headless mode

A headless agent cannot invent approval. Without an explicit policy, a permission request or structured question produces `InteractionRequired`, retains the session, prints a machine-readable interaction summary, and exits `3`.

```sh
hrns run --allow-all "Perform the operation"
hrns run --deny-all "Perform the operation"
hrns run --server team --agent form-agent \
  --answer project=hrns --answer dry_run=true "Start"
```

`--allow-all` and `--deny-all` are mutually exclusive. `--answer` values accept JSON (`true`, `3`, `{"x":1}`) or plain text.

## Local renderers

`RendererRegistry` resolves versioned presentation identifiers against locally installed
`hrns.renderers` entry points named `<rendererId>@<schemaVersion>`. Each entry point is a
provider object with optional, independent `command_view`, `tool_view` and `approval_view`
factories. A remote peer supplies only an ID, version, JSON data and readable fallback — never
an import path or widget code. Unknown versions, missing factories and factory/apply
exceptions all return the sanitized fallback. Rendered fallback text strips terminal escapes,
control characters and bidi overrides and is bounded. Generic ACP activity rendering remains
available without any provider installed. See [EXTENSIONS.md](EXTENSIONS.md).

Plugin commands are discovered with `_hrns/commands/list` and invoked with
`_hrns/commands/invoke`; direct output renders as a distinct inline command block. The
client-side SDK exposes `SessionProxy.list_commands`, `invoke_command`, `cancel_command` and
`presentations`.

## Bridge and SSH

`hrns bridge` exposes a hosted agent through local newline-delimited ACP stdio, preserving JSON-RPC payloads:

```sh
hrns bridge --server team --agent research-assistant
hrns bridge --server team --agent research-assistant --remote-cwd /workspace
```

Launch a bridge-only ACP client against that command. Without `--remote-cwd`, session cwd fields pass through for server validation. With it, `session/new`, `load`, and `resume` cwd fields are replaced by the chosen remote directory. Returned paths remain remote; no local/remote filesystem synchronization is implied.

SSH needs no bespoke client command. Use a normal stdio profile:

```toml
[profiles.remote-box]
transport = "stdio"
command = "ssh"
args = ["build-box", "hrns-host", "my_agents.developer:definition"]
```

The existing SSH configuration and authentication apply. The remote environment must install the runtime and agent definition.

## Failure behavior

- A transport error becomes a recoverable connection state; it does not crash the terminal.
- On connection loss, an active interactive turn is cancelled and the session is retained. Reconnect, inspect, and load; an uncertain prompt is never automatically resent.
- A permission cancellation is an explicit rejection, not a hidden approval.
- Oversized prompts are rejected before sending. A server-closed `1009` frame limit is reported as an actionable size failure.
- Unknown extension methods and unsupported optional ACP features degrade to the standard interaction surface.
- `hrns run --json` emits one JSON object with `ok`, `sessionId`, `stopReason`, `text`, `tools`, `usage`, timing, and an error object when applicable.

## Local developer agent

`hrns connect --local` launches the configured local agent; `hrns run --local`
uses the same settings for headless execution. Use `--local-config PATH` to select
a TOML file, or press **l** in the connection picker to configure the provider and
model. Account logins (for example GitHub Copilot's device flow) are `hrns login
PROVIDER`, or **Ctrl+G** in the local model form. See [local setup](LOCAL.md) for
DeepSeek, local model servers, credentials, approvals, and configuration precedence.

## Session names and totals

Use `/rename <name>` in the terminal, or `await session.rename(name)` on a
`SessionProxy`, to set a persistent session name. `/rename` without an argument
opens a dialog. The host falls back to the first prompt text as a title; there is no
model-generated title tool. Manual names always take precedence, including after
reopening a session.

For hrns hosts, the status bar shows how much of the selected model's context window the
current turn has filled (hover for the last request's in/out/cached split and the session
totals), while the activity panel shows cumulative session usage, updated after every
completed model response, including intermediate responses before
tool execution. The context readout comes from ACP `usage_update` (a gauge, not a counter);
session totals come from `_meta.hrns.sessionUsage` and are never derived from it.
Turn completion reconciles and persists the counters without adding the
same tokens twice. Other ACP agents can publish usage updates or return usage with the
prompt response. The client does not reconstruct a historical cost ledger from the old runtime journal.
Prices come from Pydantic AI and are estimates, not invoices. Missing pricing
displays as unavailable. DeepSeek cache-hit tokens count as cache reads; cache
misses remain ordinary input, not cache writes.
