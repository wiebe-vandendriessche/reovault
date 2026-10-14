"""Response models shared by several routers, and the row -> model
converters. Every error string goes through `AppState.redact`."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel

from reovault.db.repository import RecordingRow, RunRow, parse_iso_utc
from reovault.web.deps import AppState

# Plain-English sentences for the Problems tab: one entry per `ErrorClass`,
# since "protocol" or "exit code -2" means nothing to someone who isn't
# reading reolink-cli's source.
ERROR_CLASS_TEXT = {
    "input": "ReoVault sent reolink-cli a request it rejected.",
    "network": "Couldn't reach the camera over the network.",
    "auth": "The camera rejected the stored credentials.",
    "device": "The camera reported a problem on its end.",
    "protocol": "The camera's response didn't match what ReoVault expected.",
    "local": "Something on the ReoVault server itself failed (disk, permissions, or the gateway).",
}


class RecordingOut(BaseModel):
    id: int
    device_id: int
    remote_name: str
    start_utc: datetime
    local_time: str  # "HH:MM" in the device's timezone
    local_datetime: str  # full local timestamp, for titles and details
    duration_s: float | None
    types: list[str]
    state: str
    plaintext_size: int | None
    ciphertext_size: int | None
    remote_size: int | None
    plaintext_sha256: str | None
    archived_at: datetime | None
    attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    last_error_class: str | None
    last_error_class_text: str | None
    last_error_detail: str | None


class RunOut(BaseModel):
    id: int
    device_id: int | None
    trigger: str
    window_from_utc: datetime | None
    window_to_utc: datetime | None
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    discovered: int
    downloaded: int
    skipped_dup: int
    failed: int
    bytes_archived: int
    error: str | None


class Page[T](BaseModel):
    """Keyset-paginated list. `next` is the opaque cursor for the following
    page, `None` on the last one."""

    items: list[T]
    next: str | None


def _dt(s: str | None) -> datetime | None:
    return parse_iso_utc(s) if s else None


def recording_out(st: AppState, row: RecordingRow, tz: str) -> RecordingOut:
    local = parse_iso_utc(row.start_utc).astimezone(ZoneInfo(tz))
    return RecordingOut(
        id=row.id,
        device_id=row.device_id,
        remote_name=row.remote_name,
        start_utc=parse_iso_utc(row.start_utc),
        local_time=local.strftime("%H:%M"),
        local_datetime=local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        duration_s=row.duration_s,
        types=[t for t in (row.rec_type or "").split(",") if t],
        state=row.state,
        plaintext_size=row.plaintext_size,
        ciphertext_size=row.ciphertext_size,
        remote_size=row.remote_size,
        plaintext_sha256=row.plaintext_sha256,
        archived_at=_dt(row.archived_at),
        attempts=row.attempts,
        next_attempt_at=_dt(row.next_attempt_at),
        last_error=st.redact(row.last_error),
        last_error_class=row.last_error_class,
        last_error_class_text=ERROR_CLASS_TEXT.get(row.last_error_class or ""),
        last_error_detail=st.redact(row.last_error_detail),
    )


def run_out(st: AppState, row: RunRow) -> RunOut:
    return RunOut(
        id=row.id,
        device_id=row.device_id,
        trigger=row.trigger,
        window_from_utc=_dt(row.window_from_utc),
        window_to_utc=_dt(row.window_to_utc),
        started_at=parse_iso_utc(row.started_at),
        finished_at=_dt(row.finished_at),
        outcome=row.outcome,
        discovered=row.discovered,
        downloaded=row.downloaded,
        skipped_dup=row.skipped_dup,
        failed=row.failed,
        bytes_archived=row.bytes_archived,
        error=st.redact(row.error),
    )


def tz_of_device(st: AppState, device_id: int | None) -> str:
    row = st.repo.get_device(device_id) if device_id is not None else None
    return row.timezone if row is not None else "UTC"


def local_today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "Dates are YYYY-MM-DD.") from None


def encode_cursor(start_utc: str, rec_id: int) -> str:
    return f"{start_utc},{rec_id}"


def parse_cursor(s: str | None) -> tuple[str, int] | None:
    if not s or "," not in s:
        return None
    ts, _, id_str = s.rpartition(",")
    try:
        return (ts, int(id_str))
    except ValueError:
        return None


def utcnow() -> datetime:
    return datetime.now(UTC)
