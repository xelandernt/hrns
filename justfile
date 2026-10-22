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

# lint the project
lint:
    uv run ruff format .
    uv run ruff check . --fix
    uv run pyrefly check . --no-progress-bar
    just --format

# build
build:
    uv build

# check the project
check:
    @just lint
    @just build
    just --fmt --check
