"""ReolinkCamera against a stub reolink-cli replaying real D340W output."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
        alias="doorbell", timezone="Europe/Brussels", binary=binary, gateway=_NoGateway()
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
    list_json = (FIXTURES / "device_list.json").read_text()
    script = f"print({list_json!r} if 'list' in sys.argv else {found_json!r})\n"
    found = discover(_stub(tmp_path, script))
    assert found, "the fixture holds at least one camera"
    first = found[0]
    assert first.host and first.model  # from the onvif hardware scope
    registered = {d.host for d in found if d.already_registered}
    assert registered <= {d.host for d in found}


def test_registered_aliases_never_carry_a_password(tmp_path):
    from reovault.camera import registered_aliases

    fixture = (FIXTURES / "device_list.json").read_text()
    aliases = registered_aliases(_stub(tmp_path, f"print({fixture!r})\n"))
    assert aliases and all(isinstance(a, str) for a in aliases)
    assert "password" not in fixture.lower().replace("haspassword", "")
