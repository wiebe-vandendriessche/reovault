"""The Health page: status verdict, coverage, SD card, today's footage,
vault growth, the manual-run activity card, and the problems count."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from reovault import health
from reovault import retention as retention_policy
from reovault.db.repository import OnCardBytesRow, RunRow, StorageSampleRow, parse_iso_utc
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.scheduler import device_of_job_id, next_run_times, queued_manual_jobs
from reovault.timeline import local_day_bounds
from reovault.web.api.common import RunOut, local_today, run_out, utcnow
from reovault.web.deps import AppState, Device, DeviceCtx, State

router = APIRouter(tags=["health"])

MAX_BACKFILL_DAYS = 90

Level = Literal["ok", "warn", "bad", "idle"]


class StorageSampleOut(BaseModel):
    total_gb: float | None
    remain_gb: float | None
    formatted: bool | None
    mounted: bool | None
    sampled_at: datetime


class SdBarOut(BaseModel):
    """GiB, all on the camera's own binary scale (see `sd_bar`)."""

    total_gb: float
    used_gb: float
    free_gb: float
    archived_gb: float
    other_gb: float


class CoverageOut(BaseModel):
    oldest_on_card_utc: datetime | None
    oldest_unarchived_utc: datetime | None
    margin_fraction: float | None
    alarm: bool


class BannerOut(BaseModel):
    level: Literal["warn", "bad"]
    text: str
    link: Literal["problems"] | None = None


class TodayOut(BaseModel):
    date: str
    count: int
    archived_bytes: int


class HealthOut(BaseModel):
    status: Level
    running: RunOut | None
    last_run: RunOut | None
    next_archive_utc: datetime | None
    coverage: CoverageOut
    sample: StorageSampleOut | None
    sd: SdBarOut | None
    gateway_down: bool
    problems_count: int
    banner: BannerOut | None
    today: TodayOut


class GrowthDayOut(BaseModel):
    day: str
    bytes: int


class GrowthOut(BaseModel):
    archived_count: int
    archived_bytes: int
    days: list[GrowthDayOut]
    avg_per_day: float
    yearly_projection: float
    vault_free_bytes: int | None
    days_headroom: int | None
    vault_cap_bytes: int | None
    vault_used_bytes: int


class ActivityEventOut(BaseModel):
    at: datetime
    job_id: str
    kind: str
    detail: str | None


class ActivityOut(BaseModel):
    running: RunOut | None
    queued: int
    events: list[ActivityEventOut]
    scheduler_available: bool
    timezone: str
    default_from: str  # device-local "YYYY-MM-DDTHH:MM", for datetime-local inputs
    default_to: str
    max_backfill_days: int


class CountOut(BaseModel):
    count: int


def sample_out(sample: StorageSampleRow | None) -> StorageSampleOut | None:
    if sample is None:
        return None
    return StorageSampleOut(
        total_gb=sample.total_gb,
        remain_gb=sample.remain_gb,
        formatted=sample.formatted,
        mounted=sample.mounted,
        sampled_at=parse_iso_utc(sample.sampled_at),
    )


def sd_bar(sample: StorageSampleRow | None, on_card: OnCardBytesRow) -> SdBarOut | None:
    """Archived vs. other used space on the card. `remote_size` is decimal
    bytes, but the camera's own `totalGB`/`remainGB` are binary GiB (a
    256GB/1e9 card reports 238.7 "GB"), so both convert through 2**30. Two
    segments, not three: a "not yet archived" bucket needs the card's real
    retention boundary to mean anything, which isn't reliably available."""
    if sample is None or not sample.total_gb:
        return None
    total = sample.total_gb
    used = max(total - (sample.remain_gb or 0), 0.0)
    archived = min(on_card.archived_bytes / 2**30, used)
    return SdBarOut(
        total_gb=total,
        used_gb=used,
        free_gb=max(total - used, 0.0),
        archived_gb=archived,
        other_gb=max(used - archived, 0.0),
    )


def on_card(st: AppState, device_id: int, first_start_utc: str | None) -> OnCardBytesRow:
    # Not `sample.oldest_recording_utc`: that needs a full 3650-day `vod
    # search` that comes back empty far more often than it succeeds. The
    # earliest recording ever discovered is always available.
    since = parse_iso_utc(first_start_utc) if first_start_utc else None
    return st.repo.on_card_bytes(device_id, since)


def gateway_down(dev: DeviceCtx) -> bool:
    provider = dev.archiver.provider
    return isinstance(provider, ReolinkCliProvider) and not provider.gateway.is_listening()


def _status(last_run: RunRow | None, running: RunRow | None) -> Level:
    if running is not None:
        return "ok"
    if last_run is None:
        return "idle"
    outcomes: dict[str, Level] = {
        "success": "ok",
        "partial": "warn",
        "failed": "bad",
        "aborted": "bad",
    }
    return outcomes.get(last_run.outcome or "", "idle")


def _banner(
    sample: StorageSampleRow | None,
    coverage: health.CoverageStatus,
    is_gateway_down: bool,
    problems: int,
) -> BannerOut | None:
    if sample is not None and sample.mounted is False:
        return BannerOut(level="bad", text="The SD card is not mounted.")
    if coverage.alarm:
        pct = round((coverage.margin_fraction or 0) * 100)
        return BannerOut(
            level="warn",
            text=f"Falling behind: only {pct}% of the card's retention window is left.",
        )
    if is_gateway_down:
        return BannerOut(level="warn", text="The camera gateway is unreachable.")
    if problems > 0:
        plural = "s" if problems != 1 else ""
        return BannerOut(
            level="bad",
            text=f"{problems} recording{plural} {'needs' if problems == 1 else 'need'} attention.",
            link="problems",
        )
    return None


@router.get("/health")
def get_health(dev: Device, st: State) -> HealthOut:
    repo = st.repo
    totals = repo.totals(dev.device_id)
    coverage = health.coverage_margin(repository=repo, device_id=dev.device_id)
    sample = repo.latest_storage_sample(dev.device_id)
    running = repo.running_run(dev.device_id)
    last_run = repo.last_finished_run(dev.device_id)
    times = next_run_times(st.scheduler).get(dev.device_id, {}) if st.scheduler else {}
    is_down = gateway_down(dev)
    problems = totals.failed_count + totals.quarantined_count
    today = local_today(dev.tz)
    day_start, day_end = local_day_bounds(today, dev.tz)
    count, archived_bytes = repo.window_summary(
        device_id=dev.device_id, from_utc=day_start, to_utc=day_end
    )
    return HealthOut(
        status=_status(last_run, running),
        running=run_out(st, running) if running else None,
        last_run=run_out(st, last_run) if last_run else None,
        next_archive_utc=times.get("scheduled_archive"),
        coverage=CoverageOut(
            oldest_on_card_utc=coverage.oldest_on_card_utc,
            oldest_unarchived_utc=coverage.oldest_unarchived_utc,
            margin_fraction=coverage.margin_fraction,
            alarm=coverage.alarm,
        ),
        sample=sample_out(sample),
        sd=sd_bar(sample, on_card(st, dev.device_id, totals.first_start_utc)),
        gateway_down=is_down,
        problems_count=problems,
        banner=_banner(sample, coverage, is_down, problems),
        today=TodayOut(date=today.isoformat(), count=count, archived_bytes=archived_bytes),
    )


@router.get("/growth")
def get_growth(dev: Device, st: State, days: int = 30) -> GrowthOut:
    days = max(1, min(days, 366))
    repo = st.repo
    policy = retention_policy.load_effective_retention(st.settings, repo)
    totals = repo.totals(dev.device_id)
    # The last `days` camera-local days, today included. Both the window and
    # the labels are local: `runs_per_day` buckets by local date, so UTC
    # labels would shift every bar by a day around midnight.
    first = local_today(dev.tz) - timedelta(days=days - 1)
    from_utc, _ = local_day_bounds(first, dev.tz)
    series = repo.runs_per_day(
        device_id=dev.device_id, from_utc=from_utc, to_utc=utcnow(), tz=dev.tz
    )
    by_day = {r.day: r.bytes_archived for r in series}
    day_list = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    values = [by_day.get(d, 0) for d in day_list]
    avg = sum(values) / days
    try:
        free: int | None = shutil.disk_usage(st.settings.storage.vault_dir).free
    except OSError:
        free = None
    return GrowthOut(
        archived_count=totals.archived_count,
        archived_bytes=totals.archived_plaintext_bytes,
        days=[GrowthDayOut(day=d, bytes=v) for d, v in zip(day_list, values, strict=True)],
        avg_per_day=avg,
        yearly_projection=avg * 365,
        vault_free_bytes=free,
        days_headroom=int(free / avg) if free is not None and avg > 0 else None,
        vault_cap_bytes=(
            int(policy.max_vault_gb * retention_policy.GB) if policy.max_vault_gb else None
        ),
        vault_used_bytes=repo.archived_ciphertext_total(),
    )


def activity_out(st: AppState, dev: DeviceCtx) -> ActivityOut:
    running = st.repo.running_run(dev.device_id)
    # The activity log listens to the whole scheduler, so filter to this
    # device's jobs, or camera A's card shows camera B's activity.
    events = [e for e in st.activity_log.recent() if device_of_job_id(e.job_id) == dev.device_id][
        :5
    ]
    from zoneinfo import ZoneInfo

    now_local = datetime.now(ZoneInfo(dev.tz)).replace(second=0, microsecond=0, tzinfo=None)
    return ActivityOut(
        running=run_out(st, running) if running else None,
        queued=queued_manual_jobs(dev.device_id),
        events=[
            ActivityEventOut(at=e.at, job_id=e.job_id, kind=e.kind, detail=st.redact(e.detail))
            for e in events
        ],
        scheduler_available=st.scheduler is not None,
        timezone=dev.tz,
        default_from=(now_local - timedelta(hours=48)).isoformat(timespec="minutes"),
        default_to=now_local.isoformat(timespec="minutes"),
        max_backfill_days=MAX_BACKFILL_DAYS,
    )


@router.get("/activity")
def get_activity(dev: Device, st: State) -> ActivityOut:
    return activity_out(st, dev)


@router.get("/problems/count")
def get_problems_count(dev: Device, st: State) -> CountOut:
    totals = st.repo.totals(dev.device_id)
    return CountOut(count=totals.failed_count + totals.quarantined_count)
