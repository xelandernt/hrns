default:
    @just --list

upgrade:
    uv lock --upgrade

install:
    uv sync --locked --all-extras

lint:
    uv run ruff check .

build:
    uv build

check: lint build
