"""Coverage/lag alarm, storage sampling, and integrity scanning.

Deliberately separate from `archiver.py`: the archiver's job is "make the
state machine correct", this module's job is "notice when things are
trending toward data loss" and "notice bit rot before a downstream backup
snapshot overwrites the last good copy". Neither needs the other's
internals, they only share the `Repository`/`VaultStore` boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from reovault.db.repository import Repository, parse_iso_utc
from reovault.logging import get_logger
from reovault.providers.base import CameraProvider
from reovault.storage.vault import VaultStore

logger = get_logger(__name__)

DEFAULT_COVERAGE_MARGIN = 0.20

# Finding the oldest recording still physically on the card walks back one
# day at a time (never one huge search, see issue #13) and stops after this
# many empty days in a row. A camera that recorded nothing for longer than
# that (unplugged for a week) makes the oldest look more recent than it is,
# which errs toward the coverage alarm firing early, never late.
EMPTY_DAYS_TO_STOP = 3
# Alarm state last logged, per device. `card_status` is polled by
# /healthz, the dashboard and alert evaluation, so it logs when the alarm
# turns on or off, not on every call.
_logged_alarm: dict[int, bool] = {}
# Hard stop for the walk: far beyond any real SD card's retention.
FULL_SCAN_LOOKBACK_DAYS = 3650


CardState = Literal["not_mounted", "at_risk", "waiting", "all_archived", "unknown"]

# A card with less than this share free is looping: the camera overwrites
# its oldest clip to make room for each new one. Above it, nothing on the
# card is being overwritten yet, so there's no deadline to report.
LOOPING_FREE_FRACTION = 0.10
# Days of recent recordings the card-window estimate averages over.
WINDOW_ESTIMATE_DAYS = 7


@dataclass(frozen=True, slots=True)
class CardStatus:
    """What a user wants to know about one camera's SD card: is everything
    on it archived, and if not, how long until the oldest unarchived clip is
    overwritten."""

    state: CardState
    waiting_count: int
    """Unarchived clips still on the card."""
    lost_count: int
    """Unarchived clips older than the card's oldest clip: overwritten."""
    oldest_waiting_utc: datetime | None
    oldest_on_card_utc: datetime | None
    window: timedelta | None
    """How much footage the card holds, from the sample's oldest clip, or
    estimated from capacity / recent recording rate when that's unknown."""
    window_estimated: bool
    safe_for: timedelta | None
    """Time left before the oldest waiting clip is overwritten. Only set on
    a looping (full) card with clips waiting."""
    used_fraction: float | None

    @property
    def alarm(self) -> bool:
        return self.state == "at_risk"


def record_storage_sample(
    *, repository: Repository, provider: CameraProvider, device_id: int
) -> None:
    """Runs on every archive run (scheduled, backfill, or manual), not on
    its own schedule."""
    storage = provider.storage_status()
    oldest_on_card = _find_oldest_recording_utc(provider)
    repository.record_storage_sample(
        device_id=device_id,
        total_gb=storage.total_gb,
        remain_gb=storage.remain_gb,
        formatted=storage.formatted,
        mounted=storage.mounted,
        oldest_recording_utc=oldest_on_card,
    )


def _find_oldest_recording_utc(provider: CameraProvider) -> datetime | None:
    now = datetime.now(UTC)
    oldest: datetime | None = None
    empty_streak = 0
    for days_back in range(FULL_SCAN_LOOKBACK_DAYS):
        to_utc = now - timedelta(days=days_back)
        recordings = provider.list_recordings(from_utc=to_utc - timedelta(days=1), to_utc=to_utc)
        if not recordings:
            empty_streak += 1
            if empty_streak >= EMPTY_DAYS_TO_STOP:
                break
            continue
        empty_streak = 0
        oldest = min(r.start_utc for r in recordings)
    return oldest


def card_status(
    *,
    repository: Repository,
    device_id: int,
    margin: float = DEFAULT_COVERAGE_MARGIN,
    now: datetime | None = None,
) -> CardStatus:
    """Alarms (`at_risk`) when the oldest clip ReoVault hasn't archived yet
    is within `margin` (default 20%) of the card's window from being
    overwritten. Clips already past the card's oldest clip count as `lost`,
    not as an alarm: nothing can be done for them, and they used to pin the
    old percentage at 0% forever."""
    now = now or datetime.now(UTC)
    sample = repository.latest_storage_sample(device_id)
    oldest_on_card = (
        _parse_or_none(sample.oldest_recording_utc)
        if sample and sample.oldest_recording_utc
        else None
    )
    backlog = repository.card_backlog(device_id, oldest_on_card)

    total = sample.total_gb if sample else None
    used_fraction = max(total - (sample.remain_gb or 0), 0.0) / total if sample and total else None
    window = now - oldest_on_card if oldest_on_card and oldest_on_card < now else None
    estimated = False
    if window is None and total:
        # ponytail: assumes a steady recording rate; a quiet week stretches it.
        since = now - timedelta(days=WINDOW_ESTIMATE_DAYS)
        per_day = repository.recorded_bytes_since(device_id, since) / WINDOW_ESTIMATE_DAYS
        if per_day > 0:
            window = timedelta(days=total * 2**30 / per_day)
            estimated = True

    looping = used_fraction is not None and used_fraction >= 1 - LOOPING_FREE_FRACTION
    safe_for = None
    if looping and window and backlog.oldest_waiting_utc:
        safe_for = max(backlog.oldest_waiting_utc + window - now, timedelta(0))

    state: CardState
    if sample is not None and sample.mounted is False:
        state = "not_mounted"
    elif safe_for is not None and window and safe_for <= window * margin:
        state = "at_risk"
    elif backlog.waiting_count:
        state = "waiting"
    elif sample is None and repository.totals(device_id).first_start_utc is None:
        state = "unknown"  # never sampled, never recorded: a fresh camera
    else:
        state = "all_archived"

    _log_alarm_change(
        device_id,
        state == "at_risk",
        safe_for_seconds=safe_for.total_seconds() if safe_for is not None else None,
        oldest_on_card_utc=oldest_on_card.isoformat() if oldest_on_card else None,
        oldest_waiting_utc=(
            backlog.oldest_waiting_utc.isoformat() if backlog.oldest_waiting_utc else None
        ),
    )
    return CardStatus(
        state=state,
        waiting_count=backlog.waiting_count,
        lost_count=backlog.lost_count,
        oldest_waiting_utc=backlog.oldest_waiting_utc,
        oldest_on_card_utc=oldest_on_card,
        window=window,
        window_estimated=estimated,
        safe_for=safe_for,
        used_fraction=used_fraction,
    )


def human_duration(d: timedelta | None) -> str:
    """'3 days', '5 hours', '20 minutes': the one unit a reader needs."""
    secs = d.total_seconds() if d else 0
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = round(secs / size)
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return "under a minute"


def _log_alarm_change(device_id: int, alarm: bool, **fields: object) -> None:
    if alarm == _logged_alarm.get(device_id, False):
        return
    _logged_alarm[device_id] = alarm
    if alarm:
        logger.error("health.coverage_alarm", device_id=device_id, **fields)
    else:
        logger.info("health.coverage_alarm_cleared", device_id=device_id)


def _parse_or_none(s: str) -> datetime | None:
    try:
        return parse_iso_utc(s)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class IntegrityScanResult:
    checked: list[int]
    failed: list[int]


def run_integrity_scan(
    *,
    repository: Repository,
    vault: VaultStore,
    device_id: int,
    sample_pct: float = 5.0,
    bucket_index: int | None = None,
) -> IntegrityScanResult:
    """Re-reads a rotating sample, re-verifying GCM tags and SHA-256, so bit
    rot surfaces while a downstream backup still holds a good copy.
    `bucket_index` defaults to the day of the year, so repeated weekly calls
    rotate through the full archived set over time without a persisted
    cursor."""
    bucket_count = max(round(100 / sample_pct), 1)
    if bucket_index is None:
        bucket_index = datetime.now(UTC).timetuple().tm_yday % bucket_count
    else:
        bucket_index = bucket_index % bucket_count

    rows = repository.sample_for_integrity_scan(
        device_id=device_id, bucket_count=bucket_count, bucket_index=bucket_index
    )
    checked: list[int] = []
    failed: list[int] = []
    for row in rows:
        checked.append(row.id)
        if row.vault_path is None or not vault.verify(row.vault_path):
            failed.append(row.id)
            logger.error(
                "health.integrity_scan_failed", recording_id=row.id, vault_path=row.vault_path
            )
    return IntegrityScanResult(checked=checked, failed=failed)
