"""Fixture-pinned parsing tests. See plan: Testing #2 and
`tests/fixtures/reolink_cli/README.md`.

These run `ReolinkCliProvider`'s real parsing code against real captured
payloads (with `subprocess.run` mocked to return the fixture's exact bytes).
If a `reolink-cli` upgrade changes the schema, these fail loudly instead of
silently drifting, exactly the alarm the plan calls for given the VOD
surface's documented history of schema changes.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reovault.providers.gateway import GatewaySupervisor
from reovault.providers.reolink_cli import ReolinkCliProvider

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "reolink_cli"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _completed(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


@pytest.fixture
def provider():
    gateway = MagicMock(spec=GatewaySupervisor)
    gateway.addr = "127.0.0.1:9000"
    return ReolinkCliProvider(
        alias="doorbell", timezone="Europe/Brussels", binary="reolink-cli", gateway=gateway
    )


def test_device_info_fixture_parses(provider):
    payload = _load("device_info.json")
    assert payload["data"]["model"] == "D340W"
    assert payload["data"]["name"] == "Front door"
    assert payload["data"]["serial"] == "REDACTED"


def test_storage_status_fixture_parses(provider):
    with patch("subprocess.run", return_value=_completed(_load("storage.json"))):
        status = provider.storage_status()
    assert status.total_gb == pytest.approx(238.7177734375)
    assert status.remain_gb == pytest.approx(191.3505859375)
    assert status.formatted is True
    assert status.mounted is True


def test_vod_search_fixture_parses_all_recordtype_combinations(provider):
    fixture = _load("vod_search.json")
    with patch("subprocess.run", return_value=_completed(fixture)):
        recordings = provider.list_recordings(
            from_utc=datetime(2026, 4, 17, tzinfo=UTC),
            to_utc=datetime(2026, 4, 18, tzinfo=UTC),
        )

    expected = fixture["data"]["items"]
    assert len(recordings) == len(expected)

    by_name = {r.remote_name: r for r in recordings}
    for item in expected:
        rec = by_name[item["name"]]
        assert rec.remote_size == item["fileSize"]
        assert rec.rec_type == item["recordType"]
        assert rec.stream == item["streamType"]
        assert rec.raw_metadata == item

    # The specific behavior called out in the fixture README: a comma-joined
    # multi-type value must survive intact, not just the single-type ones.
    multi_type = [r for r in recordings if "," in (r.rec_type or "")]
    assert len(multi_type) >= 1
    assert any(r.rec_type == "md,people,vehicle,visitor" for r in recordings)


def test_vod_search_fixture_names_have_no_extension(provider):
    """Guards against a regression that assumes remote names always end in
    `.mp4` (the SKILL.md examples do; the real device does not)."""
    fixture = _load("vod_search.json")
    with patch("subprocess.run", return_value=_completed(fixture)):
        recordings = provider.list_recordings(
            from_utc=datetime(2026, 4, 17, tzinfo=UTC),
            to_utc=datetime(2026, 4, 18, tzinfo=UTC),
        )
    assert all(not r.remote_name.endswith(".mp4") for r in recordings)
