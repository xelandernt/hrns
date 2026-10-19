default:
    @just --list

setup:
    uv sync --locked --all-packages

test:
    uv run pytest -q

lint:
    uv run ruff check .

build:
    uv build --all-packages

demo:
    uv run harness connect --demo

serve-demo:
    uv run python scripts/serve_demo.py

check: lint test build
