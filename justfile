default:
    @just --list

upgrade:
    uv lock --upgrade

install:
    uv sync --locked --all-packages --all-extras

lint:
    uv run ruff check .

build:
    uv build --all-packages

check: lint build
