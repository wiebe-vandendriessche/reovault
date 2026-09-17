"""See plan: Coverage / lag alarm, and Scheduling's `storage_sample`/
`integrity_scan` rows."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from reovault.db.repository import Repository
from reovault.health import (
    coverage_margin,
    record_storage_sample,
    run_integrity_scan,
)
from reovault.models import ErrorClass, RemoteRecording
from reovault.providers.fake import FakeProvider, ScriptedRecording
from reovault.storage.vault import EncryptedFsVault


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "reovault.db")
    yield r
    r.close()


@pytest.fixture
def device_id(repo):
    return repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")


def _rec(name: str, start: datetime, size: int = 10) -> RemoteRecording:
    return RemoteRecording(
        remote_name=name,
        start_utc=start,
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=size,
        raw_metadata={},
    )


def test_record_storage_sample_uses_full_search_for_oldest(repo, device_id):
    now = datetime.now(UTC)
    provider = FakeProvider(
        recordings=[
            ScriptedRecording(_rec("old", now - timedelta(days=10)), b"x"),
            ScriptedRecording(_rec("new", now - timedelta(days=1)), b"x"),
        ]
    )
    record_storage_sample(repository=repo, provider=provider, device_id=device_id)

    sample = repo.latest_storage_sample(device_id)
    assert sample is not None
    assert sample.total_gb == 64.0  # FakeProvider's default storage status
    assert sample.oldest_recording_utc is not None


def test_record_storage_sample_handles_empty_card(repo, device_id):
    provider = FakeProvider(recordings=[])
    record_storage_sample(repository=repo, provider=provider, device_id=device_id)
    sample = repo.latest_storage_sample(device_id)
    assert sample is not None
    assert sample.oldest_recording_utc is None


def test_coverage_margin_no_alarm_with_no_sample(repo, device_id):
    status = coverage_margin(repository=repo, device_id=device_id)
    assert status.alarm is False
    assert status.margin_fraction is None


def test_coverage_margin_no_alarm_when_fully_caught_up(repo, device_id):
    now = datetime.now(UTC)
    repo.record_storage_sample(
        device_id=device_id,
        total_gb=1.0,
        remain_gb=1.0,
        formatted=True,
        mounted=True,
        oldest_recording_utc=now - timedelta(days=10),
    )
    # No unarchived recordings at all: nothing to be behind on.
    status = coverage_margin(repository=repo, device_id=device_id)
    assert status.alarm is False
    assert status.margin_fraction is None


def test_coverage_margin_alarms_when_backlog_close_to_horizon(repo, device_id):
    now = datetime.now(UTC)
    oldest_on_card = now - timedelta(days=10)
    repo.record_storage_sample(
        device_id=device_id,
        total_gb=1.0,
        remain_gb=1.0,
        formatted=True,
        mounted=True,
        oldest_recording_utc=oldest_on_card,
    )
    # Our oldest unarchived recording is only 1 day newer than the card's
    # own horizon, out of a 10-day coverage span: well under the 20% margin.
    rec = _rec("stuck", oldest_on_card + timedelta(days=1))
    rec_id, _ = repo.discover_recording(device_id=device_id, channel=0, recording=rec)
    repo.mark_quarantined(rec_id, error="test", error_class=ErrorClass.PROTOCOL)

    status = coverage_margin(repository=repo, device_id=device_id)
    assert status.alarm is True
    assert status.margin_fraction is not None
    assert status.margin_fraction < 0.20


def test_coverage_margin_no_alarm_when_backlog_has_headroom(repo, device_id):
    now = datetime.now(UTC)
    oldest_on_card = now - timedelta(days=10)
    repo.record_storage_sample(
        device_id=device_id,
        total_gb=1.0,
        remain_gb=1.0,
        formatted=True,
        mounted=True,
        oldest_recording_utc=oldest_on_card,
    )
    # Backlog is 9 of the 10-day span away from the horizon: plenty of margin.
    rec = _rec("fine", oldest_on_card + timedelta(days=9))
    repo.discover_recording(device_id=device_id, channel=0, recording=rec)

    status = coverage_margin(repository=repo, device_id=device_id)
    assert status.alarm is False
    assert status.margin_fraction > 0.20


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
