# Client release artifacts

One GitHub release builds the Python packages, native client wheels, standalone
browser bundle and npm client at the release version. Use a version such as
`0.1.0` for a stable release or `0.2.0-rc.1` for a prerelease. Cargo and npm
use the SemVer form. Python uses the matching PEP 440 form, such as
`0.2.0rc1`. The workflow applies the SemVer version to both Cargo files before
any locked Rust release build.

## Artifacts

| Artifact | Contents |
| --- | --- |
| `hrns` | PyPI wrapper for `hrns-slim[client,headless,server]`. |
| `hrns-slim` | Python client commands, headless runner, server and shared configuration. |
| `hrns-tui` | Native Ratatui executable for the matching platform, distributed as a PyPI wheel. |
| `@xelandernt/hrns-tui` | Mountable Ratzilla terminal for browser applications, distributed through npm. |
| `hrns-web.zip` | Standalone Ratzilla site attached to the GitHub release. |

Configure PyPI trusted publishing for `hrns`, `hrns-slim` and `hrns-tui` against
the `pypi` GitHub environment and the `publish.yml` workflow. Configure the npm
trusted publisher for the same workflow in the `npm` environment, with direct
publishing enabled. If the npm package needs to be created first, publish
a bootstrap version from an authorized npm account, then configure its trusted
publisher before the next GitHub release. GitHub prereleases publish to the npm
`next` tag. Stable releases publish to `latest`. See [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/).

## Version-matched native dependency

The release inserts `hrns-tui=={{ version }}` into the `hrns-slim[client]` extra
after validating the development workspace. The Hatch metadata hook expands
the pin to the same release version as the Maturin-built native wheel.

| Install command | What it installs |
| --- | --- |
| `pip install 'hrns-slim[client]>0.0.28'` | The native terminal and shared CLI. |
| `pip install 'hrns-slim[headless]>0.0.28'` | The Python runner for `hrns run`. |
| `pip install 'hrns-slim[server]'` | The Python server. |
| `pip install 'hrns-slim[client,server]>0.0.28'` | The native terminal and Python server. |
| `pip install 'hrns>0.0.28'` | The native terminal, Python runner, and Python server. |

The version floor excludes historical releases that shipped the Python terminal.
Native client installs require a compatible wheel. CI builds wheels for glibc Linux x64,
macOS arm64 and x64, and Windows x64. On other platforms, pip fails to resolve
the client requirement. The server extra remains available wherever its Python
dependencies can be installed.

Reproduce the published metadata locally:

```sh
python scripts/release_native_dependency.py
UV_DYNAMIC_VERSIONING_BYPASS=0.1.0 just build
python scripts/release_native_dependency.py --verify dist
python scripts/release_native_dependency.py --revert
```

Use `--revert` before running `uv run` or `uv sync` to restore the development
metadata used by the lockfile. `just install` builds the native binary from the
checkout through the workspace's development group.

## Embed the browser terminal

Install `@xelandernt/hrns-tui` with npm. See the [React example](https://github.com/xelandernt/hrns/tree/main/rust/npm#react)
for the mount API, cleanup, and Vite configuration. Embedded terminals use the
same ACP endpoint and deployment authentication as the standalone bundle.

To build a package locally, install Node.js, the Rust `wasm32-unknown-unknown`
target, and the `wasm-bindgen` CLI version in `rust/Cargo.lock`.

```sh
cd rust/npm
npm ci
npm run build
npm pack
```

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
python scripts/release_native_dependency.py --verify dist
python -m venv /tmp/hrns-check
/tmp/hrns-check/bin/pip install --find-links dist 'hrns>0.0.28'
/tmp/hrns-check/bin/hrns-tui --version
/tmp/hrns-check/bin/hrns --version
```

`--verify` checks that both Python artifact types carry the expected dependency
metadata. The slim client pins the native wheel to its own version and the wrapper
requests all three extras. The installation command resolves the native wheel from
`dist/` when it contains the matching artifact.

Install in an empty environment, launch the interactive client against the built-in
agent, run one headless prompt, and serve the unpacked bundle against the deployed ACP
runtime. Check that the endpoint, workspace and principal shown in the client match
the deployment configuration, and that an expired or missing credential leaves the
draft visible and the disconnected state shown.
