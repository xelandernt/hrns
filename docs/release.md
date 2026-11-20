# Client release artifacts

One release publishes the Python packages, the native client wheels and the browser
bundle at the same version.

## Artifacts

| Artifact | Contents |
| --- | --- |
| `hrns` | Metadata wrapper depending on `hrns-slim[client,server]` at the same version. |
| `hrns-slim` | Python client, headless runner, server and shared configuration. |
| `hrns-tui` | Native Ratatui executable for the matching platform. |
| `hrns-web.zip` | Optimized Ratzilla bundle built from `rust/`. |

The publish workflow rewrites the `hrns-tui` crate version to the release tag before
Maturin builds, so every wheel reports the same version as the Python packages.

## Install

```sh
pip install hrns==0.1.0
```

That installs the wrapper and the matching native client. For a server host without the
terminal:

```sh
pip install 'hrns-slim[server]==0.1.0'
```

For Python tooling without the agent:

```sh
pip install 'hrns-slim[client]==0.1.0'
```

The native executable is optional. Without it, `hrns` and `hrns connect` fall back to
the Textual client. `HRNS_TEXTUAL=1` forces Textual; `HRNS_RUST_TUI_PREVIEW=1` forces
the native client and reports a missing wheel.

The launcher resolves the target, credential references, `client_io` (CLI, then
profile, then `[tui]`), theme and permission mode, then replaces the Python process.
The native process inherits the terminal and restores it on exit.

## Serve the browser bundle

Unpack `hrns-web.zip` and serve its `dist/` directory from the same HTTPS origin as the
authenticated ACP mount. The app connects to `/acp/agent` by default.

| Query parameter | Meaning |
| --- | --- |
| `endpoint` | Absolute `wss://` ACP URL; `ws://` is accepted for loopback development. Credentials are not allowed in the URL. |
| `cwd` | Absolute remote workspace path. Optional when `session` is set and the runtime supports listing. |
| `session` | Saved session ID to load. |
| `theme` | `hrns`, `textual-dark`, `textual-light`, `ansi-dark` or `ansi-light`. |
| `permission` | `ask`, `reads` or `all`. |

The browser advertises form elicitation only. It sends no client filesystem, terminal
or MCP capability, and it never launches a local agent. Deployments that need those
capabilities use the native client or the headless runner.

Authentication, origin validation and principal mapping belong to the deployment.
See [Authentication](authentication.md#serve-the-ratzilla-client-with-acp-authentication).

## Verify a release

```sh
uvx twine check dist/*
python -m venv /tmp/hrns-check
/tmp/hrns-check/bin/pip install dist/hrns-*.whl dist/hrns_tui-*.whl
HRNS_TEXTUAL=1 /tmp/hrns-check/bin/hrns --version
/tmp/hrns-check/bin/hrns-tui --version
```

Install in an empty environment, launch the interactive client against the built-in
agent, run one headless prompt, and serve the unpacked bundle against the deployed ACP
runtime. Check that the endpoint, workspace and principal shown in the client match
the deployment configuration, and that an expired or missing credential leaves the
draft visible and the disconnected state shown.
