default:
    @just --list

upgrade:
    uv lock --upgrade

install:
    uv sync --locked --all-extras

lint:
    uv run ruff format .
    uv run ruff check . --fix
    uv run pyrefly check . --no-progress-bar

build:
    uv build

check: lint build
