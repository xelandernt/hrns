# Architecture

```text
Python application ──────────────────────────────┐
                                                ▼
Textual / headless client ── ACP stdio ──► harness-host ──► SessionManager
         │                                                  │
         └── harness.acp.v1 WebSocket ──► existing FastAPI    │
                                          + AgentServer ────┘
                                                            ▼
                                                  Pydantic AI Agent
                                                  + native capabilities
                                                  + bound workspace
```

## One distribution, five modules

| Module | Owns | Deliberately independent of |
| --- | --- | --- |
| `harness.protocol` | Typed extension DTOs, errors, framing and pagination | AI runtime, terminal, network hosting |
| `harness.config` | Shared local settings using pydantic-settings | AI runtime, terminal, network hosting |
| `harness.runtime` | Definitions, plugins, policy, execution, persistence, ACP worker | Terminal and server |
| `harness.client` | ACP connections, profiles, terminal, headless runner, stdio bridge | Native agent code and provider SDKs |
| `harness.server` | FastAPI registration, optional authentication, discovery, in-process ACP | Terminal |

Installing the `client` extra does not install Pydantic AI. The `server` extra includes the runtime: native agent definitions execute in the existing FastAPI process. Each connection has its own ACP host and session manager; the server owns one explicit shared SQLite or PostgreSQL store. Application authentication is an optional FastAPI dependency.

## Native behavior stays native

Pydantic AI owns inference, tools, capability hooks, structured outputs, and model history. Harness adds the surrounding session controller. Capabilities are not converted to tool-only plugins. Managed bindings have explicit identity and policy; disabled bindings are omitted from the next composition. Factory-private capabilities remain fixed behavior.

The upstream experimental ACP session factory runs once per session, which cannot implement a composition changing between turns. A project-owned adapter therefore uses the public ACP SDK and the same runtime controller as Python callers. Compatible upstream presentation helpers can be used without accessing upstream private adapter state.

## Configuration is a transaction

1. The client sends explicit changes with an expected selected revision and stable request ID.
2. The controller validates the complete candidate selection against schemas, toggle permissions, lifecycle, dependencies, conflicts, and resource requirements.
3. The store commits the selection and retry result before acknowledgment.
4. A turn captures an immutable active selection. Changes during execution affect the next turn.
5. Failed preparation blocks the new turn; it does not silently execute with the old selection.

An idempotent state selection does not require permission for a transition that never occurs. A disabled dependency is not automatically re-enabled. Request IDs deduplicate configuration transactions; ordinary prompts are never automatically replayed.

## State ownership

Canonical history and configuration live on the runtime host. Session access is scoped by principal and the registered agent ID/definition version. Connections release control after active work is drained. SQLite WAL or PostgreSQL transactions coordinate session ownership. ACP working directory and selected model/effort are persisted alongside conversation state for network connections. A saved transcript is distinct from model history and from live capability instances; live objects are never pickled.

Attachments can use `harness.runtime.blobs.FileBlobStore`: bounded, content-addressed files outside ephemeral workspaces, with owner/session namespaces and integrity checks. A digest alone does not grant another session access.

## Execution locations

The workspace is a named host resource, not a directory inferred from the client machine. Network ACP hosts validate `cwd` against the roots passed during Python registration. A requested sandbox that cannot be constructed fails explicitly. Local workspace execution is non-isolated; isolated command execution requires an explicitly installed and bound upstream sandbox backend. Plugins and provider clients run in the application process for network agents, or the launched process for stdio agents.

## Protocol versions

ACP v1 is the baseline. `_harness/*` methods are version-one project extensions negotiated through `_meta.harness`. Standard-only clients receive standard content and supported configuration options. The `harness.acp.v1` WebSocket subprotocol is project-defined, carries one JSON-RPC object per text message, and has a 16 MiB message limit. The bridge preserves ACP messages while translating transport framing.

See [contracts](CONTRACTS.md), [server operation](SERVER.md), [ACP behavior](ACP.md), and [catalog coverage](CATALOG.md).
