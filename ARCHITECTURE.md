# Architecture

## CLI

All command-line entry points are built with [Cyclopts](https://cyclopts.readthedocs.io/).
Do not add `argparse`, Click, or Typer parsers.

- `hrns` — `src/hrns/client/cli.py` is the main Cyclopts `App` (commands such as
  `connect`, `run`, `profiles`, and `mcp`). It is exposed through
  `hrns.client.cli:main`.
- `hrns-app` — `src/hrns/app/__main__.py` registers `acp` and `serve` as
  `@app.command` functions. It is exposed through `hrns.app.__main__:main`.
- `hrns-local` — `src/hrns/app/local.py` uses a Cyclopts `App` with `run` as its
  default command. It is exposed through `hrns.app.local:main`.

Commands are plain functions with typed keyword arguments; Cyclopts derives
options, types, defaults, and help text from those signatures. Parameter help is
declared inline with `Annotated[..., Parameter(help="...")]`. Each module exposes
a `main()` that calls its `App`, which keeps the `[project.scripts]` targets
unchanged.

`hrns.client.cli` uses `types.SimpleNamespace` as an internal argument container
for its command implementations, so the codebase has no `argparse` dependency at
all.

## ACP dependency

`agent-client-protocol` is pinned to the 1.0 release candidate (`>=1.0.0rc3,<2`).
That release provides the full ACP v1 method table, including `session/delete`.
`hrns` implements the adapter itself in `src/hrns/app/acp/`, derived from the design
of `pydantic-ai-harness`'s experimental ACP adapter but targeting ACP 1.x directly.

`pydantic-ai-harness`'s `[acp]` extra still declares `agent-client-protocol<0.12`,
so `hrns` depends on the base package and declares ACP directly; the extra
contributes only that one dependency. Harness is still used for the coding
capabilities (`Coder`, `FileSystem`), not for ACP. Remove the split once harness
supports 1.x.

The adapter implements the complete `acp.Agent` protocol. `session/list` and
`session/delete` are backed by `ListableSessionStore`/`DeletableSessionStore`;
provider configuration, MCP-over-ACP, and NES are explicitly unsupported and not
advertised until their features exist (see `TODO.md`).
