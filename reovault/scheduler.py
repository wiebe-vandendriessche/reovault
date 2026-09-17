"""APScheduler jobs. See plan: Scheduling.

Wires the cron/interval jobs onto a `BackgroundScheduler`:

- `scheduled_archive`: daily (default 05:00 UTC), window = last successful
  run's start minus the configured overlap, to now.
- `deep_backfill`: weekly (default Sunday 04:00 UTC), trailing N days.
- `reconcile`: every N hours.
- `integrity_scan`: weekly, a rotating sample (see health.py).

`storage_sample` is deliberately not its own job: per plan it runs "each
run", so it's invoked from inside the archive/backfill job functions right
after `Archiver.run()`, the same way `cli.py`'s manual `run`/`backfill`
commands do.

Cron times are evaluated in UTC. The plan doesn't specify which timezone
"daily 05:00" means; UTC keeps this unambiguous (matching the CLI's own
`--from`/`--to` convention) rather than guessing at device-local intent.

All jobs are single-instance and skip a fire that would overlap a still-running
one (`max_instances=1`, `coalesce=True`) — belt and suspenders on top of the
archiver's own `flock`, which is the real mutual-exclusion mechanism (see
plan: "so a manual run can never race the scheduled one").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from reovault import health
from reovault.archiver import AlreadyRunningError, Archiver
from reovault.config import ScheduleConfig
from reovault.logging import get_logger

logger = get_logger(__name__)


def _run_and_sample(
    archiver: Archiver, *, trigger: str, from_utc: datetime, to_utc: datetime
) -> None:
    try:
        result = archiver.run(trigger=trigger, from_utc=from_utc, to_utc=to_utc)
    except AlreadyRunningError:
        logger.warning("scheduler.skipped_already_running", trigger=trigger)
        return

    logger.info(
        "scheduler.run_finished",
        trigger=trigger,
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
        logger.exception("scheduler.storage_sample_failed")
        return

    status = health.coverage_margin(repository=archiver.repository, device_id=archiver.device_id)
    if status.alarm:
        logger.error(
            "scheduler.coverage_alarm",
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
        logger.warning("scheduler.reconcile_skipped_already_running")


def _integrity_scan_job(archiver: Archiver, sample_pct: float) -> None:
    result = health.run_integrity_scan(
        repository=archiver.repository,
        vault=archiver.vault,
        device_id=archiver.device_id,
        sample_pct=sample_pct,
    )
    logger.info(
        "scheduler.integrity_scan_finished", checked=len(result.checked), failed=len(result.failed)
    )


def build_scheduler(archiver: Archiver, schedule: ScheduleConfig) -> BackgroundScheduler:
    """One instance per daemon process. Caller owns start()/shutdown()."""
    scheduler = BackgroundScheduler(timezone=UTC)
    scheduler.add_job(
        _scheduled_archive_job,
        CronTrigger.from_crontab(schedule.archive_cron, timezone=UTC),
        args=[archiver, schedule.archive_overlap_hours],
        id="scheduled_archive",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _deep_backfill_job,
        CronTrigger.from_crontab(schedule.backfill_cron, timezone=UTC),
        args=[archiver, schedule.backfill_days],
        id="deep_backfill",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _reconcile_job,
        IntervalTrigger(hours=schedule.reconcile_interval_hours),
        args=[archiver],
        id="reconcile",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _integrity_scan_job,
        IntervalTrigger(weeks=1),
        args=[archiver, schedule.integrity_scan_sample_pct],
        id="integrity_scan",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
