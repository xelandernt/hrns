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
