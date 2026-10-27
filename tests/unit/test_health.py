"""Coverage/lag alarm, and Scheduling's `storage_sample`/`integrity_scan`
rows."""

import os
from datetime import UTC, datetime, timedelta

import pytest
import structlog

from reovault.db.repository import Repository, parse_iso_utc
from reovault.health import (
    card_status,
    record_storage_sample,
    run_integrity_scan,
)
from reovault.models import ErrorClass, RemoteRecording
from reovault.providers.fake import FakeProvider, ScriptedRecording
from reovault.storage.vault import EncryptedFsVault
from tests.unit.conftest import make_recording


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "reovault.db")
    yield r
    r.close()


@pytest.fixture
def device_id(repo):
    return repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")


def _rec(name: str, start: datetime, size: int = 10) -> RemoteRecording:
    return make_recording(name, start, size=size, raw_metadata={})


class _WindowCappedProvider(FakeProvider):
    """Fails any search wider than a day, the way a busy camera outruns
    reolink-cli's 60s gateway budget on a long window (issue #13)."""

    def list_recordings(self, *, from_utc, to_utc):
        assert to_utc - from_utc <= timedelta(days=1), "search window too wide"
        return super().list_recordings(from_utc=from_utc, to_utc=to_utc)


def test_record_storage_sample_walks_back_to_the_oldest_day(repo, device_id):
    now = datetime.now(UTC)
    oldest = now - timedelta(days=7, hours=5)
    provider = _WindowCappedProvider(
        recordings=[
            ScriptedRecording(_rec(f"r{d}", now - timedelta(days=d, hours=5)), b"x")
            for d in range(8)
        ]
    )
    record_storage_sample(repository=repo, provider=provider, device_id=device_id)

    sample = repo.latest_storage_sample(device_id)
    assert sample is not None
    assert sample.total_gb == 64.0  # FakeProvider's default storage status
    assert parse_iso_utc(sample.oldest_recording_utc) == oldest
    # 8 days with clips, then EMPTY_DAYS_TO_STOP empty ones, then stop:
    # nowhere near the 3650 a fixed 10-year lookback would take.
    assert provider.list_calls == 8 + 3


def test_record_storage_sample_handles_empty_card(repo, device_id):
    provider = FakeProvider(recordings=[])
    record_storage_sample(repository=repo, provider=provider, device_id=device_id)
    sample = repo.latest_storage_sample(device_id)
    assert sample is not None
    assert sample.oldest_recording_utc is None


def _sample(repo, device_id, *, oldest_on_card, remain_gb=0.0, mounted=True):
    repo.record_storage_sample(
        device_id=device_id,
        total_gb=1.0,
        remain_gb=remain_gb,
        formatted=True,
        mounted=mounted,
        oldest_recording_utc=oldest_on_card,
    )


def _quarantine(repo, device_id, name, start):
    rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=_rec(name, start))
    repo.mark_quarantined(rec_id, error="test", error_class=ErrorClass.PROTOCOL)
    return rec_id


def test_card_status_unknown_for_a_fresh_camera(repo, device_id):
    status = card_status(repository=repo, device_id=device_id)
    assert status.state == "unknown"
    assert status.alarm is False


def test_card_status_all_archived_when_caught_up(repo, device_id):
    _sample(repo, device_id, oldest_on_card=datetime.now(UTC) - timedelta(days=10))
    status = card_status(repository=repo, device_id=device_id)
    assert status.state == "all_archived"
    assert status.safe_for is None


def test_card_status_alarms_when_backlog_close_to_horizon(repo, device_id):
    now = datetime.now(UTC)
    oldest_on_card = now - timedelta(days=10)
    _sample(repo, device_id, oldest_on_card=oldest_on_card)
    # Our oldest unarchived clip is 1 day newer than the card's horizon on a
    # full, 10-day card: it's overwritten in ~1 day, under the 20% margin.
    _quarantine(repo, device_id, "stuck", oldest_on_card + timedelta(days=1))

    status = card_status(repository=repo, device_id=device_id, now=now)
    assert status.state == "at_risk"
    assert status.alarm is True
    assert status.waiting_count == 1
    assert status.safe_for == timedelta(days=1)


def test_card_status_waiting_with_headroom(repo, device_id):
    now = datetime.now(UTC)
    oldest_on_card = now - timedelta(days=10)
    _sample(repo, device_id, oldest_on_card=oldest_on_card)
    repo.discover_recording(
        device_id=device_id, channel=0, recording=_rec("fine", oldest_on_card + timedelta(days=9))
    )
    status = card_status(repository=repo, device_id=device_id, now=now)
    assert status.state == "waiting"
    assert status.safe_for == timedelta(days=9)


def test_card_status_no_deadline_on_a_card_that_isnt_full(repo, device_id):
    """A card with room left isn't overwriting anything yet."""
    oldest_on_card = datetime.now(UTC) - timedelta(days=10)
    _sample(repo, device_id, oldest_on_card=oldest_on_card, remain_gb=0.5)
    _quarantine(repo, device_id, "stuck", oldest_on_card + timedelta(days=1))
    status = card_status(repository=repo, device_id=device_id)
    assert status.state == "waiting"
    assert status.safe_for is None


def test_card_status_counts_overwritten_clips_as_lost_not_alarm(repo, device_id):
    """Regression: a quarantined clip the card already overwrote used to pin
    the coverage margin at 0% and alarm forever."""
    oldest_on_card = datetime.now(UTC) - timedelta(days=10)
    _sample(repo, device_id, oldest_on_card=oldest_on_card)
    _quarantine(repo, device_id, "gone", oldest_on_card - timedelta(days=2))
    status = card_status(repository=repo, device_id=device_id)
    assert status.state == "all_archived"
    assert status.alarm is False
    assert status.lost_count == 1
    assert status.waiting_count == 0


def test_mark_lost_moves_only_unarchived_clips_older_than_the_card(repo, device_id):
    boundary = datetime(2026, 10, 10, tzinfo=UTC)
    before, after = boundary - timedelta(hours=1), boundary + timedelta(hours=1)
    queued, _ = repo.discover_recording(device_id=device_id, channel=0, recording=_rec("q", before))
    quarantined = _quarantine(repo, device_id, "x", before)
    in_flight, _ = repo.discover_recording(
        device_id=device_id, channel=0, recording=_rec("d", before)
    )
    repo.transition_downloading(in_flight)
    archived, _ = repo.discover_recording(
        device_id=device_id, channel=0, recording=_rec("a", before)
    )
    repo.finalize_archived(
        archived, vault_path="a.enc", plaintext_sha256="0", plaintext_size=1, ciphertext_size=1
    )
    still_on_card = _quarantine(repo, device_id, "s", after)

    assert repo.mark_lost(device_id, boundary) == 2

    states = {
        i: repo.get(i).state for i in (queued, quarantined, in_flight, archived, still_on_card)
    }
    assert states == {
        queued: "lost",
        quarantined: "lost",
        in_flight: "downloading",
        archived: "archived",
        still_on_card: "quarantined",
    }
    assert repo.retry_recording(queued) is False  # nothing to retry, the card overwrote it
    assert repo.totals(device_id).quarantined_count == 1  # lost clips leave Problems


def test_lost_clips_count_until_dismissed(repo, device_id):
    oldest_on_card = datetime.now(UTC) - timedelta(days=10)
    _sample(repo, device_id, oldest_on_card=oldest_on_card)
    _quarantine(repo, device_id, "gone", oldest_on_card - timedelta(days=2))
    repo.mark_lost(device_id, oldest_on_card)
    assert card_status(repository=repo, device_id=device_id).lost_count == 1
    assert repo.lost_summary(device_id)[0] == 1

    assert repo.dismiss_lost(device_id) == 1

    assert card_status(repository=repo, device_id=device_id).lost_count == 0
    assert repo.lost_summary(device_id) == (0, None, None)


def test_a_lost_clip_the_camera_lists_again_is_queued_again(repo, device_id):
    """The oldest-clip scan stops at a gap in recording, so `mark_lost` can
    be wrong. A clip the camera still lists must never stay lost."""
    start = datetime(2026, 10, 1, tzinfo=UTC)
    rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=_rec("r", start))
    repo.mark_lost(device_id, start + timedelta(days=1))
    repo.dismiss_lost(device_id)

    again, created = repo.discover_recording(
        device_id=device_id, channel=0, recording=_rec("r", start)
    )

    assert (again, created) == (rec_id, False)
    assert repo.get(rec_id).state == "discovered"
    assert [r.id for r in repo.pending(device_id=device_id)] == [rec_id]


def test_record_storage_sample_marks_overwritten_clips_lost(repo, device_id):
    now = datetime.now(UTC)
    gone = _quarantine(repo, device_id, "gone", now - timedelta(days=5))
    provider = FakeProvider(
        recordings=[ScriptedRecording(_rec("r", now - timedelta(hours=2)), b"x")]
    )

    record_storage_sample(repository=repo, provider=provider, device_id=device_id)

    assert repo.get(gone).state == "lost"


def test_card_status_estimates_window_without_oldest_clip(repo, device_id):
    now = datetime.now(UTC)
    _sample(repo, device_id, oldest_on_card=None)
    # 2**30 bytes/day recorded over the last week on a 1 GiB card: ~1 day.
    for d in range(7):
        rec = make_recording(f"r{d}", now - timedelta(days=d, hours=1), size=2**30, raw_metadata={})
        repo.discover_recording(device_id=device_id, channel=0, recording=rec)
    status = card_status(repository=repo, device_id=device_id, now=now)
    assert status.window_estimated is True
    assert status.window == timedelta(days=1)


def test_card_status_not_mounted(repo, device_id):
    _sample(repo, device_id, oldest_on_card=None, mounted=False)
    assert card_status(repository=repo, device_id=device_id).state == "not_mounted"


def test_card_timeline_buckets_by_state(repo, device_id):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    archived, _ = repo.discover_recording(
        device_id=device_id, channel=0, recording=_rec("a", start + timedelta(minutes=10))
    )
    repo.finalize_archived(
        archived, vault_path="x.enc", plaintext_sha256="0", plaintext_size=1, ciphertext_size=1
    )
    repo.discover_recording(
        device_id=device_id, channel=0, recording=_rec("w", start + timedelta(hours=3))
    )
    _quarantine(repo, device_id, "q", start + timedelta(hours=3, minutes=59))

    rows = repo.card_timeline(
        device_id=device_id, from_utc=start, to_utc=start + timedelta(hours=4), buckets=4
    )
    assert [(r.archived, r.waiting, r.quarantined) for r in rows] == [
        (1, 0, 0),
        (0, 0, 0),
        (0, 0, 0),
        (0, 1, 1),
    ]
    assert rows[1].start_utc == start + timedelta(hours=1)


def test_coverage_alarm_logs_on_state_change_not_every_poll(repo, device_id, monkeypatch):
    monkeypatch.setattr("reovault.health._logged_alarm", {})
    oldest_on_card = datetime.now(UTC) - timedelta(days=10)
    _sample(repo, device_id, oldest_on_card=oldest_on_card)
    rec_id = _quarantine(repo, device_id, "stuck", oldest_on_card + timedelta(days=1))

    with structlog.testing.capture_logs() as logs:
        for _ in range(3):  # dashboard/healthz polling
            assert card_status(repository=repo, device_id=device_id).alarm is True
        repo.finalize_archived(
            rec_id, vault_path="x.enc", plaintext_sha256="0", plaintext_size=1, ciphertext_size=1
        )
        assert card_status(repository=repo, device_id=device_id).alarm is False
        card_status(repository=repo, device_id=device_id)

    assert [e["event"] for e in logs] == [
        "health.coverage_alarm",
        "health.coverage_alarm_cleared",
    ]


def test_integrity_scan_checks_a_rotating_slice(repo, device_id, tmp_path):
    vault = EncryptedFsVault(tmp_path / "vault", os.urandom(32), frame_size=16)
    now = datetime.now(UTC)
    ids = []
    for i in range(20):
        staging = tmp_path / f"staging-{i}.mp4"
        staging.write_bytes(f"content-{i}".encode())
        result = vault.put(
            staging, device_alias="doorbell", start_utc=now, remote_name=f"clip{i}.mp4"
        )
        rec = _rec(f"clip{i}.mp4", now)
        rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=rec)
        repo.finalize_archived(
            rec_id,
            vault_path=result.vault_path,
            plaintext_sha256=result.plaintext_sha256,
            plaintext_size=result.plaintext_size,
            ciphertext_size=result.ciphertext_size,
        )
        ids.append(rec_id)

    # sample_pct=5 -> 20 buckets -> exactly one id per bucket, given ids 1..20.
    seen = set()
    for bucket in range(20):
        result = run_integrity_scan(
            repository=repo, vault=vault, device_id=device_id, sample_pct=5.0, bucket_index=bucket
        )
        assert result.failed == []
        seen.update(result.checked)
    assert seen == set(ids)


def test_integrity_scan_detects_a_tampered_file(repo, device_id, tmp_path):
    vault = EncryptedFsVault(tmp_path / "vault", os.urandom(32), frame_size=16)
    now = datetime.now(UTC)
    staging = tmp_path / "staging.mp4"
    staging.write_bytes(b"real content")
    result = vault.put(staging, device_alias="doorbell", start_utc=now, remote_name="clip.mp4")
    rec = _rec("clip.mp4", now)
    rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=rec)
    repo.finalize_archived(
        rec_id,
        vault_path=result.vault_path,
        plaintext_sha256=result.plaintext_sha256,
        plaintext_size=result.plaintext_size,
        ciphertext_size=result.ciphertext_size,
    )

    ciphertext_path = vault.vault_dir / result.vault_path
    data = bytearray(ciphertext_path.read_bytes())
    data[-1] ^= 0xFF
    ciphertext_path.write_bytes(bytes(data))

    scan = run_integrity_scan(
        repository=repo, vault=vault, device_id=device_id, bucket_index=rec_id % 20
    )
    assert rec_id in scan.checked
    assert rec_id in scan.failed
