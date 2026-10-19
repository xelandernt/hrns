"""Configuration loading, validation and the server-owned worker environment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from _server_helpers import (
    EXAMPLES,
    TOKEN_ALICE,
    TOKEN_BOB,
    config_document,
    deployment,
    principal,
    write_config,
)
from harness_server import load_config, sha256_hex
from harness_server.config import ConfigError, DeploymentConfig
from harness_server.errors import ApiError
from harness_server.worker import WorkerSlots, WorkerSpawnError, build_worker_env


def _write(tmp_path: Path, document: dict) -> Path:
    return write_config(tmp_path, document)


def test_loads_and_normalizes_paths(tmp_path: Path) -> None:
    worker_dir = tmp_path / "worker-home"
    worker_dir.mkdir()
    document = config_document(
        tmp_path,
        deployments=[deployment(cwd="worker-home")],
        server={"require_tls": True, "max_worker_count": 3},
    )
    config = load_config(_write(tmp_path, document))

    assert config.server.require_tls is True
    assert config.server.max_worker_count == 3
    assert config.server.store_path == str((tmp_path / "store").resolve())
    assert config.deployments[0].cwd == str(worker_dir.resolve())
    assert config.principals[0].id == "alice"
    assert config.principals[0].token_hashes == [sha256_hex(TOKEN_ALICE)]


def test_postgres_store_url_is_passed_to_every_worker(tmp_path: Path) -> None:
    document = config_document(tmp_path)
    dsn = "postgresql://harness:secret@db.example/harness?sslmode=require"
    document["server"]["store_url"] = dsn
    config = load_config(_write(tmp_path, document))

    from harness_server.app import HarnessState

    state = HarnessState.from_config(config)
    assert state.worker_store_path("coder.r1") == dsn
    assert state.worker_store_path("researcher.r2") == dsn


def test_blank_store_url_rejected(tmp_path: Path) -> None:
    document = config_document(tmp_path)
    document["server"]["store_url"] = "  "
    with pytest.raises(ConfigError, match="store_url"):
        load_config(_write(tmp_path, document))


def test_requires_at_least_one_deployment(tmp_path: Path) -> None:
    document = config_document(tmp_path, deployments=[], principals=[principal()])
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, document))


def test_requires_at_least_one_principal(tmp_path: Path) -> None:
    document = config_document(tmp_path, deployments=[deployment()], principals=[])
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, document))


def test_duplicate_deployment_ids_rejected(tmp_path: Path) -> None:
    document = config_document(
        tmp_path,
        deployments=[deployment(), deployment(name="Duplicate")],
    )
    with pytest.raises(ConfigError, match="duplicate deployment ids"):
        load_config(_write(tmp_path, document))


def test_invalid_deployment_id_rejected(tmp_path: Path) -> None:
    document = config_document(tmp_path, deployments=[deployment(id="Not Valid")])
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, document))


def test_only_one_recommended_revision_per_agent_name(tmp_path: Path) -> None:
    document = config_document(
        tmp_path,
        deployments=[
            deployment(id="agent.r1", name="Agent", recommended=True),
            deployment(id="agent.r2", name="Agent", recommended=True),
        ],
    )
    with pytest.raises(ConfigError, match="multiple revisions are recommended"):
        load_config(_write(tmp_path, document))


def test_duplicate_token_hash_across_principals_rejected(tmp_path: Path) -> None:
    document = config_document(
        tmp_path,
        principals=[
            principal("alice", token=TOKEN_ALICE),
            principal("bob", token=TOKEN_ALICE),
        ],
    )
    with pytest.raises(ConfigError, match="token hash"):
        load_config(_write(tmp_path, document))


def test_plaintext_or_malformed_token_hash_rejected(tmp_path: Path) -> None:
    document = config_document(tmp_path)
    document["principal"][0]["token_hashes"] = ["not-a-sha256-digest"]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, document))

    document["principal"][0]["token_hashes"] = [TOKEN_BOB.upper()]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, document))


def test_principal_may_only_reference_known_agents(tmp_path: Path) -> None:
    document = config_document(
        tmp_path,
        principals=[principal(agents=["fake.acp.r1", "ghost.agent.r9"])],
    )
    with pytest.raises(ConfigError, match="unknown deployments"):
        load_config(_write(tmp_path, document))


def test_lockfile_digest_is_verified(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "coder.lock"
    lock.parent.mkdir()
    lock.write_text("pydantic-ai==1.2.3\n", encoding="utf-8")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()

    good = config_document(
        tmp_path,
        deployments=[deployment(lockfile_path="locks/coder.lock", lockfile_sha256=digest)],
    )
    config = load_config(_write(tmp_path, good))
    assert config.deployments[0].lockfile_path == str(lock.resolve())
    assert config.deployments[0].lockfile_sha256 == digest

    bad = config_document(
        tmp_path,
        deployments=[deployment(lockfile_path="locks/coder.lock", lockfile_sha256="0" * 64)],
    )
    with pytest.raises(ConfigError, match="digest mismatch"):
        load_config(_write(tmp_path, bad))

    missing = config_document(
        tmp_path, deployments=[deployment(lockfile_path="locks/missing.lock")]
    )
    with pytest.raises(ConfigError, match="lockfile"):
        load_config(_write(tmp_path, missing))


def test_nested_lockfile_table_is_accepted(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "nested.lock"
    lock.parent.mkdir()
    lock.write_text("resolved==1\n", encoding="utf-8")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()

    document = config_document(tmp_path)
    deployment_entry = document["deployment"][0]
    deployment_entry.pop("lockfile_path", None)
    deployment_entry["lockfile"] = {"path": "locks/nested.lock", "sha256": digest}
    config = load_config(_write(tmp_path, document))
    assert config.deployments[0].lockfile_path == str(lock.resolve())
    assert config.deployments[0].lockfile_sha256 == digest

    bad = config_document(tmp_path)
    bad["deployment"][0]["lockfile"] = {"path": "x.lock", "digest": "nope"}
    with pytest.raises(ConfigError, match="only path and sha256"):
        load_config(_write(tmp_path, bad))


def test_reserved_harness_environment_rejected(tmp_path: Path) -> None:
    for field in ("env", "env_from_environment"):
        value = {"HARNESS_PRINCIPAL": "spoof"} if field == "env" else ["HARNESS_STORE"]
        document = config_document(tmp_path, deployments=[deployment(**{field: value})])
        with pytest.raises(ConfigError, match="reserved HARNESS_"):
            load_config(_write(tmp_path, document))


def test_workspace_roots_must_be_absolute_and_default_listed(tmp_path: Path) -> None:
    relative = config_document(
        tmp_path, deployments=[deployment(allowed_cwds=["workspace"], default_cwd=None)]
    )
    with pytest.raises(ConfigError, match="absolute paths"):
        load_config(_write(tmp_path, relative))

    not_listed = config_document(
        tmp_path,
        deployments=[deployment(allowed_cwds=["/srv/a"], default_cwd="/srv/b")],
    )
    with pytest.raises(ConfigError, match="default_cwd"):
        load_config(_write(tmp_path, not_listed))


def test_absolute_command_must_be_executable(tmp_path: Path) -> None:
    document = config_document(
        tmp_path, deployments=[deployment(command=["/nonexistent/harness-host"])]
    )
    with pytest.raises(ConfigError, match="not an executable"):
        load_config(_write(tmp_path, document))


def test_tls_certificate_pair_required(tmp_path: Path) -> None:
    document = config_document(
        tmp_path,
        server={"ssl_certfile": "/tmp/only-cert.pem"},
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, document))


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read configuration"):
        load_config(tmp_path / "absent.toml")


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[server\nhost = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_worker_env_is_server_owned() -> None:
    deployment_config = DeploymentConfig.model_validate(
        deployment(
            env={"FAKE_RESOURCE": "literal-value"},
            env_from_environment=["REFERENCED_VALUE"],
        )
    )
    env = build_worker_env(
        deployment_config,
        principal_id="alice",
        store_path="/srv/store",
        passthrough=["PATH", "HOME", "HARNESS_PRINCIPAL", "MISSING"],
        environ={
            "PATH": "/usr/bin",
            "HOME": "/home/alice",
            "HARNESS_PRINCIPAL": "mallory",
            "REFERENCED_VALUE": "resolved",
        },
    )

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/alice"
    assert env["FAKE_RESOURCE"] == "literal-value"
    assert env["REFERENCED_VALUE"] == "resolved"
    assert env["HARNESS_PRINCIPAL"] == "alice"  # contract beats passthrough
    assert env["HARNESS_DEPLOYMENT_ID"] == "fake.acp.r1"
    assert json.loads(env["HARNESS_ALLOWED_CWDS"]) == ["/workspace/fake"]
    assert env["HARNESS_STORE"] == "/srv/store"
    assert env["HARNESS_HOSTED"] == "1"
    assert "MISSING" not in env


def test_missing_referenced_environment_is_a_spawn_error() -> None:
    deployment_config = DeploymentConfig.model_validate(
        deployment(env_from_environment=["REQUIRED_RESOURCE"])
    )
    with pytest.raises(WorkerSpawnError, match="missing"):
        build_worker_env(
            deployment_config,
            principal_id="alice",
            store_path="/srv/store",
            passthrough=[],
            environ={},
        )


def test_worker_slots_enforce_both_limits() -> None:
    slots = WorkerSlots(max_total=2, max_per_principal=1)
    slots.acquire("alice")
    with pytest.raises(ApiError) as excinfo:
        slots.acquire("alice")
    assert excinfo.value.code == "SERVER_BUSY"
    assert excinfo.value.details["reason"] == "principal_limit"

    slots.acquire("bob")
    with pytest.raises(ApiError) as excinfo:
        slots.acquire("carol")
    assert excinfo.value.details["reason"] == "worker_limit"

    slots.release("alice")
    slots.acquire("carol")
    assert slots.active == 2
    slots.release("bob")
    slots.release("carol")
    assert slots.active == 0


def test_example_deployment_config_loads() -> None:
    example = EXAMPLES / "deployments.toml"
    if not example.exists():  # pragma: no cover - example is part of the tree
        pytest.skip("examples/deployments.toml not present")
    config = load_config(example)
    assert config.deployments
    assert config.principals
    ids = {deployment_config.id for deployment_config in config.deployments}
    for principal_config in config.principals:
        assert set(principal_config.allowed_agent_ids) <= ids
