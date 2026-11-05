"""ReolinkCamera against a stub reolink-cli replaying real D340W output."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from reovault.camera import ReolinkCamera
from reovault.tools import ToolError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "reolink_cli"

# The envelope a batch download printed on 2026-10-28 (D340W, reolink-cli
# 0.19.0) when one of three names didn't exist.
BATCH = {
    "ok": True,
    "command": "vod",
    "protocol": "http",
    "data": {
        "requested": 3,
        "downloaded": 2,
        "skipped": [
            {
                "file": "0199990101000000",
                "reason": "backend error: device error: device rejected command 14 (400)",
            }
        ],
        "files": [{"file": "A", "bytes": 3}, {"file": "B", "bytes": 3}],
    },
    "error": None,
}


class _NoGateway:
    def ensure(self) -> str:
        return "127.0.0.1:1"


def _stub(tmp_path: Path, script: str) -> str:
    path = tmp_path / "reolink-cli"
    path.write_text("#!/usr/bin/env python3\nimport json, sys, pathlib\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _camera(binary: str) -> ReolinkCamera:
    return ReolinkCamera(
        id="doorbell",
        timezone="Europe/Brussels",
        host="192.0.2.10",
        user="admin",
        password=SecretStr("pw-never-on-argv"),
        binary=binary,
        gateway=_NoGateway(),
    )


def test_search_converts_camera_local_time_to_utc(tmp_path):
    fixture = (FIXTURES / "vod_search.json").read_text()
    cam = _camera(_stub(tmp_path, f"print({fixture!r})\n"))
    clips = cam.search(datetime(2026, 4, 17, 0, tzinfo=UTC), datetime(2026, 4, 17, 12, tzinfo=UTC))
    first = next(c for c in clips if c.name == "0120260417095327")
    assert first.start_utc == datetime(2026, 4, 17, 7, 53, 27, tzinfo=UTC)  # CEST = UTC+2
    assert (first.size, first.rec_type) == (160336581, "md,people")


def test_a_batch_download_reports_skipped_names_per_file(tmp_path):
    script = (
        "args = sys.argv; d = pathlib.Path(args[args.index('--directory') + 1])\n"
        "d.mkdir(parents=True, exist_ok=True)\n"
        "for n in ('A', 'B'): (d / f'{n}.mp4').write_bytes(b'abc')\n"
        f"print(json.dumps({BATCH!r}))\n"
    )
    got = _camera(_stub(tmp_path, script)).download(
        ["A", "0199990101000000", "B"], tmp_path / "dl", timeout=10
    )
    assert sorted(got.files) == ["A", "B"]
    assert "rejected command 14" in got.failed["0199990101000000"]


def test_a_failed_invocation_blames_no_clip(tmp_path):
    envelope = {"ok": False, "error": {"code": "NETWORK_ERROR", "message": "camera unreachable"}}
    script = f"print(json.dumps({envelope!r})); sys.exit(2)\n"
    with pytest.raises(ToolError, match="unreachable"):
        _camera(_stub(tmp_path, script)).download(["A", "B"], tmp_path / "dl", timeout=10)


def test_a_one_name_device_error_is_that_clips_failure(tmp_path):
    envelope = {"ok": False, "error": {"code": "DEVICE_ERROR", "message": "rejected command 14"}}
    script = f"print(json.dumps({envelope!r})); sys.exit(4)\n"
    got = _camera(_stub(tmp_path, script)).download(["A"], tmp_path / "dl", timeout=10)
    assert "rejected" in got.failed["A"]


def test_storage_reads_the_first_disk(tmp_path):
    fixture = json.loads((FIXTURES / "storage.json").read_text())
    cam = _camera(_stub(tmp_path, f"print(json.dumps({fixture!r}))\n"))
    status = cam.storage()
    assert status.mounted is True
    assert status.total_gb and status.total_gb > 0


def test_the_card_scan_does_not_count_tomorrow_as_an_empty_day():
    """Just before midnight the look-ahead window reaches into tomorrow; it
    must not use up one of the empty days that end the scan."""
    from datetime import timedelta

    from reovault.camera import scan_card
    from tests.fake_camera import FakeCamera

    cam = FakeCamera(timezone="Europe/Brussels")
    now = datetime(2026, 10, 28, 22, 50, tzinfo=UTC)  # 23:50 in Brussels
    old = cam.add(now - timedelta(days=2))
    found, oldest = scan_card(cam, now)
    assert [c.name for c in found] == [old]
    assert oldest == now - timedelta(days=2)


def test_discover_reads_model_and_name_from_scopes(tmp_path):
    from reovault.camera import discover

    found_json = (FIXTURES / "discover.json").read_text()
    found = discover(_stub(tmp_path, f"print({found_json!r})\n"), known_hosts=set())
    assert found, "the fixture holds at least one camera"
    first = found[0]
    assert first.host and first.model  # from the onvif hardware scope
    assert not any(d.added for d in found)
    again = discover(_stub(tmp_path, f"print({found_json!r})\n"), known_hosts={first.host})
    assert again[0].added  # a configured camera's address is marked


def test_the_camera_password_goes_over_stdin_never_argv(tmp_path):
    seen = tmp_path / "seen.json"
    script = (
        f"pathlib.Path({str(seen)!r}).write_text(json.dumps("
        "{'argv': sys.argv, 'stdin': sys.stdin.read()}))\n"
        "print(json.dumps({'ok': True, 'data': {'items': [{'totalGB': 1}]}}))\n"
    )
    _camera(_stub(tmp_path, script)).storage()
    call = json.loads(seen.read_text())
    assert "pw-never-on-argv" not in " ".join(call["argv"])
    assert call["stdin"].strip() == "pw-never-on-argv"
    assert call["argv"][
        call["argv"].index("--host") : call["argv"].index("--password-stdin") + 1
    ] == ["--host", "192.0.2.10", "--user", "admin", "--password-stdin"]


@pytest.mark.parametrize(
    ("script", "unreachable"),
    [
        # reolink-cli exit 2 / NETWORK_ERROR: the camera didn't answer.
        (
            "print(json.dumps({'ok': False, 'error': {'code': 'NETWORK_ERROR', "
            "'message': 'timed out'}})); sys.exit(2)\n",
            True,
        ),
        # A device error is the camera answering "no": not offline.
        (
            "print(json.dumps({'ok': False, 'error': {'code': 'DEVICE_ERROR', "
            "'message': 'rejected'}})); sys.exit(4)\n",
            False,
        ),
        ("print(json.dumps({'ok': False, 'error': {'code': 'AUTH_ERROR'}})); sys.exit(3)\n", False),
    ],
)
def test_only_a_silent_camera_counts_as_unreachable(tmp_path, script, unreachable):
    cam = _camera(_stub(tmp_path, script))
    with pytest.raises(ToolError) as err:
        cam.search(datetime(2026, 4, 17, 0, tzinfo=UTC), datetime(2026, 4, 17, 12, tzinfo=UTC))
    assert err.value.unreachable is unreachable


def test_a_short_call_that_times_out_is_unreachable_a_long_download_is_not(tmp_path, monkeypatch):
    from reovault import camera as camera_mod

    monkeypatch.setattr(camera_mod, "_SHORT_CALL_SECS", 1)  # so "long" is 2 s here, not 60
    cam = _camera(_stub(tmp_path, "import time; time.sleep(30)\n"))
    with pytest.raises(ToolError) as err:
        cam._call(["storage", "status"], timeout=0.5)  # nobody answered a quick question
    assert err.value.unreachable is True
    with pytest.raises(ToolError) as err:
        cam._call(["vod", "download", "x"], timeout=2)  # a big download is just slow
    assert err.value.unreachable is False


def test_an_error_envelope_on_stderr_becomes_its_plain_message(tmp_path):
    script = (
        "sys.stderr.write(json.dumps({'ok': False, 'error': {'code': 'AUTH_ERROR', "
        "'message': 'login failed'}})); sys.exit(3)\n"
    )
    with pytest.raises(ToolError, match="^login failed$"):
        _camera(_stub(tmp_path, script)).storage()


def test_info_reads_model_firmware_and_hardware(tmp_path):
    fixture = (FIXTURES / "device_info.json").read_text()
    info = _camera(_stub(tmp_path, f"print({fixture!r})\n")).info()
    assert (info.model, info.firmware, info.hardware) == (
        "D340W",
        "v3.0.0.6460_2605271708",
        "DB_566128M5MP_W",
    )
