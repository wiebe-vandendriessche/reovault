import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from reovault.models import RemoteRecording
from reovault.providers.base import (
    AuthError,
    DeviceError,
    LocalError,
    NetworkError,
    ProtocolError,
    describe_exit_code,
)
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


def test_storage_status_parses_real_nested_shape(provider):
    """Verified 2026-09-17 against a real D340W doorbell: fields are nested
    under `data.items[0]`, not flat under `data`."""
    payload = {
        "ok": True,
        "command": "storage",
        "protocol": "v20",
        "data": {
            "items": [
                {
                    "formatted": True,
                    "mounted": True,
                    "number": 0,
                    "remainGB": 191.35,
                    "totalGB": 238.72,
                }
            ]
        },
    }
    with patch("subprocess.run", return_value=_completed(payload)):
        status = provider.storage_status()
    assert status.total_gb == 238.72
    assert status.remain_gb == 191.35
    assert status.formatted is True
    assert status.mounted is True


def test_storage_status_falls_back_to_flat_shape(provider):
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


def test_storage_status_empty_items_list_returns_all_none(provider):
    payload = {"ok": True, "data": {"items": []}}
    with patch("subprocess.run", return_value=_completed(payload)):
        status = provider.storage_status()
    assert status.total_gb is None
    assert status.mounted is None
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


def test_list_recordings_parses_real_field_names(provider):
    """Verified 2026-09-17 against a real D340W doorbell. Real `name` values
    have no file extension (e.g. "0120260916111127"), and `recordType` can be
    a comma-separated list of simultaneously-true types (e.g. "md,people"),
    not a single enum value."""
    payload = {
        "ok": True,
        "data": {
            "truncated": False,
            "scanned": 1,
            "total": 1,
            "items": [
                {
                    "channel": 0,
                    "name": "0120260916111127",
                    "startTime": "2026-09-16T11:11:27",
                    "endTime": "2026-09-16T11:16:26",
                    "fileSize": 160336581,
                    "recordType": "md,people",
                    "streamType": "mainStream",
                }
            ],
        },
    }
    with patch("subprocess.run", return_value=_completed(payload)):
        recordings = provider.list_recordings(
            from_utc=datetime(2026, 9, 16, tzinfo=UTC),
            to_utc=datetime(2026, 9, 17, tzinfo=UTC),
        )
    assert len(recordings) == 1
    rec = recordings[0]
    assert rec.remote_name == "0120260916111127"
    assert rec.remote_size == 160336581
    assert rec.rec_type == "md,people"
    assert rec.stream == "mainStream"
    assert rec.raw_metadata["name"] == "0120260916111127"
    # start_utc must be genuinely UTC, not just numerically coincidental with
    # it: astimezone(tz=None) (a real bug fixed here) converts to whatever
    # timezone the *process* happens to be running under, not the device's
    # configured tz, and would only "look right" when the two happen to
    # match, e.g. on a dev box that happens to run with TZ=UTC.
    assert rec.start_utc.tzinfo is UTC
    assert rec.start_utc == datetime(
        2026, 9, 16, 9, 11, 27, tzinfo=UTC
    )  # 11:11:27 Brussels (UTC+2)


def test_list_recordings_parses_fallback_field_names(provider):
    """Tolerance for the documented-but-unobserved key spellings, kept as a
    fallback (see module docstring in reolink_cli.py)."""
    payload = {
        "ok": True,
        "data": {
            "truncated": False,
            "scanned": 1,
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


# -- describe_exit_code / the -2 "no message" case ---------------------


def test_describe_exit_code_names_the_signal_for_a_negative_code():
    """A negative `subprocess.returncode` is Python's convention for
    "killed by signal N" (see the `subprocess` docs). Confirmed 2026-09-20:
    a `reolink-cli fetch` in flight when the daemon itself was killed left
    exactly `-2` (SIGINT) as a recording's `last_error`, which used to
    render as the literally unhelpful "exit code -2"."""
    message = describe_exit_code(-2)
    assert message == "reolink-cli was killed by SIGINT (2), most likely by a process restart"
    assert "SIGKILL" in describe_exit_code(-9)


def test_describe_exit_code_handles_an_unknown_signal_number():
    assert "signal 999" in describe_exit_code(-999)


def test_describe_exit_code_for_a_positive_code():
    assert describe_exit_code(1) == "reolink-cli exited with code 1"


def test_provider_uses_describe_exit_code_when_reolink_cli_gives_no_message(provider):
    """No JSON `error.message`, no stderr: the fallback must name the
    signal, not just print the bare negative number."""
    payload = None  # no stdout at all -> envelope = {}
    with (
        patch("subprocess.run", return_value=_completed(payload, returncode=-2)),
        pytest.raises(DeviceError, match="SIGINT"),
    ):
        provider.storage_status()
