"""Manual job queueing for the dashboard's controls. Every click gets its
own job id, so a second run queued while the first is still going waits
its turn instead of being dropped."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from reovault.config import ScheduleConfig
from reovault.providers.fake import FakeProvider
from reovault.scheduler import (
    MANUAL_ARCHIVE_JOB_BASE,
    MANUAL_BACKFILL_JOB_BASE,
    MANUAL_RECONCILE_JOB_BASE,
    add_device_jobs,
    build_scheduler,
    device_of_job_id,
    next_run_times,
    queue_manual_archive,
    queue_manual_backfill,
    queue_manual_reconcile,
    queued_manual_jobs,
)


@pytest.fixture
def scheduler():
    s = BackgroundScheduler(timezone=UTC)
    yield s
    if s.running:
        s.shutdown(wait=False)


@pytest.mark.parametrize(
    ("queue", "base"),
    [
        (
            lambda s, a, now: queue_manual_archive(s, a, from_utc=now, to_utc=now),
            MANUAL_ARCHIVE_JOB_BASE,
        ),
        (
            lambda s, a, now: queue_manual_backfill(s, a, from_utc=now, to_utc=now),
            MANUAL_BACKFILL_JOB_BASE,
        ),
        (lambda s, a, now: queue_manual_reconcile(s, a), MANUAL_RECONCILE_JOB_BASE),
    ],
)
def test_queue_manual_job_ids_are_unique_and_keep_the_device(env, scheduler, queue, base):
    archiver = env.archiver(FakeProvider())
    scheduler.start(paused=True)
    now = datetime.now(UTC)
    before = queued_manual_jobs(env.device_id)
    first = queue(scheduler, archiver, now)
    second = queue(scheduler, archiver, now)  # must not raise ConflictingIdError
    assert first != second
    for job_id in (first, second):
        assert job_id.startswith(base + "-")
        assert device_of_job_id(job_id) == env.device_id
        assert scheduler.get_job(job_id) is not None
    assert queued_manual_jobs(env.device_id) == before + 2


def test_a_run_queued_during_another_still_runs(env):
    """The deployed bug: a second click while a run was in progress was
    silently dropped. Both must end up as finished runs, one after the
    other on the single worker."""
    archiver = env.archiver(FakeProvider())
    scheduler = build_scheduler()
    scheduler.start()
    try:
        now = datetime.now(UTC)
        before = queued_manual_jobs(env.device_id)
        queue_manual_backfill(scheduler, archiver, from_utc=now - timedelta(days=1), to_utc=now)
        queue_manual_archive(scheduler, archiver, from_utc=now - timedelta(hours=1), to_utc=now)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            runs = env.repository.recent_runs(device_id=env.device_id, limit=10)
            if len(runs) == 2 and all(r.finished_at for r in runs):
                break
            time.sleep(0.05)
        runs = env.repository.recent_runs(device_id=env.device_id, limit=10)
        assert sorted(r.trigger for r in runs) == ["backfill", "manual"]
        assert queued_manual_jobs(env.device_id) == before
    finally:
        scheduler.shutdown(wait=True)


def test_next_run_times_returns_all_four_scheduled_jobs_for_the_device(env):
    archiver = env.archiver(FakeProvider())
    scheduler = build_scheduler()
    add_device_jobs(scheduler, archiver, ScheduleConfig(), device_timezone="Europe/Brussels")
    scheduler.start(paused=True)
    times = next_run_times(scheduler)
    assert set(times.keys()) == {env.device_id}
    assert set(times[env.device_id].keys()) == {
        "scheduled_archive",
        "deep_backfill",
        "reconcile",
        "integrity_scan",
    }
    assert all(v is not None for v in times[env.device_id].values())
    scheduler.shutdown(wait=False)


def test_next_run_times_before_scheduler_start_is_none_not_a_crash(env):
    """A job added while stopped has no next_run_time slot set at all
    (see reovault/scheduler.py); next_run_times must degrade to None rather
    than raising AttributeError."""
    archiver = env.archiver(FakeProvider())
    scheduler = build_scheduler()
    add_device_jobs(scheduler, archiver, ScheduleConfig(), device_timezone="Europe/Brussels")
    times = next_run_times(scheduler)
    assert all(v is None for v in times[env.device_id].values())


def test_next_run_times_grouped_by_device(env):
    """Two devices' jobs group under two separate keys."""
    archiver_a = env.archiver(FakeProvider())
    archiver_b, device_b_id = env.archiver_for("cam-b", FakeProvider())
    scheduler = build_scheduler()
    add_device_jobs(scheduler, archiver_a, ScheduleConfig(), device_timezone="Europe/Brussels")
    add_device_jobs(scheduler, archiver_b, ScheduleConfig(), device_timezone="Europe/Brussels")
    scheduler.start(paused=True)
    times = next_run_times(scheduler)
    assert set(times.keys()) == {env.device_id, device_b_id}
    scheduler.shutdown(wait=False)


def test_next_run_times_empty_for_an_empty_scheduler():
    scheduler = BackgroundScheduler(timezone=UTC)
    assert next_run_times(scheduler) == {}
