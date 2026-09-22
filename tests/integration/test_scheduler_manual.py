"""Manual job queueing for the dashboard's controls. The scheduler is
deliberately never started in these tests: a fixed job id with
`replace_existing=False` must raise on a second click regardless of whether
the first has run yet."""

from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.schedulers.background import BackgroundScheduler

from reovault.config import ScheduleConfig
from reovault.providers.fake import FakeProvider
from reovault.scheduler import (
    MANUAL_ARCHIVE_JOB_BASE,
    MANUAL_BACKFILL_JOB_BASE,
    MANUAL_RECONCILE_JOB_BASE,
    add_device_jobs,
    build_scheduler,
    next_run_times,
    queue_manual_archive,
    queue_manual_backfill,
    queue_manual_reconcile,
)


@pytest.fixture
def scheduler():
    s = BackgroundScheduler(timezone=UTC)
    yield s
    if s.running:
        s.shutdown(wait=False)


def test_queue_manual_archive_registers_job_with_per_device_id(env, scheduler):
    archiver = env.archiver(FakeProvider())
    now = datetime.now(UTC)
    job_id = queue_manual_archive(
        scheduler, archiver, from_utc=now - timedelta(hours=1), to_utc=now
    )
    expected_id = f"{MANUAL_ARCHIVE_JOB_BASE}:{env.device_id}"
    assert job_id == expected_id
    assert scheduler.get_job(expected_id) is not None


def test_queue_manual_archive_twice_raises_conflicting_id(env, scheduler):
    """APScheduler only enforces id conflicts against jobs actually in the
    jobstore, which only happens once the scheduler is running (jobs added
    while stopped are merely 'pending'). The real daemon always starts the
    scheduler before the web app can receive a request, so this test does
    the same to exercise the real guard."""
    archiver = env.archiver(FakeProvider())
    scheduler.start(paused=True)
    now = datetime.now(UTC)
    queue_manual_archive(scheduler, archiver, from_utc=now - timedelta(hours=1), to_utc=now)
    with pytest.raises(ConflictingIdError):
        queue_manual_archive(scheduler, archiver, from_utc=now - timedelta(hours=1), to_utc=now)


def test_queue_manual_archive_on_two_devices_never_conflicts(env, scheduler):
    """The whole reason the id gained a device suffix: two cameras queueing
    a manual run at the same moment must not collide."""
    archiver_a = env.archiver(FakeProvider())
    archiver_b, _device_b_id = env.archiver_for("cam-b", FakeProvider())
    scheduler.start(paused=True)
    now = datetime.now(UTC)
    queue_manual_archive(scheduler, archiver_a, from_utc=now - timedelta(hours=1), to_utc=now)
    # Must NOT raise: different device, different job id.
    queue_manual_archive(scheduler, archiver_b, from_utc=now - timedelta(hours=1), to_utc=now)


def test_queue_manual_backfill_registers_job(env, scheduler):
    archiver = env.archiver(FakeProvider())
    now = datetime.now(UTC)
    job_id = queue_manual_backfill(
        scheduler, archiver, from_utc=now - timedelta(days=1), to_utc=now
    )
    expected_id = f"{MANUAL_BACKFILL_JOB_BASE}:{env.device_id}"
    assert job_id == expected_id
    assert scheduler.get_job(expected_id) is not None


def test_queue_manual_reconcile_registers_job(env, scheduler):
    archiver = env.archiver(FakeProvider())
    job_id = queue_manual_reconcile(scheduler, archiver)
    expected_id = f"{MANUAL_RECONCILE_JOB_BASE}:{env.device_id}"
    assert job_id == expected_id
    assert scheduler.get_job(expected_id) is not None


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
