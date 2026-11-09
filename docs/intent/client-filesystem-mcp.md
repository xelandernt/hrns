# Intent: client-side filesystem MCP server

Status: confirmed. Date: 2026-11-09.

## Statement of intent

- **Outcome:** A built-in, in-process MCP server shipped with the `hrns` client exposes
  Coder-style file tools and a persistent shell to a remote ACP agent over
  MCP-over-ACP, so the agent can read, search, edit, and run commands in the client's
  local project.
- **User:** You, running the `hrns` TUI or `hrns run` against a remote ACP server,
  wanting its work to land on your local checkout.
- **Why now:** ACP v2 removes `fs/*` and `terminal/*` and names client-provided MCP
  servers as the replacement. `client_io` only serves v1 agents that wire the ACP
  filesystem toolset, and `hrns agent` does not; this server is the forward-compatible
  path.
- **Success:** With `[client.filesystem] enabled = true`, any agent advertising
  `mcpCapabilities.acp` sees `client_read_file`, `client_write_file`, `client_edit_file`,
  `client_list_files`, `client_grep`, and `client_shell`. Reads run automatically;
  writes, edits, and shell ask the client first (allow once / session / always / deny).
  Nothing is exposed when disabled. The `client_` prefix avoids collisions with Coder,
  and `/mcp` shows the server as `builtin`.
- **Constraint:** Root is the client's **local** working directory — `target.cwd` for a
  local target, the launch directory for a remote target, never `remote_cwd`. `client_io`
  is fixed to use the same local root, so remote agents using ACP `fs/*` with remote
  absolute paths are rejected rather than resolved locally. In-process only, no child
  process. `[client.filesystem]` is client-owned and global, default off. Headless:
  reads auto, writes/shell only with `--allow-all` (or `approval = "allow"`), `--deny-all`
  disables the server. `Coder()` stays in `build_agent`; `require_approval` is no longer
  installed by default and remains opt-in for custom agents. Shell is Coder-parity
  persistent: foreground/background handles, job state under the root, processes killed
  when the connection closes.
- **Out of scope:** Migrating `hrns` itself to ACP v2; removing `client_io`; changing
  local agent composition; `apply_changes`; whole-filesystem or multi-root access;
  interactive PTY; exposing the builtin to non-ACP MCP clients; per-profile overrides.

## Decisions recorded during the interview

| Topic | Decision |
| --- | --- |
| Tool surface | File tools plus a Coder-parity persistent shell |
| Tool names | Fixed `client_` prefix |
| Root | Client's local working directory; never the remote workspace |
| Enablement | Explicit opt-in, default off, in a client-owned section |
| Config shape | `[client.filesystem]` typed section, not a `[mcp.servers]` entry |
| Packaging | In-process low-level MCP server registered with `McpOverAcpProvider` |
| Approval | Client-side gate; reads auto, writes/edits/shell ask |
| Headless | Reads auto; writes/shell need `--allow-all` or `approval = "allow"` |
| Server approval | `require_approval` removed from the default `build_agent`; opt-in |
| Local agent | `Coder()` stays server-side; no composition change |
| `client_io` | Kept as the v1 compatibility path; root fixed to the local cwd |

## Relevant existing code

- `hrns_slim/hrns/client/io.py` — ACP v1 `fs/*` and `terminal/*` handlers.
- `hrns_slim/hrns/client/mcp_provider.py` — MCP-over-ACP provider and consent.
- `hrns_slim/hrns/shared/acp_mcp/` — stateless MCP-over-ACP binding.
- `hrns_slim/hrns/server/acp/toolsets.py` — `acp_filesystem` / `acp_terminal`,
  not installed by `hrns agent`.
- `hrns_slim/hrns/server/approvals.py` — `require_approval`, currently installed by
  default in `build_agent`.
- `hrns_slim/hrns/shared/config/settings.py` — `ClientIoMode`, `Profile`, `TuiSettings`.
- `docs/mcp.md`, `docs/acp.md`, `docs/configuration.md` — user-facing documentation.
