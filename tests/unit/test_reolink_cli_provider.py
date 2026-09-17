import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from reovault.models import RemoteRecording
from reovault.providers.base import AuthError, LocalError, NetworkError, ProtocolError
from reovault.providers.gateway import GatewaySupervisor
from reovault.providers.reolink_cli import ReolinkCliProvider


def _completed(
    payload: dict | None, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess:
    stdout = json.dumps(payload) if payload is not None else ""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def provider():
    gateway = MagicMock(spec=GatewaySupervisor)
    gateway.addr = "127.0.0.1:9000"
    return ReolinkCliProvider(
        alias="doorbell",
        timezone="Europe/Brussels",
        binary="reolink-cli",
        gateway=gateway,
    )


def test_storage_status_parses_documented_fields(provider):
    payload = {
        "ok": True,
        "command": "storage status",
        "data": {"totalGB": 64.0, "remainGB": 12.5, "formatted": True, "mounted": True},
    }
    with patch("subprocess.run", return_value=_completed(payload)):
        status = provider.storage_status()
    assert status.total_gb == 64.0
    assert status.remain_gb == 12.5
    assert status.formatted is True
    assert status.mounted is True
    provider.gateway.ensure_running.assert_called_once()


def test_auth_error_raised_on_exit_code_3(provider):
    payload = {"ok": False, "error": {"message": "invalid credentials"}}
    with (
        patch("subprocess.run", return_value=_completed(payload, returncode=3)),
        pytest.raises(AuthError, match="invalid credentials"),
    ):
        provider.storage_status()


def test_timeout_raises_network_error(provider):
    with (
        patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="reolink-cli", timeout=30)
        ),
        pytest.raises(NetworkError),
    ):
        provider.storage_status()


def test_missing_binary_raises_local_error(provider):
    with patch("subprocess.run", side_effect=FileNotFoundError), pytest.raises(LocalError):
        provider.storage_status()


def test_non_json_output_raises_protocol_error(provider):
    bad = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
    with patch("subprocess.run", return_value=bad), pytest.raises(ProtocolError):
        provider.storage_status()


def test_list_recordings_parses_tolerant_fields(provider):
    payload = {
        "ok": True,
        "data": {
            "truncated": False,
            "scanned": 2,
            "files": [
                {
                    "fileName": "20260417_120000.mp4",
                    "startTime": "2026-04-17T12:00:00",
                    "endTime": "2026-04-17T12:05:00",
                    "size": 12345,
                    "type": "people",
                    "stream": "main",
                }
            ],
        },
    }
    with patch("subprocess.run", return_value=_completed(payload)):
        recordings = provider.list_recordings(
            from_utc=datetime(2026, 4, 17, tzinfo=UTC),
            to_utc=datetime(2026, 4, 18, tzinfo=UTC),
        )
    assert len(recordings) == 1
    rec = recordings[0]
    assert rec.remote_name == "20260417_120000.mp4"
    assert rec.remote_size == 12345
    assert rec.rec_type == "people"
    assert rec.raw_metadata["fileName"] == "20260417_120000.mp4"


def test_list_recordings_raises_protocol_error_on_unrecognized_shape(provider):
    payload = {"ok": True, "data": {"somethingElse": []}}
    with (
        patch("subprocess.run", return_value=_completed(payload)),
        pytest.raises(ProtocolError),
    ):
        provider.list_recordings(
            from_utc=datetime(2026, 4, 17, tzinfo=UTC),
            to_utc=datetime(2026, 4, 18, tzinfo=UTC),
        )


def test_list_recordings_logs_but_does_not_raise_on_truncated(provider, caplog):
    payload = {
        "ok": True,
        "data": {"truncated": True, "scanned": 100000, "files": []},
    }
    with patch("subprocess.run", return_value=_completed(payload)):
        recordings = provider.list_recordings(
            from_utc=datetime(2026, 4, 17, tzinfo=UTC),
            to_utc=datetime(2026, 4, 18, tzinfo=UTC),
        )
    assert recordings == []


def test_fetch_builds_by_name_download_command_with_dash_o(provider, tmp_path):
    dest = tmp_path / "staging" / "clip.mp4"
    payload = {"ok": True, "data": {}}

    def fake_run(cmd, **kwargs):
        # A real `vod download NAME -o FILE` writes the file as a side effect.
        dest.write_bytes(b"0123456789")
        return _completed(payload)

    rec = RemoteRecording(
        remote_name="20260417_120000.mp4",
        start_utc=datetime(2026, 4, 17, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type=None,
        stream=None,
        remote_size=10,
        raw_metadata={},
    )
    with patch("subprocess.run", side_effect=fake_run) as run:
        result = provider.fetch(rec, str(dest))

    called_cmd = run.call_args[0][0]
    assert "vod" in called_cmd
    assert "download" in called_cmd
    assert "20260417_120000.mp4" in called_cmd
    assert "-o" in called_cmd
    assert str(dest) in called_cmd
    assert result.bytes_written == 10
