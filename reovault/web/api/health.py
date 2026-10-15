"""The Health page: status verdict, coverage, SD card, today's footage,
vault growth, the manual-run activity card, and the problems count."""

from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta
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


class TimelineBucketOut(BaseModel):
    start_utc: datetime
    archived: int
    waiting: int
    quarantined: int


class CardOut(BaseModel):
    """One camera's SD card, as `health.card_status` sees it, plus clip
    counts across the card's window for the timeline strip."""

    state: health.CardState
    waiting_count: int
    lost_count: int
    oldest_waiting_utc: datetime | None
    window_start_utc: datetime | None
    window_estimated: bool
    safe_for_seconds: float | None
    used_fraction: float | None
    timeline: list[TimelineBucketOut]


class BannerOut(BaseModel):
    level: Literal["warn", "bad"]
    text: str
    link: Literal["problems"] | None = None


class IssueOut(BaseModel):
    """One open condition on one camera, for the Overview's "needs
    attention" list. `link` is the page that resolves it."""

    device_id: int
    level: Literal["warn", "bad"]
    text: str
    link: Literal["problems", "health"]


class TodayOut(BaseModel):
    date: str
    count: int
    archived_bytes: int


class HealthOut(BaseModel):
    status: Level
    running: RunOut | None
    last_run: RunOut | None
    next_archive_utc: datetime | None
    card: CardOut
    sample: StorageSampleOut | None
    gateway_down: bool
    problems_count: int
    banner: BannerOut | None
    today: TodayOut


class GrowthDayOut(BaseModel):
    day: str
    bytes: int
    clips: int


class GrowthOut(BaseModel):
    """One camera's archived footage over the last N local days, by the day
    it was *recorded* (not the day a run fetched it, so a backfill doesn't
    pile a week into one bar). The vault's disk lives on `/overview`."""

    days: list[GrowthDayOut]
    total_bytes: int
    total_clips: int
    avg_per_day: float
    yearly_projection: float


class VaultOut(BaseModel):
    """The archive's volume (the disk/mount `vault_dir` lives on)."""

    total_bytes: int | None
    used_bytes: int | None  # everything on the volume, ReoVault included
    free_bytes: int | None
    archive_bytes: int  # ReoVault's encrypted clips
    cap_bytes: int | None  # retention.max_vault_gb
    max_age_days: int | None
    clip_count: int
    clip_bytes: int  # plaintext, what the clips play back as
    first_start_utc: datetime | None
    last_start_utc: datetime | None
    avg_per_day: float  # archived bytes/day over the last 30 days, fleet-wide
    days_headroom: int | None


class FleetCardOut(BaseModel):
    device_id: int
    alias: str
    name: str | None
    enabled: bool
    card: CardOut
    sample: StorageSampleOut | None
    running: bool
    last_run: RunOut | None
    next_archive_utc: datetime | None
    gateway_down: bool
    problems_count: int


class OverviewOut(BaseModel):
    # Every open condition across the fleet, most urgent first: a card not
    # recording, then clips about to be overwritten (soonest first), then a
    # gateway outage, then failed clips (already lost or retryable later).
    issues: list[IssueOut]
    vault: VaultOut
    cards: list[FleetCardOut]


class TypeDayOut(BaseModel):
    day: str
    counts: dict[str, int]  # rec_type -> clips that day


class TypeStatsOut(BaseModel):
    types: list[str]  # every type seen in the window, busiest first
    days: list[TypeDayOut]  # one entry per local day, oldest first, zeros included


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


TIMELINE_BUCKETS = 48


def card_out(st: AppState, device_id: int) -> tuple[health.CardStatus, CardOut]:
    now = utcnow()
    card = health.card_status(repository=st.repo, device_id=device_id, now=now)
    start = card.oldest_on_card_utc or (now - card.window if card.window else None)
    timeline = (
        st.repo.card_timeline(
            device_id=device_id, from_utc=start, to_utc=now, buckets=TIMELINE_BUCKETS
        )
        if start
        else []
    )
    return card, CardOut(
        state=card.state,
        waiting_count=card.waiting_count,
        lost_count=card.lost_count,
        oldest_waiting_utc=card.oldest_waiting_utc,
        window_start_utc=start,
        window_estimated=card.window_estimated,
        safe_for_seconds=card.safe_for.total_seconds() if card.safe_for is not None else None,
        used_fraction=card.used_fraction,
        timeline=[
            TimelineBucketOut(
                start_utc=b.start_utc,
                archived=b.archived,
                waiting=b.waiting,
                quarantined=b.quarantined,
            )
            for b in timeline
        ],
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


def _issues(
    card: health.CardStatus,
    is_gateway_down: bool,
    problems: int,
) -> list[tuple[int, float, BannerOut]]:
    """Every open condition on one camera as (rank, seconds left, banner),
    lower sorting first. Seconds left orders at-risk cards across cameras."""
    out: list[tuple[int, float, BannerOut]] = []
    if card.state == "not_mounted":
        out.append((0, 0.0, BannerOut(level="bad", text="The SD card is not mounted.")))
    if card.alarm:
        n = card.waiting_count
        left = card.safe_for.total_seconds() if card.safe_for is not None else 0.0
        text = (
            f"{n} clip{'s' if n != 1 else ''} not archived yet; the oldest is overwritten "
            f"in about {health.human_duration(card.safe_for)}."
        )
        out.append((1, left, BannerOut(level="warn", text=text)))
    if is_gateway_down:
        out.append((2, 0.0, BannerOut(level="warn", text="The camera gateway is unreachable.")))
    if problems > 0:
        plural = "s" if problems != 1 else ""
        text = f"{problems} recording{plural} {'needs' if problems == 1 else 'need'} attention."
        out.append((3, 0.0, BannerOut(level="bad", text=text, link="problems")))
    return out


def _banner(card: health.CardStatus, is_gateway_down: bool, problems: int) -> BannerOut | None:
    """The single most urgent condition, for one camera's Health page."""
    issues = _issues(card, is_gateway_down, problems)
    return issues[0][2] if issues else None


@router.get("/health")
def get_health(dev: Device, st: State) -> HealthOut:
    repo = st.repo
    totals = repo.totals(dev.device_id)
    card, card_o = card_out(st, dev.device_id)
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
        card=card_o,
        sample=sample_out(sample),
        gateway_down=is_down,
        problems_count=problems,
        banner=_banner(card, is_down, problems),
        today=TodayOut(date=today.isoformat(), count=count, archived_bytes=archived_bytes),
    )


@router.get("/overview")
def get_overview(st: State) -> OverviewOut:
    """The fleet home page: the archive's volume plus every enabled camera's
    SD card. Reads stored samples only, so a page load never calls a camera."""
    repo = st.repo
    now = utcnow()
    times = next_run_times(st.scheduler) if st.scheduler else {}
    cards: list[FleetCardOut] = []
    issues: list[tuple[int, float, IssueOut]] = []
    clip_count = clip_bytes = recent_bytes = 0
    first: str | None = None
    last: str | None = None
    for d in repo.list_devices(enabled_only=True):
        totals = repo.totals(d.id)
        clip_count += totals.archived_count
        clip_bytes += totals.archived_plaintext_bytes
        first = min(filter(None, (first, totals.first_start_utc)), default=None)
        last = max(filter(None, (last, totals.last_start_utc)), default=None)
        recent_bytes += sum(
            r.bytes_archived
            for r in repo.runs_per_day(
                device_id=d.id, from_utc=now - timedelta(days=30), to_utc=now, tz=d.timezone
            )
        )
        archiver = st.fleet.get(d.id)
        is_down = (
            archiver is not None
            and isinstance(archiver.provider, ReolinkCliProvider)
            and not archiver.provider.gateway.is_listening()
        )
        card, card_o = card_out(st, d.id)
        running = repo.running_run(d.id)
        last_run = repo.last_finished_run(d.id)
        problems = totals.failed_count + totals.quarantined_count
        for rank, left, b in _issues(card, is_down, problems):
            issue = IssueOut(
                device_id=d.id,
                level=b.level,
                text=f"{d.name or d.alias}: {b.text}",
                link=b.link or "health",
            )
            issues.append((rank, left, issue))
        cards.append(
            FleetCardOut(
                device_id=d.id,
                alias=d.alias,
                name=d.name,
                enabled=d.enabled,
                card=card_o,
                sample=sample_out(repo.latest_storage_sample(d.id)),
                running=running is not None,
                last_run=run_out(st, last_run) if last_run else None,
                next_archive_utc=times.get(d.id, {}).get("scheduled_archive"),
                gateway_down=is_down,
                problems_count=problems,
            )
        )
    try:
        usage = shutil.disk_usage(st.settings.storage.vault_dir)
    except OSError:
        usage = None
    policy = st.settings.retention
    avg = recent_bytes / 30
    return OverviewOut(
        issues=[i for _, _, i in sorted(issues, key=lambda t: (t[0], t[1]))],
        vault=VaultOut(
            total_bytes=usage.total if usage else None,
            used_bytes=usage.used if usage else None,
            free_bytes=usage.free if usage else None,
            archive_bytes=repo.archived_ciphertext_total(),
            cap_bytes=(
                int(policy.max_vault_gb * retention_policy.GB) if policy.max_vault_gb else None
            ),
            max_age_days=policy.max_age_days,
            clip_count=clip_count,
            clip_bytes=clip_bytes,
            first_start_utc=parse_iso_utc(first) if first else None,
            last_start_utc=parse_iso_utc(last) if last else None,
            avg_per_day=avg,
            days_headroom=int(usage.free / avg) if usage and avg > 0 else None,
        ),
        cards=cards,
    )


@router.get("/growth")
def get_growth(dev: Device, st: State, days: int = 30) -> GrowthOut:
    days = max(1, min(days, 366))
    local_days, from_utc = _local_window(dev, days)
    by_day = {
        r.day: r
        for r in st.repo.day_buckets(
            device_id=dev.device_id, from_utc=from_utc, to_utc=utcnow(), tz=dev.tz
        )
    }
    out = [
        GrowthDayOut(
            day=d.isoformat(),
            bytes=b.bytes if (b := by_day.get(d.isoformat())) else 0,
            clips=b.archived if b else 0,
        )
        for d in local_days
    ]
    total = sum(d.bytes for d in out)
    return GrowthOut(
        days=out,
        total_bytes=total,
        total_clips=sum(d.clips for d in out),
        avg_per_day=total / days,
        yearly_projection=total / days * 365,
    )


def _local_window(dev: DeviceCtx, days: int) -> tuple[list[date], datetime]:
    """The last `days` camera-local days, today included, and the UTC
    instant the first one starts."""
    first = local_today(dev.tz) - timedelta(days=days - 1)
    return [first + timedelta(days=i) for i in range(days)], local_day_bounds(first, dev.tz)[0]


@router.get("/stats/types")
def get_type_stats(dev: Device, st: State, days: int = 30) -> TypeStatsOut:
    """Clips per day per detection type, for the Health page's chart."""
    day_list, from_utc = _local_window(dev, max(1, min(days, 366)))
    per_day = st.repo.type_counts_per_day(
        device_id=dev.device_id, from_utc=from_utc, to_utc=utcnow(), tz=dev.tz
    )
    totals: dict[str, int] = {}
    for counts in per_day.values():
        for t, n in counts.items():
            totals[t] = totals.get(t, 0) + n
    return TypeStatsOut(
        types=sorted(totals, key=lambda t: (-totals[t], t)),
        days=[
            TypeDayOut(day=d.isoformat(), counts=per_day.get(d.isoformat(), {})) for d in day_list
        ],
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
