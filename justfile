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
    uv sync --locked --all-extras --all-packages
    uv run prek install
# preview the documentation locally
docs:
    uv run --group docs zensical serve
# build the documentation site into site/
docs-build:
    uv run --group docs zensical build --clean
# apply automatic formatting and lint fixes
fix:
    uv run ruff format hrns_slim examples
    uv run ruff check . --fix
    just --format
# verify formatting, lint, and types without changing files
lint:
    uv run ruff format --check hrns_slim examples
    uv run ruff check .
    uv run pyrefly check hrns_slim examples --no-progress-bar
    just --fmt --check
# build
build:
    uv build --all-packages
# check the project
check:
    @just lint
    @just build
