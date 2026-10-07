"""Global, rolling retention: keeps the vault under an age limit and/or a
size cap by pruning the oldest archived clips first, across every device.

A pruned clip keeps its DB row in state `pruned` (see
`Repository.mark_pruned`): dedup is the row alone, so deleting it would let
the next scan or backfill re-download a clip still on the SD card.

The policy lives in `app_settings` under `retention`, seeded from
`reovault.toml`'s `[retention]` table and editable from the dashboard,
unless any `REOVAULT_RETENTION__*` env var pins it, the same scheme as the
schedule (see `scheduler.load_effective_schedule`).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from reovault.config import RetentionConfig, Settings
from reovault.db.repository import RecordingRow, Repository, parse_iso_utc
from reovault.logging import get_logger
from reovault.storage.vault import VaultStore

logger = get_logger(__name__)

RETENTION_SETTING_KEY = "retention"
_RETENTION_ENV_PREFIX = "REOVAULT_RETENTION__"
_BATCH = 500
GB = 1024**3


def archiver_hook(
    settings: Settings, repository: Repository, vault: VaultStore
) -> Callable[[], PruneResult]:
    """The `Archiver.retention` callable. Re-reads the policy on every call
    so a dashboard edit applies from the next clip on."""
    return lambda: enforce(repository, vault, load_effective_retention(settings, repository))


def is_retention_env_pinned() -> bool:
    return any(k.startswith(_RETENTION_ENV_PREFIX) for k in os.environ)


def load_effective_retention(settings: Settings, repository: Repository) -> RetentionConfig:
    if is_retention_env_pinned():
        return settings.retention
    stored = repository.get_setting(RETENTION_SETTING_KEY)
    if stored is None:
        # Not logged: this also runs inside CLI commands whose stdout is JSON.
        repository.set_setting(RETENTION_SETTING_KEY, settings.retention.model_dump_json())
        return settings.retention
    return RetentionConfig.model_validate_json(stored)


def save_retention(repository: Repository, policy: RetentionConfig) -> None:
    repository.set_setting(RETENTION_SETTING_KEY, policy.model_dump_json())


@dataclass
class PruneResult:
    count: int = 0
    bytes: int = 0


def _over_limit(
    repository: Repository, policy: RetentionConfig, now: datetime
) -> Iterator[RecordingRow]:
    """Archived rows the policy wants gone, oldest first. Stops at the first
    row that is both young enough and fits under the cap: rows are ordered
    by `start_utc`, so nothing after it can be over either limit. Keyset
    paging makes this correct whether or not the caller prunes as it goes,
    which is what lets `preview` reuse it."""
    if policy.max_age_days is None and policy.max_vault_gb is None:
        return
    cutoff = now - timedelta(days=policy.max_age_days) if policy.max_age_days else None
    cap = int(policy.max_vault_gb * GB) if policy.max_vault_gb else None
    remaining = repository.archived_ciphertext_total()
    after: tuple[str, int] | None = None
    while True:
        rows = repository.oldest_archived(limit=_BATCH, after=after)
        if not rows:
            return
        for row in rows:
            too_old = cutoff is not None and parse_iso_utc(row.start_utc) < cutoff
            too_big = cap is not None and remaining > cap
            if not (too_old or too_big):
                return
            remaining -= row.ciphertext_size or 0
            yield row
        after = (rows[-1].start_utc, rows[-1].id)


def preview(
    repository: Repository, policy: RetentionConfig, now: datetime | None = None
) -> PruneResult:
    """What `enforce` would delete right now, without deleting anything."""
    result = PruneResult()
    for row in _over_limit(repository, policy, now or datetime.now(UTC)):
        result.count += 1
        result.bytes += row.ciphertext_size or 0
    return result


def enforce(
    repository: Repository,
    vault: VaultStore,
    policy: RetentionConfig,
    now: datetime | None = None,
) -> PruneResult:
    """Prune until the policy holds. File first, then row: a crash in
    between leaves an `archived` row with no file, which the next call
    picks up again (it is still the oldest) and `vault.delete` tolerates.
    The other order would leave an orphan file that reconciliation could
    mistake for an interrupted archive."""
    result = PruneResult()
    for row in _over_limit(repository, policy, now or datetime.now(UTC)):
        if row.vault_path is not None:
            vault.delete(row.vault_path)
        repository.mark_pruned(row.id)
        result.count += 1
        result.bytes += row.ciphertext_size or 0
    if result.count:
        logger.info("retention.pruned", count=result.count, bytes=result.bytes)
    return result
