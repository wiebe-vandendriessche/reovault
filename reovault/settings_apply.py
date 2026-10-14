"""Turning a settings change into running state: device rows, the fleet of
archivers, and their scheduler jobs. Used at daemon start and by the
`ConfigStore` listener on every change, whether it came from the dashboard
or from a hand edit of reovault.toml, so both paths behave identically.
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from reovault.config import Settings
from reovault.db.repository import Repository
from reovault.fleet import Fleet
from reovault.logging import get_logger
from reovault.scheduler import (
    add_device_jobs,
    load_effective_schedule,
    remove_device_jobs,
    reschedule_device,
)

logger = get_logger(__name__)


def sync_devices(settings: Settings, repository: Repository, *, authoritative: bool) -> None:
    """Device rows follow `[[devices]]`: each entry is upserted, and with an
    authoritative (writable, two-way) config its `enabled` is applied and a
    camera missing from the file is disabled. Never deletes a row, so a
    camera's archived footage always stays browsable. Not authoritative
    (a read-only config mount): entries are only upserted, as before 0.4."""
    in_file: set[int] = set()
    for device in settings.devices:
        device_id = repository.upsert_device(
            alias=device.alias, channel=device.channel, timezone=device.timezone, name=device.name
        )
        in_file.add(device_id)
        if authoritative:
            repository.set_device_enabled(device_id, device.enabled)
    if not authoritative:
        return
    for row in repository.list_devices():
        if row.id not in in_file and row.enabled:
            repository.set_device_enabled(row.id, False)
            logger.info("settings.device_disabled_not_in_config", alias=row.alias)


def apply_change(
    old: Settings,
    new: Settings,
    *,
    repository: Repository,
    fleet: Fleet,
    scheduler: BackgroundScheduler | None,
    authoritative: bool,
) -> None:
    """Brings the fleet and the scheduler in line with `new`."""
    sync_devices(new, repository, authoritative=authoritative)
    enabled = {row.id: row for row in repository.list_devices(enabled_only=True)}
    old_by_alias = {(d.alias, d.channel): d for d in old.devices}
    new_by_alias = {(d.alias, d.channel): d for d in new.devices}

    for device_id in fleet.ids():
        if device_id not in enabled:
            fleet.remove(device_id)
            if scheduler is not None:
                remove_device_jobs(scheduler, device_id)

    for device_id, row in enabled.items():
        key = (row.alias, row.channel)
        before, after = old_by_alias.get(key), new_by_alias.get(key)
        # The provider bakes in the camera's timezone: re-create it.
        moved = before is not None and after is not None and before.timezone != after.timezone
        if moved:
            fleet.remove(device_id)
            if scheduler is not None:
                remove_device_jobs(scheduler, device_id)
        archiver = fleet.get(device_id)
        schedule = load_effective_schedule(new, repository, device_id=device_id)
        if archiver is None:
            archiver = fleet.add(row)
            if scheduler is not None:
                add_device_jobs(scheduler, archiver, schedule, device_timezone=row.timezone)
        elif scheduler is not None and schedule != load_effective_schedule(
            old, repository, device_id=device_id
        ):
            reschedule_device(
                scheduler, archiver, repository, schedule, device_timezone=row.timezone
            )
