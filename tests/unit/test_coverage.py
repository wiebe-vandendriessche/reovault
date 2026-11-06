"""Where a camera's footage is, per place: the SD card, the primary, a copy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reovault.backup import MAX_ATTEMPTS, CameraState
from reovault.config import Settings
from reovault.db import Db
from reovault.status import COVERAGE_BUCKETS, Coverage, coverage, coverage_start

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
DAYS = 7
START = NOW - timedelta(days=DAYS)


def _settings(copy_keep_days: int = 0) -> Settings:
    return Settings.model_validate(
        {
            "cameras": [{"id": "cam", "timezone": "UTC", "demo": True}],
            "destinations": [
                {"name": "local", "repository": "/local", "primary": True},
                {"name": "cloud", "repository": "/cloud", "keep_days": copy_keep_days},
            ],
        }
    )


def _clip(db: Db, ago: timedelta) -> int:
    name = f"01{(NOW - ago):%Y%m%d%H%M%S}"
    db.upsert_clips("cam", [(name, NOW - ago, NOW - ago + timedelta(seconds=30), 10, "md")])
    (clip,) = db.clips_by_name("cam", [name]).values()
    return clip.id


def _backed_up(db: Db, ago: timedelta, snapshot: str, *, on_cloud: bool) -> None:
    cid = _clip(db, ago)
    db.mark_backed_up("cam", "local", snapshot, NOW - ago, {cid: f"/b/{cid}.mp4"})
    if on_cloud:
        held = {(r["root"], r["snapshot_id"]) for r in db.snapshot_usage("cloud")}
        rows = [(root, sid, NOW) for root, sid in held] + [(snapshot, f"c-{snapshot}", NOW)]
        db.set_locations("cam", "cloud", rows)


def _at(cov: Coverage, row: str, ago: timedelta) -> str:
    """The state of `row` at the moment `ago` before now."""
    i = int((cov.end - ago - cov.start) / ((cov.end - cov.start) / cov.buckets))
    (r,) = [r for r in cov.rows if r.name == row]
    (run,) = [x for x in r.runs if x.start <= i < x.start + x.length]
    return run.state


@pytest.fixture
def db(tmp_path):
    return Db(tmp_path / "x.db")


def test_each_row_says_where_the_footage_is(db):
    card = CameraState(oldest_on_card=NOW - timedelta(days=3))
    _backed_up(db, timedelta(days=5), "s-old", on_cloud=False)  # before the card's window
    _backed_up(db, timedelta(days=2), "s-mid", on_cloud=False)  # the copy is behind
    _backed_up(db, timedelta(hours=12), "s-new", on_cloud=True)  # everywhere
    _clip(db, timedelta(days=1))  # still waiting on the card
    failing = _clip(db, timedelta(hours=30))
    for _ in range(MAX_ATTEMPTS):
        db.record_failure(failing, "device rejected command")
    db.mark_lost(_clip(db, timedelta(days=4)))  # overwritten before backup

    cov = coverage("cam", db, _settings(), card, START, NOW)
    assert [r.name for r in cov.rows] == ["SD card", "local", "cloud"]
    assert cov.buckets == COVERAGE_BUCKETS
    assert cov.rows[0].marker == pytest.approx(4 / 7 * 100)

    # The card holds only its own window; losses sit just before it.
    assert _at(cov, "SD card", timedelta(days=5)) == "none"
    assert _at(cov, "SD card", timedelta(days=4)) == "lost"
    assert _at(cov, "SD card", timedelta(days=2)) == "safe"
    assert _at(cov, "SD card", timedelta(days=1)) == "waiting"
    assert _at(cov, "SD card", timedelta(hours=30)) == "failing"

    # The primary has what was backed up; what is still on the card is coming.
    assert _at(cov, "local", timedelta(days=5)) == "safe"
    assert _at(cov, "local", timedelta(days=4)) == "none"  # lost: shown on the card row only
    assert _at(cov, "local", timedelta(days=1)) == "waiting"
    assert _at(cov, "local", timedelta(hours=30)) == "waiting"

    # The copy wants everything (keep 0 days) but only has the newest snapshot.
    assert _at(cov, "cloud", timedelta(days=5)) == "waiting"
    assert _at(cov, "cloud", timedelta(days=2)) == "waiting"
    assert _at(cov, "cloud", timedelta(hours=12)) == "safe"


def test_a_copy_only_waits_for_what_its_keep_days_want(db):
    _backed_up(db, timedelta(days=5), "s-old", on_cloud=False)
    cov = coverage("cam", db, _settings(copy_keep_days=2), None, START, NOW)
    assert _at(cov, "cloud", timedelta(days=5)) == "none"  # older than it keeps: not owed


def test_neighbouring_slices_merge_and_the_runs_cover_the_axis(db):
    for h in range(1, 48):
        _clip(db, timedelta(hours=h))
    card = CameraState(oldest_on_card=NOW - timedelta(days=2))
    for days in (1, 7, 14, 30):
        cov = coverage("cam", db, _settings(), card, NOW - timedelta(days=days), NOW)
        for row in cov.rows:
            assert sum(r.length for r in row.runs) == COVERAGE_BUCKETS
            assert all(a.state != b.state for a, b in zip(row.runs, row.runs[1:], strict=False))


def test_without_a_card_scan_the_card_row_is_empty(db):
    _clip(db, timedelta(hours=3))
    cov = coverage("cam", db, _settings(), CameraState(), START, NOW)
    assert {r.state for r in cov.rows[0].runs} == {"none"}
    assert cov.rows[0].marker is None


def test_the_span_can_follow_the_card_everything_or_a_number_of_days(db):
    card = CameraState(oldest_on_card=NOW - timedelta(days=12))
    assert coverage_start("card", "cam", db, card, NOW) == NOW - timedelta(days=12)
    assert coverage_start("90", "cam", db, card, NOW) == NOW - timedelta(days=90)
    _clip(db, timedelta(days=400))  # a long-kept clip
    assert coverage_start("all", "cam", db, card, NOW) == NOW - timedelta(days=400)
    # Before the first card scan, the card span falls back to a week.
    assert coverage_start("card", "cam", db, CameraState(), NOW) == NOW - timedelta(days=7)
