"""Release-wheel checks for the four Harness distributions (CONCEPT.md section 5).

| Distribution       | Declared dependencies (wheel METADATA)                                     |
| ------------------ | -------------------------------------------------------------------------- |
| `harness-protocol` | `pydantic` only                                                            |
| `harness-client`   | `harness-protocol`, `agent-client-protocol`, `textual`, `httpx`, `websockets`, `platformdirs`, `keyring` |
| `harness-runtime`  | `harness-protocol`, `pydantic-ai-slim`, `pydantic-ai-harness`, `pyyaml`, `platformdirs`, `packaging` |
| `harness-server`   | `harness-protocol`, `starlette`, `uvicorn`, `websockets`                    |

Unlike :mod:`test_package_boundaries`, these tests read only the release
artifacts in ``dist/`` produced by ``uv build --all-packages`` (or ``just
build``).  They never build, install, or touch the network: when a built
artifact is missing, the test skips with a reason naming the build command, and
stale ``dist/`` content is the maintainer's responsibility (``just check``
rebuilds fresh artifacts).
"""

from __future__ import annotations

import configparser
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD_COMMAND = "uv build --all-packages"

#: PEP 503 canonical names the *unconditional* METADATA ``Requires-Dist`` lines
#: must declare exactly — no missed dependency, no smuggled one.
REQUIRED_DIST_NAMES: dict[str, frozenset[str]] = {
    "harness-protocol": frozenset({"pydantic"}),
    "harness-client": frozenset(
        {
            "harness-protocol",
            "agent-client-protocol",
            "textual",
            "httpx",
            "websockets",
            "platformdirs",
            "keyring",
        }
    ),
    "harness-runtime": frozenset(
        {
            "harness-protocol",
            "pydantic-ai-slim",
            "pydantic-ai-harness",
            "pyyaml",
            "platformdirs",
            "packaging",
        }
    ),
    "harness-server": frozenset({"harness-protocol", "starlette", "uvicorn", "websockets"}),
}

#: Canonical names never allowed anywhere in a wheel, including extras
#: (`Requires-Dist: ...; extra == '...'`), per CONCEPT.md section 5.
FORBIDDEN_DIST_NAMES: dict[str, frozenset[str]] = {
    "harness-protocol": frozenset(
        {
            "pydantic-ai",
            "pydantic-ai-slim",
            "pydantic-ai-harness",
            "pydantic-graph",
            "textual",
            "starlette",
            "uvicorn",
            "harness-runtime",
            "harness-client",
            "harness-server",
            "e2b",
            "modal",
            "playwright",
        }
    ),
    "harness-client": frozenset(
        {
            "pydantic-ai",
            "pydantic-ai-slim",
            "pydantic-ai-harness",
            "pydantic-graph",
            "harness-runtime",
            "harness-server",
            "starlette",
            "uvicorn",
        }
    ),
    "harness-runtime": frozenset(
        {"textual", "starlette", "uvicorn", "harness-client", "harness-server"}
    ),
    "harness-server": frozenset(
        {
            "textual",
            "pydantic-ai",
            "pydantic-ai-slim",
            "pydantic-ai-harness",
            "pydantic-graph",
            "harness-runtime",
            "harness-client",
        }
    ),
}

#: Conditional (extra-marked) requirements may only use these canonical names.
EXTRA_ONLY_NAMES: dict[str, frozenset[str]] = {
    "harness-protocol": frozenset(),
    "harness-client": frozenset(),
    "harness-runtime": frozenset({"pydantic-ai-slim", "psycopg"}),
    "harness-server": frozenset(),
}

#: Console scripts each wheel must expose (protocol ships none).
WHEEL_CONSOLE_SCRIPTS: dict[str, dict[str, str]] = {
    "harness-protocol": {},
    "harness-client": {"harness": "harness_client.cli:main"},
    "harness-runtime": {"harness-host": "harness_runtime.host:main"},
    "harness-server": {"harness-server": "harness_server.cli:main"},
}


def manifest(distribution: str) -> dict:
    return tomllib.loads((ROOT / "packages" / distribution / "pyproject.toml").read_text("utf-8"))


def canonical_name(requirement: str) -> str:
    """Normalize a requirement line to its PEP 503 distribution name."""
    base = requirement.split(";")[0]
    for index, char in enumerate(base):
        if char in "<>=!~;[" or char.isspace():
            base = base[:index]
            break
    return base.strip().lower().replace("_", "-")


def artifact(distribution: str, pattern: str) -> Path:
    """Return the single built artifact matching ``pattern``, or skip with a clear reason."""
    matches = sorted(DIST.glob(pattern))
    if not matches:
        pytest.skip(
            f"no built {distribution} artifact in {DIST}/ matching {pattern!r} "
            f"(missing or stale build); run `{BUILD_COMMAND}` and re-run this suite"
        )
    return matches[0]


def wheel_path(distribution: str) -> Path:
    project = manifest(distribution)["project"]
    return artifact(
        distribution, f"{distribution.replace('-', '_')}-{project['version']}-py3-none-any.whl"
    )


def sdist_path(distribution: str) -> Path:
    project = manifest(distribution)["project"]
    return artifact(distribution, f"{distribution.replace('-', '_')}-{project['version']}.tar.gz")


def read_wheel_entry(distribution: str, suffix: str) -> str:
    with zipfile.ZipFile(wheel_path(distribution)) as archive:
        entry = next(name for name in archive.namelist() if name.endswith(".dist-info/" + suffix))
        return archive.read(entry).decode("utf-8")


def requires_dist(distribution: str) -> tuple[list[str], list[str]]:
    """Unconditional and extra-marked ``Requires-Dist`` lines of the built wheel."""
    requirements = [
        line.removeprefix("Requires-Dist: ")
        for line in read_wheel_entry(distribution, "METADATA").splitlines()
        if line.startswith("Requires-Dist: ")
    ]
    return [r for r in requirements if "extra" not in r], [r for r in requirements if "extra" in r]


# --- build outputs -------------------------------------------------------------


def test_all_four_release_sdists_exist():
    for distribution in ("harness-protocol", "harness-client", "harness-runtime", "harness-server"):
        sdist_path(distribution)


def test_release_wheels_are_pure_python():
    for distribution in ("harness-protocol", "harness-client", "harness-runtime", "harness-server"):
        project = manifest(distribution)["project"]
        wheel = wheel_path(distribution)
        expected_stem = f"{distribution.replace('-', '_')}-{project['version']}-py3-none-any.whl"
        assert wheel.name == expected_stem, f"unexpected release wheel name: {wheel.name}"
        tags = [
            line.removeprefix("Tag: ")
            for line in read_wheel_entry(distribution, "WHEEL").splitlines()
            if line.startswith("Tag: ")
        ]
        with zipfile.ZipFile(wheel) as archive:
            binaries = [entry for entry in archive.namelist() if entry.endswith((".so", ".pyd"))]
        assert tags == ["py3-none-any"], f"{distribution} wheel must be pure Python, tagged {tags}"
        assert not binaries, f"{distribution} wheel must ship no compiled extensions: {binaries}"


# --- declared dependency boundaries (METADATA) ----------------------------------


@pytest.mark.parametrize(
    "distribution",
    ["harness-protocol", "harness-client", "harness-runtime", "harness-server"],
)
def test_release_wheel_declares_only_concept_dependencies(distribution: str):
    unconditioned, conditioned = requires_dist(distribution)
    declared = {canonical_name(requirement) for requirement in unconditioned}
    forbidden = FORBIDDEN_DIST_NAMES[distribution]

    assert declared == REQUIRED_DIST_NAMES[distribution], (
        f"{distribution} wheel must declare exactly "
        f"{sorted(REQUIRED_DIST_NAMES[distribution])}, declared {sorted(declared)}"
    )
    extra_names = {canonical_name(requirement) for requirement in conditioned}
    assert not extra_names & forbidden, (
        f"{distribution} wheel extras depend on forbidden packages: {sorted(extra_names & forbidden)}"
    )
    unexpected_extras = extra_names - EXTRA_ONLY_NAMES[distribution]
    assert not unexpected_extras, (
        f"{distribution} wheel extras name unexpected packages: {sorted(unexpected_extras)}"
    )

    # Name, version and Python floor echo the manifest the wheel was built from.
    project = manifest(distribution)["project"]
    metadata = read_wheel_entry(distribution, "METADATA")
    assert f"Name: {distribution}" in metadata
    assert f"Version: {project['version']}" in metadata
    assert metadata.splitlines()[0].startswith("Metadata-Version: ")
    requires_python = next(
        line.removeprefix("Requires-Python: ")
        for line in metadata.splitlines()
        if line.startswith("Requires-Python: ")
    )
    assert requires_python == project["requires-python"], (
        f"{distribution} wheel Requires-Python {requires_python!r} != manifest "
        f"{project['requires-python']!r}"
    )


# --- console scripts (dist-info/entry_points.txt) --------------------------------


@pytest.mark.parametrize(
    "distribution",
    ["harness-protocol", "harness-client", "harness-runtime", "harness-server"],
)
def test_release_wheel_exposes_expected_console_scripts(distribution: str):
    wheel = wheel_path(distribution)
    with zipfile.ZipFile(wheel) as archive:
        entry_points = next(
            (n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")), None
        )
        if entry_points is not None:
            parser = configparser.ConfigParser()
            parser.read_string(archive.read(entry_points).decode("utf-8"))
            scripts = dict(parser["console_scripts"]) if parser.has_section("console_scripts") else {}
        else:
            scripts = {}
    expected = WHEEL_CONSOLE_SCRIPTS[distribution]
    assert scripts == expected, (
        f"{distribution} wheel console scripts {scripts} != expected {expected}"
    )


# --- shipped code integrity ------------------------------------------------------


def compile_status(source: bytes, name: str) -> str | None:
    """Diagnostic ``None`` when the source compiles, else the reason it does not."""
    try:
        compile(source, name, "exec")
    except (SyntaxError, ValueError, UnicodeDecodeError) as error:
        return f"  {name}: {error!r}"
    return None


@pytest.mark.parametrize(
    "distribution",
    ["harness-protocol", "harness-client", "harness-runtime", "harness-server"],
)
def test_release_wheel_ships_compilable_python(distribution: str):
    """Every shipped ``.py`` compiles — a patch-marker or truncated file fails here.

    Guards against release artifacts built from transiently corrupted sources
    (observed once as ``+``-prefixed diff lines inside ``harness_client/cli.py``,
    which made every ``harness`` invocation die with a SyntaxError).
    """
    with zipfile.ZipFile(wheel_path(distribution)) as archive:
        sources = {
            name: archive.read(name)
            for name in archive.namelist()
            if name.endswith(".py") and not name.startswith("__pycache__")
        }
    broken = [detail for name, data in sorted(sources.items())
              if (detail := compile_status(data, name))]
    assert not broken, (
        "wheel ships Python files that do not compile:\n" + "\n".join(broken) + "\n"
        "The sources the wheel was built from were likely corrupted after editing; "
        f"re-run `{BUILD_COMMAND}` from clean sources."
    )
