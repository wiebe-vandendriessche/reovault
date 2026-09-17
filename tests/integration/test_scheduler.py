"""See plan: Scheduling. Tests the job callables directly (calling them
synchronously) rather than waiting on real APScheduler timing, that
library's own cron/interval trigger math isn't ours to re-test."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from reovault.archiver import AlreadyRunningError, instance_lock
from reovault.config import ScheduleConfig
from reovault.models import RemoteRecording
from reovault.providers.fake import FakeProvider, ScriptedRecording
from reovault.scheduler import (
    _deep_backfill_job,
    _integrity_scan_job,
    _reconcile_job,
    _run_and_sample,
    _scheduled_archive_job,
    build_scheduler,
)


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


def test_build_scheduler_registers_all_four_jobs(env):
    archiver = env.archiver(FakeProvider())
    schedule = ScheduleConfig()
    scheduler = build_scheduler(archiver, schedule)
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"scheduled_archive", "deep_backfill", "reconcile", "integrity_scan"}


def test_run_and_sample_archives_and_records_storage(env):
    content = b"clip bytes"
    rec = _rec("clip1", datetime(2026, 4, 17, 12, tzinfo=UTC), len(content))
    provider = FakeProvider(recordings=[ScriptedRecording(rec, content)])
    archiver = env.archiver(provider)

    _run_and_sample(
        archiver,
        trigger="schedule",
        from_utc=datetime(2026, 4, 17, tzinfo=UTC),
        to_utc=datetime(2026, 4, 18, tzinfo=UTC),
    )

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "archived"
    sample = env.repository.latest_storage_sample(env.device_id)
    assert sample is not None


def test_run_and_sample_skips_storage_sample_when_run_is_already_running(env):
    archiver = env.archiver(FakeProvider())
    with (
        instance_lock(env.lock_path),
        patch("reovault.scheduler.health.record_storage_sample") as sample_fn,
    ):
        _run_and_sample(
            archiver,
            trigger="schedule",
            from_utc=datetime(2026, 4, 17, tzinfo=UTC),
            to_utc=datetime(2026, 4, 18, tzinfo=UTC),
        )
        sample_fn.assert_not_called()


def test_run_and_sample_tolerates_storage_sample_failure(env):
    provider = FakeProvider()
    archiver = env.archiver(provider)
    with patch("reovault.scheduler.health.record_storage_sample", side_effect=RuntimeError("boom")):
        _run_and_sample(
            archiver,
            trigger="schedule",
            from_utc=datetime(2026, 4, 17, tzinfo=UTC),
            to_utc=datetime(2026, 4, 18, tzinfo=UTC),
        )  # must not raise


def test_scheduled_archive_job_uses_overlap_from_now_with_no_prior_run(env):
    archiver = env.archiver(FakeProvider())
    with patch("reovault.scheduler._run_and_sample") as run_fn:
        _scheduled_archive_job(archiver, overlap_hours=48)
        _, kwargs = run_fn.call_args
        assert kwargs["trigger"] == "schedule"
        span = kwargs["to_utc"] - kwargs["from_utc"]
        assert timedelta(hours=47) < span < timedelta(hours=49)


def test_scheduled_archive_job_anchors_to_last_successful_run(env):
    """`last_successful_run_started_at` is when the previous run was
    *invoked* (`started_at`), not the scan window it covered, per plan:
    "last successful run start − 48h overlap"."""
    archiver = env.archiver(FakeProvider())
    run_id = env.repository.start_run(
        device_id=env.device_id,
        trigger="schedule",
        window_from_utc=datetime(2026, 4, 10, tzinfo=UTC),
        window_to_utc=datetime(2026, 4, 11, tzinfo=UTC),
    )
    env.repository.finish_run(run_id, outcome="success")
    last_start = env.repository.last_successful_run_started_at(env.device_id)
    assert last_start is not None

    with patch("reovault.scheduler._run_and_sample") as run_fn:
        _scheduled_archive_job(archiver, overlap_hours=48)
        _, kwargs = run_fn.call_args
        assert kwargs["from_utc"] == last_start - timedelta(hours=48)


def test_deep_backfill_job_uses_trailing_days_window(env):
    archiver = env.archiver(FakeProvider())
    with patch("reovault.scheduler._run_and_sample") as run_fn:
        _deep_backfill_job(archiver, days=30)
        _, kwargs = run_fn.call_args
        assert kwargs["trigger"] == "backfill"
        span = kwargs["to_utc"] - kwargs["from_utc"]
        assert timedelta(days=29) < span < timedelta(days=31)


def test_reconcile_job_calls_archiver_reconcile(env):
    archiver = env.archiver(FakeProvider())
    with patch.object(archiver, "reconcile") as reconcile_fn:
        _reconcile_job(archiver)
        reconcile_fn.assert_called_once()


def test_reconcile_job_tolerates_already_running(env):
    archiver = env.archiver(FakeProvider())
    with patch.object(archiver, "reconcile", side_effect=AlreadyRunningError("busy")):
        _reconcile_job(archiver)  # must not raise


def test_integrity_scan_job_calls_health_run_integrity_scan(env):
    archiver = env.archiver(FakeProvider())
    with patch("reovault.scheduler.health.run_integrity_scan") as scan_fn:
        scan_fn.return_value.checked = []
        scan_fn.return_value.failed = []
        _integrity_scan_job(archiver, sample_pct=5.0)
        scan_fn.assert_called_once_with(
            repository=archiver.repository,
            vault=archiver.vault,
            device_id=archiver.device_id,
            sample_pct=5.0,
        )
