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

from reovault.db.repository import Repository, parse_iso_utc
from reovault.logging import get_logger
from reovault.providers.base import CameraProvider
from reovault.storage.vault import VaultStore

logger = get_logger(__name__)

DEFAULT_COVERAGE_MARGIN = 0.20

# How far back a "full search" looks to find the oldest recording still
# physically on the card. The card's own loop-overwrite bounds what actually
# comes back regardless of how wide this is; ~10 years comfortably exceeds
# any real SD card's retention.
FULL_SCAN_LOOKBACK_DAYS = 3650


@dataclass(frozen=True, slots=True)
class CoverageStatus:
    oldest_on_card_utc: datetime | None
    oldest_unarchived_utc: datetime | None
    margin_fraction: float | None
    """`(oldest_unarchived - oldest_on_card) / (now - oldest_on_card)`: how
    much of the card's current retention window is left before the
    loop-overwrite reaches our oldest not-yet-archived recording. `None`
    when there isn't enough data yet (no sample, or nothing unarchived, in
    which case there's nothing to be behind on)."""
    alarm: bool


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
    recordings = provider.list_recordings(
        from_utc=now - timedelta(days=FULL_SCAN_LOOKBACK_DAYS), to_utc=now
    )
    if not recordings:
        return None
    return min(r.start_utc for r in recordings)


def coverage_margin(
    *, repository: Repository, device_id: int, margin: float = DEFAULT_COVERAGE_MARGIN
) -> CoverageStatus:
    """Alarms when the oldest recording ReoVault hasn't archived yet is
    within `margin` (default 20%) of the card's own loop-overwrite horizon,
    i.e. the safety buffer before that recording gets overwritten
    unarchived is running out."""
    sample = repository.latest_storage_sample(device_id)
    oldest_on_card = (
        _parse_or_none(sample.oldest_recording_utc)
        if sample and sample.oldest_recording_utc
        else None
    )
    oldest_unarchived = repository.oldest_unarchived_start_utc(device_id)

    if oldest_on_card is None or oldest_unarchived is None:
        # Nothing to be behind on yet (fully caught up, or no sample taken).
        return CoverageStatus(oldest_on_card, oldest_unarchived, None, alarm=False)

    now = datetime.now(UTC)
    coverage_span = (now - oldest_on_card).total_seconds()
    if coverage_span <= 0:
        return CoverageStatus(oldest_on_card, oldest_unarchived, None, alarm=False)

    remaining_buffer = (oldest_unarchived - oldest_on_card).total_seconds()
    fraction = max(remaining_buffer, 0.0) / coverage_span
    alarm = fraction <= margin
    if alarm:
        logger.error(
            "health.coverage_alarm",
            device_id=device_id,
            margin_fraction=fraction,
            configured_margin=margin,
            oldest_on_card_utc=oldest_on_card.isoformat(),
            oldest_unarchived_utc=oldest_unarchived.isoformat(),
        )
    return CoverageStatus(oldest_on_card, oldest_unarchived, fraction, alarm=alarm)


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
