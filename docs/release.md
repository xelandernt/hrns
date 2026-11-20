# Client release artifacts

One release publishes the Python packages, the native client wheels and the browser
bundle at the same version.

## Artifacts

| Artifact | Contents |
| --- | --- |
| `hrns` | Metadata wrapper depending on `hrns-slim[client,server]` and, on the four built platforms, `hrns-tui` at the same version. |
| `hrns-slim` | Python client, headless runner, server and shared configuration. |
| `hrns-tui` | Native Ratatui executable for the matching platform. |
| `hrns-web.zip` | Optimized Ratzilla bundle built from `rust/`. |

The publish workflow rewrites the `hrns-tui` crate version to the release tag before
Maturin builds, so every wheel reports the same version as the Python packages. Register
trusted publishing for both `hrns` and `hrns-tui` against the same `pypi` environment, or
move the native wheels to their own publish job when the projects differ.

## Version-matched native dependency

`pip install hrns` installs the native client at exactly the version of the wheel, while
`hrns-slim[server]` and `hrns-slim[client]` stay free of it. The workspace cannot declare
that requirement: `hrns-tui` is not on the index during development and its version comes
from the Rust crate rather than from `uv-dynamic-versioning`, so `uv lock` and `uv sync`
would fail. The publish workflow therefore patches the wrapper metadata after the tree
has been validated and rebuilds `dist/`:

| Step | Command |
| --- | --- |
| Validate the pristine tree | `just install`, `just check` |
| Add the requirement | `python scripts/release_native_dependency.py` |
| Rebuild `dist/` | `just build` |
| Assert the metadata | `python scripts/release_native_dependency.py --verify dist` |

The requirement is `hrns-tui=={{ version }}`, expanded by the hatch metadata hook to the
wheel's own version, with markers for the four targets CI builds:

| Platform | Marker |
| --- | --- |
| Linux x64 | `sys_platform == "linux" and platform_machine == "x86_64"` |
| macOS arm64 | `sys_platform == "darwin" and platform_machine == "arm64"` |
| macOS x64 | `sys_platform == "darwin" and platform_machine == "x86_64"` |
| Windows x64 | `sys_platform == "win32" and (platform_machine == "AMD64" or platform_machine == "x86_64")` |

`hrns-slim` metadata is untouched, so only the wrapper selects the binary. Unsupported
platforms (Linux arm64, Windows arm64, 32-bit Python, FreeBSD) evaluate every marker to
false and install the Python packages alone. A PEP 508 marker cannot see the C library,
so musl Linux matches the Linux x64 marker without a matching wheel; release musllinux
wheels before advertising Alpine, or install with
`pip install --no-deps hrns 'hrns-slim[client,server]'`.

Reproduce the published metadata locally:

```sh
python scripts/release_native_dependency.py
UV_DYNAMIC_VERSIONING_BYPASS=0.1.0 just build
python scripts/release_native_dependency.py --verify dist
python scripts/release_native_dependency.py --revert
```

`--revert` is required before `uv run` or `uv sync`: the lock has no `hrns-tui` entry, so
a patched tree no longer resolves.

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

The wrapper's native requirement carries platform markers, so `pip install hrns` adds
the binary on Linux x64, macOS arm64/x64 and Windows x64. Other platforms install the
Python packages and no client binary; see
[version-matched native dependency](#version-matched-native-dependency) for musl Linux.

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
python scripts/release_native_dependency.py --verify dist
python -m venv /tmp/hrns-check
/tmp/hrns-check/bin/pip install --find-links dist hrns
/tmp/hrns-check/bin/hrns-tui --version
/tmp/hrns-check/bin/hrns --version
```

`--verify` fails unless the `hrns` wheel and sdist each declare
`Requires-Dist: hrns-tui==<their own version>` with every supported platform marker, and
unless `hrns-slim` declares no native dependency. The `pip install --find-links dist`
step resolves the native wheel from `dist/` exactly as PyPI would. For local development,
run `just native-install` after `just install` (a `uv sync` drops packages outside the
lockfile); it builds the release wheel with Maturin and installs it into the project
environment, after which bare `hrns` selects it and `HRNS_TEXTUAL=1` forces Textual.

Install in an empty environment, launch the interactive client against the built-in
agent, run one headless prompt, and serve the unpacked bundle against the deployed ACP
runtime. Check that the endpoint, workspace and principal shown in the client match
the deployment configuration, and that an expired or missing credential leaves the
draft visible and the disconnected state shown.
