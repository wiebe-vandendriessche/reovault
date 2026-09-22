"""View-model helpers. `redact_paths` is registered as a Jinja filter so
every error-string render goes through it: the realistic leak vector is
`archive_runs.error` / `recordings.last_error` text (an `OSError` message
can carry an absolute staging path), not template output.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from reovault.config import StorageConfig
from reovault.db.repository import parse_iso_utc


def redact_paths(text: str | None, storage: StorageConfig) -> str:
    if not text:
        return ""
    result = text
    for placeholder, path in (
        ("<vault>", storage.vault_dir),
        ("<staging>", storage.staging_dir),
        ("<config>", storage.config_dir),
        ("<key>", storage.master_key_path),
    ):
        result = result.replace(str(path), placeholder)
    return result


def local_dt(value: str | datetime | None, tz: str) -> str:
    """A run/schedule timestamp, in the device's own configured timezone,
    written out for a human (`20 Sep 2026, 21:00 CEST`) instead of a raw
    UTC ISO string (`2026-09-20T21:00`), which carries no timezone at all
    and reads as a machine log line, not a time a person can place. Takes
    either an ISO string straight from the database (`RunRow.started_at`)
    or an already-aware `datetime` (APScheduler's own `next_run_time`), so
    every timestamp in the app can go through the one filter."""
    if not value:
        return "-"
    dt = parse_iso_utc(value) if isinstance(value, str) else value
    zone: tzinfo
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - an unknown/invalid tz string degrades to UTC, never a 500
        zone = UTC
    return dt.astimezone(zone).strftime("%d %b %Y, %H:%M %Z")


def human_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
