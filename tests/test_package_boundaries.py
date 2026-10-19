"""Import-boundary checks for the four Harness distributions (CONCEPT.md section 5).

| Distribution       | Must not require                        |
| ------------------ | --------------------------------------- |
| `harness-protocol` | Pydantic AI, Textual, sandbox SDKs      |
| `harness-runtime`  | Textual, the network server, the client |
| `harness-client`   | Pydantic AI, the runtime (AI/plugins)   |
| `harness-server`   | Textual, the runtime/AI                 |

Tests inspect three layers so a mistake cannot hide behind one of them:

1. the source tree (statically parsed, including lazy function-level imports),
2. a fresh interpreter's ``sys.modules`` after importing package entrypoints,
3. the built ``harness-protocol`` wheel (metadata plus the code it ships).

Sibling packages (runtime/client/server) are being implemented concurrently;
their checks are skipped until source exists, then enforced automatically.
"""

import ast
import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"

# The only third-party modules harness-protocol may import: pydantic and the
# runtime family that pydantic itself ships.
PYDANTIC_FAMILY = frozenset(
    {"pydantic", "pydantic_core", "annotated_types", "typing_extensions", "typing_inspection"}
)

# Import roots forbidden in Python source (module names use underscores).
AI_MODULES = frozenset({"pydantic_ai", "pydantic_ai_harness", "pydantic_graph"})
UI_MODULES = frozenset({"textual"})
SERVER_MODULES = frozenset({"starlette", "uvicorn"})
SANDBOX_MODULES = frozenset({"e2b", "modal", "smolmachines", "playwright"})
TRANSPORT_MODULES = frozenset({"websockets", "httpx", "httpx2"})
SIBLING_MODULES = frozenset(
    {"harness_protocol", "harness_runtime", "harness_client", "harness_server"}
)

# Distribution names forbidden in manifests (PEP 503 canonical, hyphenated).
AI_DISTS = frozenset({"pydantic-ai", "pydantic-ai-slim", "pydantic-ai-harness", "pydantic-graph"})
UI_DISTS = frozenset({"textual"})
SERVER_DISTS = frozenset({"starlette", "uvicorn"})
SANDBOX_DISTS = frozenset({"e2b", "modal", "smolmachines", "playwright"})
TRANSPORT_DISTS = frozenset({"websockets", "httpx", "httpx2"})
SIBLING_DISTS = frozenset(
    {"harness-protocol", "harness-runtime", "harness-client", "harness-server"}
)

HEAVY_FOR_PROTOCOL_MODULES = (
    AI_MODULES | UI_MODULES | SERVER_MODULES | SANDBOX_MODULES | TRANSPORT_MODULES | SIBLING_MODULES
)
HEAVY_FOR_PROTOCOL_DISTS = (
    AI_DISTS | UI_DISTS | SERVER_DISTS | SANDBOX_DISTS | TRANSPORT_DISTS | SIBLING_DISTS
)


def source_dir(distribution: str) -> Path:
    return PACKAGES / distribution / "src" / distribution.replace("-", "_")


def manifest(distribution: str) -> dict:
    return tomllib.loads((PACKAGES / distribution / "pyproject.toml").read_text(encoding="utf-8"))


def canonical_name(requirement: str) -> str:
    """Normalize a requirement string to its PEP 503 distribution name."""
    base = requirement.split(";")[0]
    for index, char in enumerate(base):
        if char in "<>=!~;[" or char.isspace():
            base = base[:index]
            break
    return base.strip().lower().replace("_", "-")


def all_requirements(distribution: str) -> list[str]:
    project = manifest(distribution)["project"]
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    return requirements


def import_roots(path: Path) -> set[str]:
    """Top-level imported module names, including lazy imports inside functions."""
    roots: set[str] = set()
    for file in sorted(path.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def third_party_roots(path: Path) -> set[str]:
    return import_roots(path) - sys.stdlib_module_names


def modules_in(src: Path) -> tuple[str, ...]:
    """Top-level module stems that belong to the package (excluding __init__)."""
    return tuple(sorted(file.stem for file in src.glob("*.py") if file.stem != "__init__"))


def imported_roots(package: str, submodules: tuple[str, ...] = ()) -> tuple[int, set[str], str]:
    """Import ``package`` in a fresh interpreter; return (code, loaded roots, stderr)."""
    script = "\n".join(
        [
            "import importlib, json, sys",
            f'importlib.import_module("{package}")',
            *(f'importlib.import_module("{package}.{name}")' for name in submodules),
            "print(json.dumps(sorted({name.split('.')[0] for name in sys.modules})))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        return result.returncode, set(), result.stderr
    loaded = set(json.loads(result.stdout.splitlines()[-1]))
    loaded.discard(package.split(".")[0])  # the package under test is allowed to load itself
    return result.returncode, loaded, result.stderr


def assert_no_forbidden_imports(roots: set[str], forbidden: frozenset[str], scope: str) -> None:
    offenders = roots & forbidden
    assert not offenders, (
        f"{scope} must not import these modules per CONCEPT.md section 5: {sorted(offenders)}"
    )


def assert_no_forbidden_dependencies(
    requirements: list[str], forbidden: frozenset[str], scope: str
) -> None:
    names = [canonical_name(requirement) for requirement in requirements]
    offenders = {name for name in names if name in forbidden}
    assert not offenders, (
        f"{scope} must not depend on these packages per CONCEPT.md section 5: {offenders}"
    )


# --- harness-protocol: source tree -------------------------------------------


def test_protocol_source_imports_only_stdlib_and_pydantic():
    src = source_dir("harness-protocol")
    assert src.is_dir(), "harness-protocol source must exist"
    strays = third_party_roots(src) - PYDANTIC_FAMILY
    assert not strays, (
        f"harness-protocol must not import AI/UI/network code, found: {sorted(strays)}"
    )


# --- harness-protocol: built wheel boundary -----------------------------------


def test_built_protocol_wheel_is_dependency_minimal(tmp_path: Path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv (build tooling from the dev environment) is not on PATH")
    result = subprocess.run(
        [uv, "build", "--package", "harness-protocol", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"harness-protocol wheel build failed:\n{result.stderr}{result.stdout}"
    )

    version = manifest("harness-protocol")["project"]["version"]
    wheels = list(tmp_path.glob(f"harness_protocol-{version}-*.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one protocol wheel, built: {list(tmp_path.iterdir())}"
    )

    contents = tmp_path / "wheel_src"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
        archive.extractall(contents)

    requires = [
        line.removeprefix("Requires-Dist: ")
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist: ")
    ]
    dependencies = {canonical_name(requirement) for requirement in requires}
    offenders = dependencies & (HEAVY_FOR_PROTOCOL_DISTS - SIBLING_DISTS)
    assert not offenders, f"built harness-protocol wheel requires forbidden packages: {offenders}"
    assert dependencies <= {"pydantic"}, (
        f"harness-protocol wheels must depend only on pydantic, found {sorted(dependencies)}"
    )

    binaries = [name for name in names if name.endswith((".so", ".pyd"))]
    assert not binaries, f"protocol must be a pure-Python wheel, contains: {binaries}"

    strays = third_party_roots(contents / "harness_protocol") - PYDANTIC_FAMILY
    assert not strays, (
        f"shipped harness_protocol code imports non-contract packages: {sorted(strays)}"
    )


# --- harness-protocol: fresh-interpreter import graph -------------------------


def test_importing_protocol_does_not_pull_heavy_dependencies():
    modules = modules_in(source_dir("harness-protocol"))
    status, loaded, stderr = imported_roots("harness_protocol", modules)
    assert status == 0, f"harness_protocol failed to import in a fresh interpreter:\n{stderr}"
    assert "pydantic" in loaded, f"expected the pydantic dependency to load, got: {sorted(loaded)}"
    assert_no_forbidden_imports(loaded, HEAVY_FOR_PROTOCOL_MODULES, "harness_protocol import")


# --- package manifests ---------------------------------------------------------


def test_manifests_declare_correct_wheel_targets():
    for distribution in ("harness-protocol", "harness-runtime", "harness-client", "harness-server"):
        module = distribution.replace("-", "_")
        tool = manifest(distribution)["tool"]["hatch"]["build"]["targets"]["wheel"]
        assert tool["packages"] == [f"src/{module}"], (
            f"{distribution} wheel target must package src/{module}"
        )


def test_manifests_respect_dependency_boundaries():
    for distribution, forbidden in {
        "harness-protocol": HEAVY_FOR_PROTOCOL_DISTS,
        "harness-runtime": UI_DISTS | SERVER_DISTS | {"harness-client", "harness-server"},
        "harness-client": AI_DISTS | {"harness-runtime", "harness-server"},
        "harness-server": UI_DISTS | AI_DISTS | {"harness-runtime", "harness-client"},
    }.items():
        assert_no_forbidden_dependencies(
            all_requirements(distribution), forbidden, f"{distribution} manifest"
        )

    protocol = {
        canonical_name(req)
        for req in manifest("harness-protocol")["project"].get("dependencies", [])
    }
    assert protocol <= {"pydantic"}, (
        f"harness-protocol runtime dependencies must stay within the pydantic budget: {sorted(protocol)}"
    )


# --- sibling distributions: enforced as they land ------------------------------


def test_runtime_source_never_touches_ui_or_the_network_server():
    src = source_dir("harness-runtime")
    if not src.is_dir():
        pytest.skip("harness-runtime source has not landed yet")
    assert_no_forbidden_imports(
        import_roots(src),
        UI_MODULES | SERVER_MODULES | {"harness_client", "harness_server"},
        "harness-runtime source",
    )


def test_runtime_entrypoint_does_not_load_ui_or_the_network_server():
    src = source_dir("harness-runtime")
    if not src.is_dir():
        pytest.skip("harness-runtime source has not landed yet")
    status, loaded, stderr = imported_roots("harness_runtime", modules_in(src))
    if status != 0:
        pytest.skip(f"harness_runtime is not importable yet: {stderr.strip().splitlines()[-1]}")
    assert_no_forbidden_imports(
        loaded,
        UI_MODULES | SERVER_MODULES | {"harness_client", "harness_server"},
        "harness_runtime import",
    )


def test_client_source_never_imports_runtime_or_ai():
    src = source_dir("harness-client")
    if not src.is_dir():
        pytest.skip("harness-client source has not landed yet")
    assert_no_forbidden_imports(
        import_roots(src),
        AI_MODULES | {"harness_runtime", "harness_server"},
        "harness-client source",
    )


def test_client_entrypoint_does_not_load_ai_or_runtime():
    src = source_dir("harness-client")
    if not src.is_dir():
        pytest.skip("harness-client source has not landed yet")
    status, loaded, stderr = imported_roots("harness_client", modules_in(src))
    if status != 0:
        pytest.skip(f"harness_client is not importable yet: {stderr.strip().splitlines()[-1]}")
    assert_no_forbidden_imports(
        loaded, AI_MODULES | {"harness_runtime", "harness_server"}, "harness_client import"
    )


def test_server_source_never_imports_ui_or_the_runtime():
    src = source_dir("harness-server")
    if not src.is_dir():
        pytest.skip("harness-server source has not landed yet")
    assert_no_forbidden_imports(
        import_roots(src),
        UI_MODULES | AI_MODULES | {"harness_runtime", "harness_client"},
        "harness-server source",
    )


def test_server_entrypoint_does_not_load_ui_or_the_runtime():
    src = source_dir("harness-server")
    if not src.is_dir():
        pytest.skip("harness-server source has not landed yet")
    status, loaded, stderr = imported_roots("harness_server", modules_in(src))
    if status != 0:
        pytest.skip(f"harness_server is not importable yet: {stderr.strip().splitlines()[-1]}")
    assert_no_forbidden_imports(
        loaded,
        UI_MODULES | AI_MODULES | {"harness_runtime", "harness_client"},
        "harness_server import",
    )


# --- shared-contract surface ---------------------------------------------------


def test_sibling_packages_only_import_names_exported_by_protocol():
    import harness_protocol

    requested: dict[str, set[str]] = {}
    for distribution in ("harness-runtime", "harness-client", "harness-server"):
        src = source_dir(distribution)
        if not src.is_dir():
            continue
        for file in sorted(src.rglob("*.py")):
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module == "harness_protocol"
                ):
                    names = {alias.name for alias in node.names if alias.name != "*"}
                    requested.setdefault(distribution, set()).update(names)

    missing = {
        distribution: sorted(names - set(harness_protocol.__all__))
        for distribution, names in requested.items()
        if names - set(harness_protocol.__all__)
    }
    assert not missing, (
        "packages import harness_protocol names that are not exported, so they break at "
        f"import time: {missing}. Add each name to the .models/.transport import block and "
        "__all__ in packages/harness-protocol/src/harness_protocol/__init__.py "
        "(BindingId is currently missing)."
    )
