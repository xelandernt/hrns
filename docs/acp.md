# ACP support

`hrns.server.acp` is this repository's own [Agent Client Protocol](https://agentclientprotocol.com)
adapter. It targets ACP 1.x (protocol version 1) and serves both
transports: `run_acp_stdio` and the FastAPI mount.

This page lists method behavior, advertised capabilities, and client interactions.
For hosting, see [agent servers](mount-acp.md).

## Status legend

| Status          | Meaning                                                                                                       |
|-----------------|---------------------------------------------------------------------------------------------------------------|
| **Yes**         | Available on every adapter                                                                           |
| **Conditional** | Available with the required configuration; other calls receive `method_not_found` |
| **No**          | Returns `method_not_found`                                               |
| **Ignored**     | The notification is accepted and discarded                                                                |

## Transport

| Transport | Entry point                                                   | Framing                                                 |
|-----------|---------------------------------------------------------------|---------------------------------------------------------|
| stdio     | `run_acp_stdio(factory, session_store=..., model_resolver=...)` | Newline-delimited JSON on stdin/stdout                  |
| WebSocket | `server.mount_acp(app, prefix="/acp")`                          | One text frame per JSON message, bridged to the adapter |

Both transports enable unstable protocol methods with `use_unstable_protocol=True`.

## What `initialize` advertises

`initialize` reports capabilities according to the adapter configuration:

| Field | Advertised when | Notes |
| --- | --- | --- |
| `loadSession` | a `session_store` is configured | every registration supplies one |
| `sessionCapabilities.close` | always | `session/close` works |
| `sessionCapabilities.list` | the store implements `ListableSessionStore` | `InMemorySessionStore` does |
| `sessionCapabilities.delete` | the store also implements `DeletableSessionStore` | `InMemorySessionStore` does |
| `providers` | a `model_resolver` was passed | Enables the `model` config option and `providers/*` |
| `mcpCapabilities` | `mcp_capabilities` was passed | `hrns agent` advertises `acp`, `http`, and `sse` |

## Agent methods

Every method in `acp.AGENT_METHODS`, in wire order.

| Method | Kind | Status | Condition / behavior |
| --- | --- | --- | --- |
| `initialize` | request | **Yes** | Negotiates version 1 and returns the advertisement above |
| `authenticate` | request | **Yes** | Returns `null`; admission is configured on the transport mount |
| `providers/list` | request | **Conditional** | Requires `model_resolver` |
| `providers/set` | request | **Conditional** | Requires `model_resolver`; a `ValueError` from the resolver becomes `invalid_params` |
| `providers/disable` | request | **Conditional** | Same; a `required` provider cannot be disabled |
| `session/new` | request | **Yes** | First advertised model is the session default; returns the `model` and setting config options when a resolver is present |
| `session/load` | request | **Conditional** | Requires a `session_store`; attaches to live execution or restores saved state, then replays before responding |
| `session/set_mode` | request | **No** | Returns `method_not_found` |
| `session/set_config_option` | request | **Conditional** | `model`, plus any setting option the current model advertises; the value must be one of the advertised values |
| `session/prompt` | request | **Yes** | Queues one turn in the shared FIFO and streams `session/update`; `stop_reason` is the finish reason, or `cancelled` |
| `session/cancel` | notification | **Yes** | Cancels the in-flight turn and pauses pending work; the prompt returns `cancelled` |
| `mcp/message` | request | **Conditional** | The stateless binding routes registered provider requests; client-provided servers are consumed through `McpOverAcpCaller` |
| `session/list` | request | **Conditional** | Requires a `ListableSessionStore`; supports `cwd` filter and cursor paging |
| `session/delete` | request | **Conditional** | Requires a `DeletableSessionStore`; closes live state first |
| `session/fork` | request | **No** | Returns `method_not_found` |
| `session/resume` | request | **No** | Restore saved sessions with `session/load` |
| `session/close` | request | **Yes** | Cancels any active turn and discards session state |
| `logout` | request | **Yes** | Returns `null` |
| `nes/start` | request | **No** | Returns `method_not_found` |
| `nes/suggest` | request | **No** | Same |
| `nes/accept` | notification | **Ignored** | Accepted and dropped |
| `nes/reject` | notification | **Ignored** | Accepted and dropped |
| `nes/close` | request | **No** | Same |
| `document/didOpen` | notification | **Yes** | Records the document on the session |
| `document/didChange` | notification | **Yes** | Applies the content changes to the tracked text |
| `document/didClose` | notification | **Yes** | Forgets the document |
| `document/didSave` | notification | **Yes** | Marks the tracked document saved |
| `document/didFocus` | notification | **Yes** | Records cursor and viewport |

Document notifications maintain a session-local view of open files, cursor, and viewport.
Detach clears this document state.

`mcp/message` is routed by `hrns_slim/hrns/shared/acp_mcp/router.py`. The adapter's `mcp_message` fallback
still answers `method_not_found` on an unbound connection.

## Client methods the agent calls

The adapter calls these methods on the connected client:

| Method | Sent when |
| --- | --- |
| `session/update` | Always, once per streamed event: agent text, thought text, tool call start/update/completion, plan updates (from `Todo`), and a `UsageUpdate` gauge with context tokens and session cost |
| `session/request_permission` | A tool call that is `unapproved` (see [approval](capabilities.md#approval)) needs a decision; one request per batch for `apply_changes` |
| `fs/read_text_file` | Only when the agent uses `AcpFileSystemToolset` / `acp_filesystem`, and the client advertises `fs.readTextFile` |
| `fs/write_text_file` | Same, when the client also advertises `fs.writeTextFile`; otherwise writes fall back to a local `FileSystem` bounded by the session `cwd` |
| `terminal/create`, `terminal/wait_for_exit`, `terminal/output`, `terminal/kill`, `terminal/release` | Only when the agent uses `AcpTerminalToolset` / `acp_terminal`, and the client advertises terminal support |
| `mcp/message` | When the adapter uses a client-provided (`type: "acp"`) server; the caller sends requests and receives request-scoped notifications |
| `elicitation/create` | When the model calls `ask_user_question` and the client advertised `elicitation.form`; one form per call, session-scoped to the tool call |

The ACP-backed filesystem and terminal are opt-in through `attachment_config` on
`server.add_agent`; the default `build_agent` runs file and shell tools on the server.

`attachment_config` is where MCP declarations become toolsets. `hrns agent` passes
`acp_mcp_toolsets`, which turns `stdio`, `http`, and `sse` entries into ordinary
`MCPToolset`s and turns `type: "acp"` entries into `AcpMcpClientTransport`s backed by the
session's caller. The adapter advertises `mcpCapabilities` only when an `attachment_config` is
present.

In the terminal, `/mcp` opens the MCP side panel: it shows each configured server's
transport, export state, target, negotiated protocol, and failure reason. Startup and
authentication failures also appear as transcript notices and a status hint.

## Session config: the `model` and setting options

Models and settings such as reasoning effort are session config options.
The adapter advertises them when a
[`ModelResolver`](mount-acp.md#the-resolver-object) is supplied:

- `session/new` and `session/load` return `configOptions` containing a `model` select,
  with each choice tagged `_meta.providerId`, followed by every
  [`SettingOption`](mount-acp.md#session-settings) `list_options(model_id)` returns. The
  effort option carries `category: "thought_level"`. The bundled terminal client's
  `/effort` uses that option for any connected agent, local or remote, and only writes the
  `[model_settings]` file when the agent is `hrns agent`.
- `session/set_config_option` accepts `model` and any advertised setting id; a value
  outside the advertised choices is `invalid_params`. Selections are persisted with the
  session and revalidated on `session/load`, so a value that a model switch or provider
  reroute removed is dropped.
- The model is resolved per turn, so a routing change from `providers/set` applies from
  the next turn; a turn already running keeps the endpoint it started with.
- Setting selections are applied before each **model request**: `resolve_settings`
  runs before each request in the agent's tool loop, so a change made during a turn lands
  on the next request of that same turn. A request already in flight keeps its settings.
- `providers/set` and `providers/disable` can change which settings a model offers. When
  they do, resident sessions receive a `config_option_update` with the complete new state.

## Skills in `_meta`

When the agent has a
[`Skills`](skills.md) capability, `session/new` and `session/load` return its catalog in
the response `_meta` under `hrns/skills`, and `session/prompt` accepts a reference in a
text block's `_meta` under the same key. Clients use these payloads to offer `$name`
completion. The full payload shapes are on the [Skills](skills.md) page.


## Errors

| Code | Raised for |
| --- | --- |
| `method_not_found` | A refused method, or a conditional method without the object it needs |
| `invalid_params` | Unknown session or config id, unadvertised model, bad cursor, a disabled provider at resolve time, or a provider configuration the resolver rejects |
| `internal_error` | A stored session that cannot be read |

A resolver's `ProviderDisabled` becomes `invalid_params` while resolving, and a
`ValueError` from `providers/set` or `providers/disable` does too. Any other exception
escapes as an internal error, so raise those two for anything a client can fix.

## Runtime scope

- MCP-over-ACP serves tools, resources, prompts, completions, progress, and input-required
  results through single exchanges. See [MCP over ACP](mcp.md) for polling, retries, and
  upstream transport behavior.
- Questions use form elicitation. A client lacking form support receives a plain
  proceed-with-an-assumption result.
- One ACP client attaches to each live session. Session list/delete depend on the store's
  interfaces.
- Provider configuration lives in memory at registration scope. Public provider payloads
  contain routing endpoints; credentials remain in request settings.

## Shared live sessions

Execution belongs to the server, so disconnecting ACP leaves accepted work alive.
Only one ACP client may attach to a session. Programmatic turns and ACP prompts use
one FIFO; cancellation/errors pause it, with inspection/resume/clear available only
in Python. Approvals and questions wait for a client and are reissued after reconnection.
The `_hrns/session/state` notification and `hrns/session/state` load metadata report
revisioned server-started turn state. [AG-UI](ag_ui.md) provides independent read-only
observation.
