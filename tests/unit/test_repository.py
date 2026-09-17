from datetime import UTC, datetime

import pytest

from reovault.db.repository import Repository
from reovault.models import ErrorClass, RemoteRecording


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "reovault.db")
    yield r
    r.close()


def _rec(name="20260417_120000.mp4", start=None, size=1024):
    return RemoteRecording(
        remote_name=name,
        start_utc=start or datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=size,
        raw_metadata={"fileName": name},
    )


def test_upsert_device_is_idempotent(repo):
    id1 = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    id2 = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels", model="D350W")
    assert id1 == id2


def test_discover_recording_dedups_on_unique_key(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    rec = _rec()

    id1, created1 = repo.discover_recording(device_id=device_id, channel=0, recording=rec)
    id2, created2 = repo.discover_recording(device_id=device_id, channel=0, recording=rec)

    assert created1 is True
    assert created2 is False
    assert id1 == id2

    row = repo.get(id1)
    assert row.state == "discovered"


def test_discover_recording_absorbs_overlapping_windows(repo):
    """Simulates the archiver re-discovering the same recording from two
    overlapping scan windows, the exact scenario the unique index exists
    for (see plan: Reliability)."""
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    rec = _rec()

    ids = [repo.discover_recording(device_id=device_id, channel=0, recording=rec) for _ in range(3)]
    assert len({i for i, _ in ids}) == 1
    assert sum(1 for _, created in ids if created) == 1


def test_state_machine_full_success_path(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=_rec())

    repo.transition_downloading(rec_id)
    assert repo.get(rec_id).state == "downloading"
    assert repo.get(rec_id).attempts == 1

    repo.transition_verifying(rec_id)
    assert repo.get(rec_id).state == "verifying"

    repo.finalize_archived(
        rec_id,
        vault_path="doorbell/2026/04/17/clip.mp4.enc",
        plaintext_sha256="a" * 64,
        plaintext_size=1024,
        ciphertext_size=1100,
    )
    row = repo.get(rec_id)
    assert row.state == "archived"
    assert row.vault_path == "doorbell/2026/04/17/clip.mp4.enc"
    assert row.plaintext_sha256 == "a" * 64


def test_mark_failed_then_due_for_retry(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=_rec())

    past = datetime(2020, 1, 1, tzinfo=UTC)
    repo.mark_failed(
        rec_id, error="network drop", error_class=ErrorClass.NETWORK, next_attempt_at=past
    )

    due = repo.due_for_retry(now=datetime(2026, 1, 1, tzinfo=UTC))
    assert [r.id for r in due] == [rec_id]

    future = datetime(2099, 1, 1, tzinfo=UTC)
    repo.mark_failed(
        rec_id, error="network drop", error_class=ErrorClass.NETWORK, next_attempt_at=future
    )
    due = repo.due_for_retry(now=datetime(2026, 1, 1, tzinfo=UTC))
    assert due == []


def test_mark_quarantined(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=_rec())
    repo.mark_quarantined(rec_id, error="unrecognized schema", error_class=ErrorClass.PROTOCOL)
    row = repo.get(rec_id)
    assert row.state == "quarantined"


def test_find_by_hash_only_matches_archived(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=_rec())
    assert repo.find_by_hash("b" * 64) is None

    repo.finalize_archived(
        rec_id, vault_path="x", plaintext_sha256="b" * 64, plaintext_size=1, ciphertext_size=2
    )
    found = repo.find_by_hash("b" * 64)
    assert found is not None
    assert found.id == rec_id


def test_archive_run_lifecycle(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    run_id = repo.start_run(
        device_id=device_id,
        trigger="manual",
        window_from_utc=datetime(2026, 4, 17, tzinfo=UTC),
        window_to_utc=datetime(2026, 4, 18, tzinfo=UTC),
    )
    repo.finish_run(
        run_id, outcome="success", discovered=3, downloaded=2, skipped_dup=1, bytes_archived=2048
    )

    row = repo.conn.execute("SELECT * FROM archive_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["outcome"] == "success"
    assert row["downloaded"] == 2
    assert row["finished_at"] is not None


def test_migrate_is_idempotent(tmp_path):
    db_path = tmp_path / "reovault.db"
    r1 = Repository(db_path)
    r1.close()
    r2 = Repository(db_path)  # re-opening re-runs migrate(); must not error
    r2.close()


def test_record_storage_sample(repo):
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="Europe/Brussels")
    repo.record_storage_sample(
        device_id=device_id,
        total_gb=64.0,
        remain_gb=10.0,
        formatted=True,
        mounted=True,
        oldest_recording_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )
    row = repo.conn.execute("SELECT * FROM device_storage_samples").fetchone()
    assert row["total_gb"] == 64.0
