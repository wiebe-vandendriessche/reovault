"""Tests the job callables directly (calling them synchronously) rather than
waiting on real APScheduler timing, that library's own cron/interval trigger
math isn't ours to re-test."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from reovault.archiver import AlreadyRunningError, instance_lock
from reovault.config import ScheduleConfig
from reovault.providers.fake import FakeProvider, ScriptedRecording
from reovault.scheduler import (
    _deep_backfill_job,
    _integrity_scan_job,
    _reconcile_job,
    _run_and_sample,
    _scheduled_archive_job,
    add_device_jobs,
    build_scheduler,
)
from tests.integration.conftest import make_recording


def _rec(name: str, start: datetime, size: int = 10):
    return make_recording(name, start, size=size, raw_metadata={})


def test_build_scheduler_starts_with_no_jobs(env):
    scheduler = build_scheduler()
    assert scheduler.get_jobs() == []


def test_add_device_jobs_registers_all_four_with_the_device_suffix(env):
    archiver = env.archiver(FakeProvider())
    scheduler = build_scheduler()
    add_device_jobs(scheduler, archiver, ScheduleConfig(), device_timezone="Europe/Brussels")
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {
        f"scheduled_archive:{env.device_id}",
        f"deep_backfill:{env.device_id}",
        f"reconcile:{env.device_id}",
        f"integrity_scan:{env.device_id}",
    }


def test_add_device_jobs_is_independent_per_device(env):
    """Two devices' jobs must never collide on one id (the bug multi-camera
    would hit if job ids were still the four fixed base names)."""
    archiver_a = env.archiver(FakeProvider())
    archiver_b, device_b_id = env.archiver_for("cam-b", FakeProvider())

    scheduler = build_scheduler()
    add_device_jobs(scheduler, archiver_a, ScheduleConfig(), device_timezone="Europe/Brussels")
    add_device_jobs(scheduler, archiver_b, ScheduleConfig(), device_timezone="Europe/Brussels")

    assert len(scheduler.get_jobs()) == 8


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
    *invoked* (`started_at`), not the scan window it covered: the scheduler
    reuses "last successful run start minus 48h overlap" as the new window."""
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


def test_disabled_jobs_are_not_registered(env):
    """Item 13's "turn it on/off": a job whose *_enabled flag is False must
    not exist in the scheduler at all, not just be prevented from firing."""
    archiver = env.archiver(FakeProvider())
    scheduler = build_scheduler()
    schedule = ScheduleConfig(archive_enabled=False, backfill_enabled=True)
    add_device_jobs(scheduler, archiver, schedule, device_timezone="Europe/Brussels")
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert f"scheduled_archive:{env.device_id}" not in job_ids
    assert f"deep_backfill:{env.device_id}" in job_ids
    assert f"reconcile:{env.device_id}" in job_ids
    assert f"integrity_scan:{env.device_id}" in job_ids


def test_all_jobs_disabled_registers_nothing(env):
    archiver = env.archiver(FakeProvider())
    scheduler = build_scheduler()
    schedule = ScheduleConfig(
        archive_enabled=False,
        backfill_enabled=False,
        reconcile_enabled=False,
        integrity_scan_enabled=False,
    )
    add_device_jobs(scheduler, archiver, schedule, device_timezone="Europe/Brussels")
    assert scheduler.get_jobs() == []


def test_reschedule_all_devices_replaces_jobs_for_every_device(env):
    from reovault.fleet import Fleet
    from reovault.providers.gateway import GatewaySupervisor
    from reovault.scheduler import reschedule_all_devices

    archiver_b, device_b_id = env.archiver_for("cam-b", FakeProvider())
    fleet = Fleet(
        settings=None,  # not used by reschedule_all_devices itself
        repository=env.repository,
        vault=env.vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    fleet.archivers[env.device_id] = env.archiver(FakeProvider())
    fleet.archivers[device_b_id] = archiver_b

    scheduler = build_scheduler()
    add_device_jobs(
        scheduler, fleet.get(env.device_id), ScheduleConfig(), device_timezone="Europe/Brussels"
    )
    add_device_jobs(scheduler, archiver_b, ScheduleConfig(), device_timezone="Europe/Brussels")
    assert len(scheduler.get_jobs()) == 8

    # Disabling backfill and rescheduling must drop it for BOTH devices.
    # `reschedule_all_devices` loads each device's own effective schedule
    # (see `load_effective_schedule`), so with no per-device schedule saved
    # yet, both devices fall back to the same global/TOML-seeded one --
    # saving it here as the global schedule is what "new" schedule means.
    from reovault.config import Settings
    from reovault.scheduler import save_schedule

    new_schedule = ScheduleConfig(backfill_enabled=False)
    save_schedule(env.repository, new_schedule)
    reschedule_all_devices(scheduler, fleet, env.repository, Settings())

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert len(job_ids) == 6
    assert f"deep_backfill:{env.device_id}" not in job_ids
    assert f"deep_backfill:{device_b_id}" not in job_ids
    assert f"scheduled_archive:{env.device_id}" in job_ids
    assert f"scheduled_archive:{device_b_id}" in job_ids


# -- effective schedule: env pin, DB seed/persist --


def test_load_effective_schedule_seeds_db_from_toml_on_first_boot(env, monkeypatch):
    from reovault.config import Settings
    from reovault.scheduler import SCHEDULE_SETTING_KEY, load_effective_schedule

    monkeypatch.delenv("REOVAULT_SCHEDULE__ARCHIVE_CRON", raising=False)
    settings = Settings(schedule=ScheduleConfig(archive_cron="0 6 * * *"))
    assert env.repository.get_setting(SCHEDULE_SETTING_KEY) is None

    effective = load_effective_schedule(settings, env.repository)

    assert effective.archive_cron == "0 6 * * *"
    assert env.repository.get_setting(SCHEDULE_SETTING_KEY) is not None


def test_load_effective_schedule_db_wins_once_seeded(env):
    from reovault.config import Settings
    from reovault.scheduler import save_schedule

    save_schedule(env.repository, ScheduleConfig(archive_cron="0 9 * * *"))
    settings = Settings(schedule=ScheduleConfig(archive_cron="0 6 * * *"))  # different TOML value

    from reovault.scheduler import load_effective_schedule

    effective = load_effective_schedule(settings, env.repository)
    assert effective.archive_cron == "0 9 * * *"  # DB wins, not TOML


def test_env_pinned_schedule_always_wins_even_over_the_db(env, monkeypatch):
    from reovault.config import Settings
    from reovault.scheduler import (
        is_schedule_env_pinned,
        load_effective_schedule,
        save_schedule,
    )

    save_schedule(env.repository, ScheduleConfig(archive_cron="0 9 * * *"))
    monkeypatch.setenv("REOVAULT_SCHEDULE__ARCHIVE_CRON", "0 3 * * *")
    settings = Settings(schedule=ScheduleConfig(archive_cron="0 3 * * *"))

    assert is_schedule_env_pinned() is True
    effective = load_effective_schedule(settings, env.repository)
    assert effective.archive_cron == "0 3 * * *"  # env wins over the DB


def test_is_schedule_env_pinned_false_with_no_matching_env(monkeypatch):
    from reovault.scheduler import is_schedule_env_pinned

    monkeypatch.delenv("REOVAULT_SCHEDULE__ARCHIVE_CRON", raising=False)
    assert is_schedule_env_pinned() is False


def test_per_device_schedules_are_independent(env):
    """Two cameras, different cron times: each device's own
    `load_effective_schedule(device_id=...)` returns its own, saving one
    leaves the other on the global/TOML fallback untouched."""
    from reovault.config import Settings
    from reovault.scheduler import load_effective_schedule, save_schedule

    device_b_id = env.repository.upsert_device(alias="cam-b", channel=0, timezone="Europe/Brussels")
    settings = Settings()

    # Neither device has its own schedule yet: both see the same fallback.
    fallback = load_effective_schedule(settings, env.repository, device_id=env.device_id)
    assert load_effective_schedule(settings, env.repository, device_id=device_b_id) == fallback

    # Saving device A's own schedule must not touch device B's.
    save_schedule(env.repository, ScheduleConfig(archive_cron="0 9 * * *"), device_id=env.device_id)
    a_schedule = load_effective_schedule(settings, env.repository, device_id=env.device_id)
    b_schedule = load_effective_schedule(settings, env.repository, device_id=device_b_id)
    assert a_schedule.archive_cron == "0 9 * * *"
    assert b_schedule.archive_cron == fallback.archive_cron
    assert b_schedule.archive_cron != "0 9 * * *"
