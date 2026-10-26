[default]
[private]
_:
    @just --list

# update dependencies
upgrade:
    uv lock --upgrade
    @just install

# install dependencies
install:
    uv sync --locked --all-extras

# apply automatic formatting and lint fixes
fix:
    uv run ruff format src examples test.py
    uv run ruff check . --fix
    just --format

# verify formatting, lint, and types without changing files
lint:
    uv run ruff format --check src examples
    uv run ruff check .
    uv run pyrefly check src examples --no-progress-bar
    just --fmt --check

# build
build:
    uv build

# check the project
check:
    @just lint
    @just build
