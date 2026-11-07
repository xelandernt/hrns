# ACP support

`hrns.app.acp` is this repository's own [Agent Client Protocol](https://agentclientprotocol.com)
adapter. It targets ACP 1.x directly (`acp` SDK, protocol version 1) and is used by both
transports: `run_acp_stdio` and the FastAPI mount.

This page is the inventory: which ACP methods the adapter answers, which it refuses, and
which it never advertises. For mounting it on a web server, see [mount_acp](mount-acp.md).

## Status legend

| Status          | Meaning                                                                                                       |
|-----------------|---------------------------------------------------------------------------------------------------------------|
| **Yes**         | Implemented, no extra wiring needed                                                                           |
| **Conditional** | Implemented, but only when the adapter was constructed with the required object; otherwise `method_not_found` |
| **No**          | Always `method_not_found`, and never advertised in `initialize`                                               |
| **Ignored**     | The notification is accepted and has no effect                                                                |

## Transport

| Transport | Entry point                                                   | Framing                                                 |
|-----------|---------------------------------------------------------------|---------------------------------------------------------|
| stdio     | `run_acp_stdio(agent, session_store=..., model_resolver=...)` | Newline-delimited JSON on stdin/stdout                  |
| WebSocket | `mount_acp(app, agent_factory, ...)`                          | One text frame per JSON message, bridged to the adapter |

Both run the adapter with `use_unstable_protocol=True`, so unstable methods are routed to
it rather than filtered by the SDK.

## What `initialize` advertises

The adapter advertises only what it can actually do, so a well-behaved client will not
call a refused method in the first place.

| Field | Advertised when | Notes |
| --- | --- | --- |
| `loadSession` | a `session_store` is configured | `create_acp_agent` always supplies one |
| `sessionCapabilities.close` | always | `session/close` works |
| `sessionCapabilities.list` | the store implements `ListableSessionStore` | `InMemorySessionStore` does |
| `sessionCapabilities.delete` | the store also implements `DeletableSessionStore` | `InMemorySessionStore` does |
| `providers` | a `model_resolver` was passed | Enables the `model` config option and `providers/*` |
| `mcpCapabilities` | `mcp_capabilities` was passed | `hrns-local` advertises `acp`, `http`, and `sse` |
| `promptCapabilities` | never | No image, audio, or embedded-context prompts |
| `nesCapabilities` | never | No next-edit suggestions |
| `auth` | never | No authentication methods |

## Agent methods

Every method in `acp.AGENT_METHODS`, in wire order.

| Method | Kind | Status | Condition / behavior |
| --- | --- | --- | --- |
| `initialize` | request | **Yes** | Negotiates version 1 and returns the advertisement above |
| `authenticate` | request | **Yes** | No-op returning `null`; no auth methods are advertised, so a client has no reason to call it |
| `providers/list` | request | **Conditional** | Requires `model_resolver` |
| `providers/set` | request | **Conditional** | Requires `model_resolver`; a `ValueError` from the resolver becomes `invalid_params` |
| `providers/disable` | request | **Conditional** | Same; a `required` provider cannot be disabled |
| `session/new` | request | **Yes** | First advertised model is the session default; returns the `model` and setting config options when a resolver is present |
| `session/load` | request | **Conditional** | Requires a `session_store`; replays the stored transcript as `session/update` before responding |
| `session/set_mode` | request | **No** | Modes are not implemented |
| `session/set_config_option` | request | **Conditional** | `model`, plus any setting option the current model advertises; the value must be one of the advertised values |
| `session/prompt` | request | **Yes** | Runs one turn and streams `session/update`; `stop_reason` is the finish reason, or `cancelled` |
| `session/cancel` | notification | **Yes** | Cancels the in-flight turn; the prompt returns `cancelled` |
| `mcp/message` | request | **Conditional** | The SDK route is replaced by the stateless binding when a `session_config` is installed; it serves agent-as-provider/proxy requests. Client-provided servers are consumed through `McpOverAcpCaller`. |
| `session/list` | request | **Conditional** | Requires a `ListableSessionStore`; supports `cwd` filter and cursor paging |
| `session/delete` | request | **Conditional** | Requires a `DeletableSessionStore`; closes live state first |
| `session/fork` | request | **No** | Not implemented |
| `session/resume` | request | **No** | Not implemented; use `session/load` |
| `session/close` | request | **Yes** | Cancels any active turn and discards session state |
| `logout` | request | **Yes** | No-op; there is no login to end |
| `nes/start` | request | **No** | No next-edit-suggestion service |
| `nes/suggest` | request | **No** | Same |
| `nes/accept` | notification | **Ignored** | Accepted and dropped |
| `nes/reject` | notification | **Ignored** | Accepted and dropped |
| `nes/close` | request | **No** | Same |
| `document/didOpen` | notification | **Yes** | Records the document on the session |
| `document/didChange` | notification | **Yes** | Applies the content changes to the tracked text |
| `document/didClose` | notification | **Yes** | Forgets the document |
| `document/didSave` | notification | **Yes** | Marks the tracked document saved |
| `document/didFocus` | notification | **Yes** | Records cursor and viewport |

Document tracking exists so a future NES implementation has a correct view of the client's
open files. Nothing else consumes it today, and unknown sessions are silently ignored.

`mcp/message` is routed by `src/hrns/acp_mcp/router.py`, which replaces the Python SDK's
stateful route when a `session_config` is installed. The adapter's `mcp_message` fallback
still answers `method_not_found` on an unbound connection.

## Client methods the agent calls

The adapter is a client of the editor/terminal on the other side. These are the client
methods it may send, and when.

| Method | Sent when |
| --- | --- |
| `session/update` | Always, once per streamed event: agent text, thought text, tool call start/update/completion, plan updates (from `Todo`), and a `UsageUpdate` gauge with context tokens and session cost |
| `session/request_permission` | A tool call that is `unapproved` (see [approval](capabilities.md#approval)) needs a decision; one request per batch for `apply_changes` |
| `fs/read_text_file` | Only when the agent uses `AcpFileSystemToolset` / `acp_filesystem`, and the client advertises `fs.readTextFile` |
| `fs/write_text_file` | Same, when the client also advertises `fs.writeTextFile`; otherwise writes fall back to a local `FileSystem` bounded by the session `cwd` |
| `terminal/create`, `terminal/wait_for_exit`, `terminal/output`, `terminal/kill`, `terminal/release` | Only when the agent uses `AcpTerminalToolset` / `acp_terminal`, and the client advertises terminal support |
| `mcp/message` | When the adapter uses a client-provided (`type: "acp"`) server; the caller sends requests and receives request-scoped notifications. `mcp/connect` and `mcp/disconnect` are never sent: the binding is stateless. |
| `elicitation/create` | When the model calls `ask_user_question` and the client advertised `elicitation.form`; one form per call, session-scoped to the tool call |
| `elicitation/complete` | Never; `ask_user_question` elicitation is answered synchronously |

The ACP-backed filesystem and terminal are opt-in through `session_config` on
`PydanticAIACPAgent`; the default `build_agent` uses the harness `FileSystem` and `Shell`
on the server side, so a plain mount never asks the client to touch files or run commands.

`session_config` is also where MCP declarations become toolsets. `hrns-local` passes
`acp_mcp_toolsets`, which turns `stdio`, `http`, and `sse` entries into ordinary
`MCPToolset`s and turns `type: "acp"` entries into `AcpMcpClientTransport`s backed by the
session's caller. The adapter advertises `mcpCapabilities` only when a `session_config` is
present.

## Session config: the `model` and setting options

ACP has no model methods. Models are a session config option, and so are model settings
such as reasoning effort; the adapter advertises both only when a
[`ModelResolver`](mount-acp.md#the-resolver-object) is supplied:

- `session/new` and `session/load` return `configOptions` containing a `model` select,
  with each choice tagged `_meta.providerId`, followed by every
  [`SettingOption`](mount-acp.md#session-settings) `list_options(model_id)` returns. The
  effort option carries `category: "thought_level"`. The bundled terminal client's
  `/effort` uses that option for any connected agent, local or remote, and only writes the
  `[model_settings]` file when the agent is `hrns-local`.
- `session/set_config_option` accepts `model` and any advertised setting id; a value
  outside the advertised choices is `invalid_params`. Selections are persisted with the
  session and revalidated on `session/load`, so a value that a model switch or provider
  reroute removed is dropped instead of replayed.
- The model is resolved per turn, so a routing change from `providers/set` applies from
  the next turn; a turn already running keeps the endpoint it started with.
- Setting selections are applied per **model request**, not per turn: `resolve_settings`
  runs before each request in the agent's tool loop, so a change made during a turn lands
  on the next request of that same turn. A request already in flight keeps its settings.
- `providers/set` and `providers/disable` can change which settings a model offers. When
  they do, resident sessions receive a `config_option_update` with the complete new state.


## Errors

| Code | Raised for |
| --- | --- |
| `method_not_found` | A refused method, or a conditional method without the object it needs |
| `invalid_params` | Unknown session or config id, unadvertised model, bad cursor, a disabled provider at resolve time, or a provider configuration the resolver rejects |
| `internal_error` | A stored session that cannot be read |

A resolver's `ProviderDisabled` becomes `invalid_params` while resolving, and a
`ValueError` from `providers/set` or `providers/disable` does too. Any other exception
escapes as an internal error, so raise those two for anything a client can fix.

## Known gaps

- MCP-over-ACP supports tools, resources, prompts, completions, progress, and
  input-required results. Long-lived `subscriptions/listen` responses are not streamed
  yet, and MRTR is passed through without an explicit retry policy.
- Upstream MCP servers may speak a handshake-era revision. That path is best-effort: the
  proxy mirrors the upstream's advertised capabilities and logs the negotiated revision,
  and the server's own reverse requests (sampling, elicitation, roots) fail with
  `INVALID_REQUEST` instead of being bridged.
- Next-edit suggestions, session fork/resume, and session modes are not implemented.
  None of them are advertised.
- Form elicitation is used only for `ask_user_question`. URL elicitation is never sent, and
  a client without form support gets a plain "proceed without an answer" result.
- One connection per adapter. `session/list` and `session/delete` work for stores that
  support them, but there is no multi-client fan-out above the adapter.
- Provider configuration is process-scoped and in memory. It is never persisted with a
  session and never echoed back with secrets.

The implementation notes and remaining MCP-over-ACP work are in
[`mcp-over-acp-analysis.md`](mcp-over-acp-analysis.md).
