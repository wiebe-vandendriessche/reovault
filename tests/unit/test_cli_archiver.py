"""End-to-end CLI tests for `run`/`probe`/`status`/`verify`/`export`, with
`subprocess.run` mocked to simulate a real `reolink-cli` + gateway. This
exercises the full `_bootstrap` wiring (preflight, provider, vault,
repository, archiver) that the integration tests (which use `FakeProvider`
directly) don't touch."""

import json
import subprocess
from unittest.mock import patch

from typer.testing import CliRunner

from reovault.cli import app
from reovault.crypto.keyring import generate_master_key

runner = CliRunner()

PINNED_VERSION = "0.19.0"


def _toml(tmp_path) -> str:
    config = tmp_path / "reovault.toml"
    config.write_text(
        f"""
        [[devices]]
        alias = "doorbell"
        channel = 0
        timezone = "UTC"

        [reolink_cli]
        pinned_version = "{PINNED_VERSION}"

        [storage]
        config_dir = "{tmp_path / "config"}"
        vault_dir = "{tmp_path / "vault"}"
        staging_dir = "{tmp_path / "staging"}"
        db_path = "{tmp_path / "config" / "reovault.db"}"
        master_key_path = "{tmp_path / "config" / "master.key"}"
        """
    )
    return str(config)


def _fake_subprocess_run(downloaded_content: bytes = b"fake mp4 bytes"):
    def _run(cmd, capture_output=True, text=True, timeout=None, input=None):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"reolink-cli {PINNED_VERSION}\n", "")
        if "status" in cmd and "gateway" in cmd:
            payload = {"ok": True, "data": {"addr": "127.0.0.1:9000", "listening": True}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if "search" in cmd:
            payload = {
                "ok": True,
                "data": {
                    "truncated": False,
                    "scanned": 1,
                    "total": 1,
                    "items": [
                        {
                            "channel": 0,
                            "name": "clip1",
                            "startTime": "2026-04-17T12:00:00",
                            "endTime": "2026-04-17T12:05:00",
                            "fileSize": len(downloaded_content),
                            "recordType": "md",
                            "streamType": "mainStream",
                        }
                    ],
                },
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if "download" in cmd:
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "wb") as f:
                f.write(downloaded_content)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"ok": True, "data": {}}), "")
        if "storage" in cmd:
            payload = {
                "ok": True,
                "data": {
                    "items": [
                        {"formatted": True, "mounted": True, "totalGB": 100.0, "remainGB": 50.0}
                    ]
                },
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        raise AssertionError(f"unexpected command: {cmd}")

    return _run


def _env(config_file: str) -> dict:
    return {"REOVAULT_CONFIG_FILE": config_file, "REOVAULT_MASTER_PASSPHRASE": "test passphrase"}


def _init_key(tmp_path, config_file: str) -> None:
    key_path = tmp_path / "config" / "master.key"
    generate_master_key(key_path, b"test passphrase")


def test_run_archives_one_recording_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess_run())
    config_file = _toml(tmp_path)
    _init_key(tmp_path, config_file)

    result = runner.invoke(
        app,
        ["run", "--from", "2026-04-17T00:00:00", "--to", "2026-04-17T23:59:59"],
        env=_env(config_file),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["discovered"] == 1
    assert payload["downloaded"] == 1
    assert payload["failed"] == 0

    vault_files = list((tmp_path / "vault").rglob("*.enc"))
    assert len(vault_files) == 1


def test_probe_prints_recordings_and_storage(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess_run())
    config_file = _toml(tmp_path)
    _init_key(tmp_path, config_file)

    result = runner.invoke(app, ["probe"], env=_env(config_file))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["recordings_last_24h"] >= 0
    assert payload["storage"]["mounted"] is True


def test_status_after_a_run_reports_archived_count_and_bytes(tmp_path, monkeypatch):
    content = b"fake mp4 bytes"
    monkeypatch.setattr("subprocess.run", _fake_subprocess_run(content))
    config_file = _toml(tmp_path)
    _init_key(tmp_path, config_file)

    runner.invoke(
        app,
        ["run", "--from", "2026-04-17T00:00:00", "--to", "2026-04-17T23:59:59"],
        env=_env(config_file),
    )
    result = runner.invoke(app, ["status"], env=_env(config_file))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["archived_count"] == 1
    assert payload["archived_bytes"] == len(content)


def test_verify_and_export_round_trip_after_a_run(tmp_path, monkeypatch):
    content = b"fake mp4 bytes"
    monkeypatch.setattr("subprocess.run", _fake_subprocess_run(content))
    config_file = _toml(tmp_path)
    _init_key(tmp_path, config_file)

    runner.invoke(
        app,
        ["run", "--from", "2026-04-17T00:00:00", "--to", "2026-04-17T23:59:59"],
        env=_env(config_file),
    )

    verify_result = runner.invoke(app, ["verify", "--all"], env=_env(config_file))
    assert verify_result.exit_code == 0, verify_result.output
    assert "OK" in verify_result.output

    out_path = tmp_path / "exported.mp4"
    export_result = runner.invoke(app, ["export", "1", "-o", str(out_path)], env=_env(config_file))
    assert export_result.exit_code == 0, export_result.output
    assert out_path.read_bytes() == content


def test_run_fails_preflight_on_wrong_pinned_version(tmp_path, monkeypatch):
    def _run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "reolink-cli 0.18.0\n", "")
        raise AssertionError("should not get past the version check")

    monkeypatch.setattr("subprocess.run", _run)
    config_file = _toml(tmp_path)
    _init_key(tmp_path, config_file)

    result = runner.invoke(
        app,
        ["run", "--from", "2026-04-17T00:00:00", "--to", "2026-04-17T23:59:59"],
        env=_env(config_file),
    )
    assert result.exit_code == 1
    assert "0.19.0" in result.output


def test_reconcile_runs_without_error(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess_run())
    config_file = _toml(tmp_path)
    _init_key(tmp_path, config_file)

    result = runner.invoke(app, ["reconcile"], env=_env(config_file))
    assert result.exit_code == 0, result.output
    assert "complete" in result.output.lower()


def test_daemon_starts_scheduler_and_serves_healthz(tmp_path, monkeypatch):
    """Mocks `uvicorn.run` so this doesn't actually bind a port and block;
    what's under test is that `daemon` wires the scheduler and the app
    together and shuts the scheduler down afterward, not uvicorn itself."""
    monkeypatch.setattr("subprocess.run", _fake_subprocess_run())
    config_file = _toml(tmp_path)
    _init_key(tmp_path, config_file)

    with patch("reovault.cli.uvicorn.run") as run_fn:
        result = runner.invoke(app, ["daemon"], env=_env(config_file))

    assert result.exit_code == 0, result.output
    run_fn.assert_called_once()
    app_arg = run_fn.call_args[0][0]
    assert app_arg.title == "ReoVault"
