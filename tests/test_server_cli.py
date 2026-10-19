"""CLI wiring: Uvicorn contract parameters, validation mode and error exits."""

from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn
from _server_helpers import load_test_config
from harness_server import cli
from harness_server.errors import MAX_MESSAGE_BYTES


def _config_file(tmp_path: Path, server: dict | None = None) -> Path:
    load_test_config(tmp_path, server=server)
    return tmp_path / "deployments.toml"


def test_main_passes_the_transport_contract_to_uvicorn(tmp_path, monkeypatch) -> None:
    path = _config_file(tmp_path, server={"host": "127.0.0.1", "port": 9123})
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main(["--config", str(path)]) == 0

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9123
    assert captured["ws_max_size"] == MAX_MESSAGE_BYTES == 16 * 1024 * 1024
    assert captured["ws_ping_interval"] == 30.0
    assert captured["ws_ping_timeout"] == 30.0
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == ["127.0.0.1"]
    assert captured["ssl_certfile"] is None
    assert captured["ssl_keyfile"] is None
    assert captured["app"] is not None


def test_cli_host_and_port_override_the_file(tmp_path, monkeypatch) -> None:
    path = _config_file(tmp_path, server={"host": "127.0.0.1", "port": 9123})
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert (
        cli.main(
            ["--config", str(path), "--host", "0.0.0.0", "--port", "7000"]
        )
        == 0
    )
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 7000


def test_cli_certificates_are_forwarded(tmp_path, monkeypatch) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    path = _config_file(
        tmp_path, server={"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    )
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main(["--config", str(path)]) == 0
    assert captured["ssl_certfile"] == str(cert)
    assert captured["ssl_keyfile"] == str(key)


def test_check_mode_validates_without_serving(tmp_path, monkeypatch, capsys) -> None:
    path = _config_file(tmp_path)

    def explode(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("uvicorn.run must not run in --check mode")

    monkeypatch.setattr(uvicorn, "run", explode)
    assert cli.main(["--config", str(path), "--check"]) == 0
    output = capsys.readouterr().out
    assert "configuration ok" in output
    assert "1 deployment" in output


def test_invalid_configuration_exits_with_status_2(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[server]\nhost = 5\n", encoding="utf-8")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("must not run"))
    assert cli.main(["--config", str(path)]) == 2
    assert "harness-server:" in capsys.readouterr().err


def test_missing_configuration_exits_with_status_2(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("must not run"))
    assert cli.main(["--config", str(tmp_path / "absent.toml")]) == 2


def test_out_of_range_port_is_rejected(tmp_path, monkeypatch) -> None:
    path = _config_file(tmp_path)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("must not run"))
    assert cli.main(["--config", str(path), "--port", "70000"]) == 2


def test_root_path_and_log_level_are_forwarded(tmp_path, monkeypatch) -> None:
    path = _config_file(tmp_path)
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert (
        cli.main(
            [
                "--config",
                str(path),
                "--root-path",
                "/harness",
                "--log-level",
                "warning",
            ]
        )
        == 0
    )
    assert captured["root_path"] == "/harness"
    assert captured["log_level"] == "warning"


def test_log_filter_only_silences_the_uvicorn_denial_quirk() -> None:
    import logging

    logger = logging.getLogger("uvicorn.error")
    cli.install_log_filters()
    cli.install_log_filters()
    filters = [
        item for item in logger.filters if isinstance(item, cli._DenialResponseNoiseFilter)
    ]
    assert len(filters) == 1

    def record(message: str) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, message, None, None)

    assert filters[0].filter(record(cli._UVICORN_DENIAL_NOISE)) is False
    assert filters[0].filter(record("Exception in ASGI application")) is True
