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
    uv run ruff format hrns_slim examples scripts
    uv run ruff check . --fix
    just --format
# verify formatting, lint, and types without changing files
lint:
    uv run ruff format --check hrns_slim examples scripts
    uv run ruff check .
    uv run pyrefly check hrns_slim examples scripts --no-progress-bar
    just --fmt --check
# A wheel built from the unpacked sdist has no git history, so uv-dynamic-versioning
# falls back to `fallback-version` and pins the sibling packages to the wrong version.
# Build the sdist and the wheel from the source tree in separate invocations.

# build the Python distributions
build:
    uv build --all-packages --sdist --clear
    uv build --all-packages --wheel
# build and install the native client wheel for this machine
native-install:
    uvx maturin build --release --locked --manifest-path rust/Cargo.toml
    uv pip install --python .venv --no-deps --force-reinstall "$(ls -t rust/target/wheels/hrns_tui-*.whl | head -1)"
# check the project
check:
    @just lint
    @just build
