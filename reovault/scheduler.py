"""APScheduler jobs, one set per device.

Four standing jobs per device:

- `scheduled_archive:<device_id>`: daily (default 05:00), window = last
  successful run's start minus the configured overlap, to now.
- `deep_backfill:<device_id>`: weekly (default Sunday 04:00), trailing N days.
- `reconcile:<device_id>`: every N hours.
- `integrity_scan:<device_id>`: weekly, a rotating sample (see health.py).

`storage_sample` is deliberately not its own job: it runs on every archive
run, so it's invoked from inside the archive/backfill job functions right
after `Archiver.run()`, the same way `cli.py`'s manual `run`/`backfill`
commands do.

Cron timezone: each device's jobs are evaluated in that device's own
`timezone`, unless `schedule.timezone` is set, which forces every device
onto one zone regardless of where each camera actually is.

Every job is `max_instances=1, coalesce=True` (skip a fire that would
overlap a still-running one), belt and suspenders on top of the archiver's
own `flock`, which is the real mutual-exclusion mechanism. The scheduler
itself runs everything through a single-worker executor (`ThreadPoolExecutor(1)`):
every camera call funnels through one `reolink-gateway` whose concurrent
behavior is unverified, and throughput here is bounded by the LAN and one
disk, not concurrency.
# ponytail: one worker, so archive runs are sequential across cameras.
# Raise to N and give each camera its own gateway only if archive windows
# actually start overlapping in practice.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from reovault import health
from reovault.archiver import AlreadyRunningError, Archiver
from reovault.config import ScheduleConfig, Settings
from reovault.db.repository import Repository
from reovault.fleet import Fleet
from reovault.logging import get_logger

logger = get_logger(__name__)

MANUAL_ARCHIVE_JOB_BASE = "manual_archive"
MANUAL_BACKFILL_JOB_BASE = "manual_backfill"
MANUAL_RECONCILE_JOB_BASE = "manual_reconcile"

_STANDING_JOB_BASES = ("scheduled_archive", "deep_backfill", "reconcile", "integrity_scan")

# Spreads N cameras' scheduled fires across a 5-minute window instead of all
# hitting the one shared gateway at exactly the same second.
_JITTER_SECS = 300

SCHEDULE_SETTING_KEY = "schedule"
_SCHEDULE_ENV_PREFIX = "REOVAULT_SCHEDULE__"


def is_schedule_env_pinned() -> bool:
    """True if any `REOVAULT_SCHEDULE__*` env var is set. Whole-document
    pinning, not per-field: an operator who pinned even one schedule field
    almost certainly wants the whole schedule fixed and out of the
    dashboard's reach, and per-field merge logic would be more code for a
    much harder story to explain in the UI."""
    return any(k.startswith(_SCHEDULE_ENV_PREFIX) for k in os.environ)


def _schedule_key(device_id: int | None) -> str:
    return SCHEDULE_SETTING_KEY if device_id is None else f"{SCHEDULE_SETTING_KEY}:{device_id}"


def load_effective_schedule(
    settings: Settings, repository: Repository, device_id: int | None = None
) -> ScheduleConfig:
    """The schedule actually in effect for one camera (or the global
    fallback, with `device_id=None`). Env-pinned always wins outright, for
    every camera alike (see `is_schedule_env_pinned`'s docstring).
    Otherwise: this device's own saved schedule if it has one, else the
    global `schedule` key (seeded from `reovault.toml`'s `[schedule]` table
    on first boot, logged once), so a camera that has never had its own
    schedule edited still inherits the same effective schedule it always
    had before per-device schedules existed."""
    if is_schedule_env_pinned():
        return settings.schedule
    if device_id is not None:
        stored = repository.get_setting(_schedule_key(device_id))
        if stored is not None:
            return ScheduleConfig.model_validate_json(stored)
    stored = repository.get_setting(SCHEDULE_SETTING_KEY)
    if stored is None:
        repository.set_setting(SCHEDULE_SETTING_KEY, settings.schedule.model_dump_json())
        logger.info("settings.schedule_seeded_from_toml")
        return settings.schedule
    return ScheduleConfig.model_validate_json(stored)


def save_schedule(
    repository: Repository, schedule: ScheduleConfig, device_id: int | None = None
) -> None:
    repository.set_setting(_schedule_key(device_id), schedule.model_dump_json())


def _job_id(base: str, device_id: int) -> str:
    return f"{base}:{device_id}"


def device_of_job_id(job_id: str) -> int | None:
    """Inverse of `_job_id`: the device id a job belongs to, or None for a
    job id that isn't one of ours (defensive; shouldn't happen)."""
    _, _, suffix = job_id.rpartition(":")
    try:
        return int(suffix)
    except ValueError:
        return None


def _run_and_sample(
    archiver: Archiver, *, trigger: str, from_utc: datetime, to_utc: datetime
) -> None:
    try:
        result = archiver.run(trigger=trigger, from_utc=from_utc, to_utc=to_utc)
    except AlreadyRunningError:
        logger.warning(
            "scheduler.skipped_already_running", trigger=trigger, device_id=archiver.device_id
        )
        return

    logger.info(
        "scheduler.run_finished",
        trigger=trigger,
        device_id=archiver.device_id,
        discovered=result.discovered,
        downloaded=result.downloaded,
        failed=result.failed,
        error=result.error,
    )

    try:
        health.record_storage_sample(
            repository=archiver.repository, provider=archiver.provider, device_id=archiver.device_id
        )
    except Exception:
        logger.exception("scheduler.storage_sample_failed", device_id=archiver.device_id)
        return

    status = health.coverage_margin(repository=archiver.repository, device_id=archiver.device_id)
    if status.alarm:
        logger.error(
            "scheduler.coverage_alarm",
            device_id=archiver.device_id,
            margin_fraction=status.margin_fraction,
            oldest_on_card_utc=status.oldest_on_card_utc,
            oldest_unarchived_utc=status.oldest_unarchived_utc,
        )


def _scheduled_archive_job(archiver: Archiver, overlap_hours: int) -> None:
    now = datetime.now(UTC)
    last_start = archiver.repository.last_successful_run_started_at(archiver.device_id)
    # First-ever scheduled run has no prior success to anchor to; operators
    # are expected to `reovault backfill` once to seed history before this
    # fires, so a plain overlap window back from now is a safe, small default
    # rather than guessing at a longer one.
    from_utc = (
        (last_start - timedelta(hours=overlap_hours))
        if last_start
        else now - timedelta(hours=overlap_hours)
    )
    _run_and_sample(archiver, trigger="schedule", from_utc=from_utc, to_utc=now)


def _deep_backfill_job(archiver: Archiver, days: int) -> None:
    now = datetime.now(UTC)
    _run_and_sample(archiver, trigger="backfill", from_utc=now - timedelta(days=days), to_utc=now)


def _reconcile_job(archiver: Archiver) -> None:
    try:
        archiver.reconcile()
    except AlreadyRunningError:
        logger.warning("scheduler.reconcile_skipped_already_running", device_id=archiver.device_id)


def _integrity_scan_job(archiver: Archiver, sample_pct: float) -> None:
    result = health.run_integrity_scan(
        repository=archiver.repository,
        vault=archiver.vault,
        device_id=archiver.device_id,
        sample_pct=sample_pct,
    )
    logger.info(
        "scheduler.integrity_scan_finished",
        device_id=archiver.device_id,
        checked=len(result.checked),
        failed=len(result.failed),
    )


def build_scheduler() -> BackgroundScheduler:
    """One instance per daemon process, with no jobs yet: call
    `add_device_jobs` once per device in the fleet. Caller owns
    start()/shutdown(). A single-worker executor, see module docstring."""
    return BackgroundScheduler(timezone=UTC, executors={"default": ThreadPoolExecutor(1)})


def add_device_jobs(
    scheduler: BackgroundScheduler,
    archiver: Archiver,
    schedule: ScheduleConfig,
    *,
    device_timezone: str,
) -> None:
    """Adds whichever of the four standing jobs are enabled for one device
    (see `ScheduleConfig.*_enabled`). Safe to call on an already-`start()`ed
    scheduler (adding a camera at runtime wakes it)."""
    device_id = archiver.device_id
    tz = ZoneInfo(schedule.timezone or device_timezone)
    if schedule.archive_enabled:
        scheduler.add_job(
            _scheduled_archive_job,
            CronTrigger.from_crontab(schedule.archive_cron, timezone=tz),
            args=[archiver, schedule.archive_overlap_hours],
            id=_job_id("scheduled_archive", device_id),
            max_instances=1,
            coalesce=True,
            jitter=_JITTER_SECS,
        )
    if schedule.backfill_enabled:
        scheduler.add_job(
            _deep_backfill_job,
            CronTrigger.from_crontab(schedule.backfill_cron, timezone=tz),
            args=[archiver, schedule.backfill_days],
            id=_job_id("deep_backfill", device_id),
            max_instances=1,
            coalesce=True,
            jitter=_JITTER_SECS,
        )
    if schedule.reconcile_enabled:
        scheduler.add_job(
            _reconcile_job,
            IntervalTrigger(hours=schedule.reconcile_interval_hours),
            args=[archiver],
            id=_job_id("reconcile", device_id),
            max_instances=1,
            coalesce=True,
        )
    if schedule.integrity_scan_enabled:
        scheduler.add_job(
            _integrity_scan_job,
            IntervalTrigger(weeks=1),
            args=[archiver, schedule.integrity_scan_sample_pct],
            id=_job_id("integrity_scan", device_id),
            max_instances=1,
            coalesce=True,
        )


def reschedule_all_devices(
    scheduler: BackgroundScheduler,
    fleet: Fleet,
    repository: Repository,
    settings: Settings,
) -> None:
    """Applies each device's own effective schedule (see
    `load_effective_schedule`) to every device currently in the fleet: drop
    each device's four job ids and re-add whichever are enabled under that
    device's schedule. Used at startup and whenever the fleet itself
    changes (a device added/enabled), so per-device schedules -- not one
    schedule applied to everyone -- are what actually takes effect."""
    for device_id in fleet.ids():
        archiver = fleet.get(device_id)
        device_row = repository.get_device(device_id)
        if archiver is None or device_row is None:
            continue
        schedule = load_effective_schedule(settings, repository, device_id=device_id)
        remove_device_jobs(scheduler, device_id)
        add_device_jobs(scheduler, archiver, schedule, device_timezone=device_row.timezone)


def reschedule_device(
    scheduler: BackgroundScheduler,
    archiver: Archiver,
    repository: Repository,
    schedule: ScheduleConfig,
    *,
    device_timezone: str,
) -> None:
    """The single-camera version of `reschedule_all_devices`: applies a
    freshly-saved schedule to just the one camera that was edited, leaving
    every other camera's jobs untouched. Used by the dashboard's
    per-device Schedule tab."""
    remove_device_jobs(scheduler, archiver.device_id)
    add_device_jobs(scheduler, archiver, schedule, device_timezone=device_timezone)


def remove_device_jobs(scheduler: BackgroundScheduler, device_id: int) -> None:
    """Best-effort: removes whichever of the four standing job ids exist for
    this device. Used when a device is disabled/removed from the fleet."""
    for base in _STANDING_JOB_BASES:
        job_id = _job_id(base, device_id)
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)


# -- manual jobs queued from the dashboard. The web layer never touches
# APScheduler directly; it calls these, so scheduler knowledge stays in one
# module.
#
# Each uses a per-device job id with `replace_existing=False`, so a second
# click on the same camera while the first is still queued raises
# apscheduler's own `ConflictingIdError` rather than silently queueing
# twice. Two different cameras never collide on one id. The web layer
# converts a ConflictingIdError into an "already queued" fragment.


def queue_manual_archive(
    scheduler: BackgroundScheduler, archiver: Archiver, *, from_utc: datetime, to_utc: datetime
) -> str:
    job_id = _job_id(MANUAL_ARCHIVE_JOB_BASE, archiver.device_id)
    scheduler.add_job(
        _run_and_sample,
        DateTrigger(run_date=datetime.now(UTC)),
        kwargs={"archiver": archiver, "trigger": "manual", "from_utc": from_utc, "to_utc": to_utc},
        id=job_id,
        max_instances=1,
        misfire_grace_time=None,
        replace_existing=False,
    )
    return job_id


def queue_manual_backfill(
    scheduler: BackgroundScheduler, archiver: Archiver, *, from_utc: datetime, to_utc: datetime
) -> str:
    job_id = _job_id(MANUAL_BACKFILL_JOB_BASE, archiver.device_id)
    scheduler.add_job(
        _run_and_sample,
        DateTrigger(run_date=datetime.now(UTC)),
        kwargs={
            "archiver": archiver,
            "trigger": "backfill",
            "from_utc": from_utc,
            "to_utc": to_utc,
        },
        id=job_id,
        max_instances=1,
        misfire_grace_time=None,
        replace_existing=False,
    )
    return job_id


def queue_manual_reconcile(scheduler: BackgroundScheduler, archiver: Archiver) -> str:
    job_id = _job_id(MANUAL_RECONCILE_JOB_BASE, archiver.device_id)
    scheduler.add_job(
        _reconcile_job,
        DateTrigger(run_date=datetime.now(UTC)),
        args=[archiver],
        id=job_id,
        max_instances=1,
        misfire_grace_time=None,
        replace_existing=False,
    )
    return job_id


def next_run_times(scheduler: BackgroundScheduler) -> dict[int, dict[str, datetime | None]]:
    """Next fire time for each standing job, grouped by device id, for the
    Overview/Schedule pages: `{device_id: {"scheduled_archive": ..., ...}}`.
    Iterates `get_jobs()` and splits each id rather than probing fixed
    names, since the job set is now dynamic (one set per device, devices
    can be added at runtime).

    A job added while the scheduler is stopped is only "pending":
    APScheduler doesn't compute `next_run_time` (a __slots__ attribute,
    never even set) until start() runs, so it's legitimately absent (via
    getattr's default) rather than merely None."""
    result: dict[int, dict[str, datetime | None]] = {}
    for job in scheduler.get_jobs():
        device_id = device_of_job_id(job.id)
        if device_id is None:
            continue
        base = job.id.rsplit(":", 1)[0]
        if base not in _STANDING_JOB_BASES:
            continue
        result.setdefault(device_id, {})[base] = getattr(job, "next_run_time", None)
    return result
