"""Footage: month calendar -> day -> hour, single recordings, retry/verify,
and the Range-capable decrypting stream.

Routes keyed by `recording_id` deliberately skip device scoping: the id is a
global primary key, the row says which device it belongs to, and using the
*selected* device's timezone instead would render a plausible but wrong
local time for another camera's clip. They also work for a recording whose
device has since been disabled.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reovault.db.repository import RecordingRow, parse_iso_utc
from reovault.logging import get_logger
from reovault.timeline import local_day_bounds
from reovault.web.api.common import (
    Page,
    RecordingOut,
    encode_cursor,
    local_today,
    parse_cursor,
    parse_date,
    recording_out,
    tz_of_device,
)
from reovault.web.deps import AppState, Device, State
from reovault.web.ranges import ByteRange, parse_range
from reovault.web.streaming import StreamTruncatedError, iter_plaintext, safe_download_filename

logger = get_logger(__name__)

router = APIRouter(tags=["footage"])

_HOUR_PAGE = 200
_PROBLEMS = "__problems__"
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class CalendarDayOut(BaseModel):
    date: str
    count: int
    problems: int
    density: int  # 0 (empty) .. 4 (busiest day of the month)


class CalendarOut(BaseModel):
    month: str
    today: str
    days: list[CalendarDayOut]  # only days with at least one clip


class HourOut(BaseModel):
    hour: int
    total: int
    bytes: int


class DayOut(BaseModel):
    date: str
    today: str
    total: int
    total_bytes: int
    hours: list[HourOut]  # only hours with at least one clip
    available_types: list[str]


class VerifyOut(BaseModel):
    ok: bool
    recording: RecordingOut


def _filters(types: str) -> tuple[list[str] | None, str | None]:
    """`types` is one rec_type, or `__problems__` for failed/quarantined."""
    if types == _PROBLEMS:
        return ["failed", "quarantined"], None
    return None, types or None


@router.get("/calendar")
def get_calendar(dev: Device, st: State, month: str) -> CalendarOut:
    if not _MONTH_RE.match(month):
        raise HTTPException(422, "month is YYYY-MM.")
    year, mon = (int(x) for x in month.split("-"))
    month_start = date(year, mon, 1)
    next_month = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    from_utc, _ = local_day_bounds(month_start, dev.tz)
    _, to_utc = local_day_bounds(next_month - timedelta(days=1), dev.tz)
    buckets = st.repo.day_buckets(
        device_id=dev.device_id, from_utc=from_utc, to_utc=to_utc, tz=dev.tz
    )
    peak = max((b.total for b in buckets), default=0) or 1
    return CalendarOut(
        month=month,
        today=local_today(dev.tz).isoformat(),
        days=[
            CalendarDayOut(
                date=b.day,
                count=b.total,
                problems=b.problems,
                density=max(1, round(b.total / peak * 4)),
            )
            for b in buckets
            if b.total
        ],
    )


def _bucket_by_local_hour(rows: list[RecordingRow], tz: str) -> dict[int, HourOut]:
    zone = ZoneInfo(tz)
    out: dict[int, HourOut] = {}
    for r in rows:
        h = parse_iso_utc(r.start_utc).astimezone(zone).hour
        b = out.setdefault(h, HourOut(hour=h, total=0, bytes=0))
        b.total += 1
        if r.state == "archived":
            b.bytes += r.plaintext_size or 0
    return out


@router.get("/day")
def get_day(dev: Device, st: State, date_: str | None = None, types: str = "") -> DayOut:
    """`date_` defaults to the camera's own local today, so the dashboard
    can open Footage without first knowing the camera's timezone."""
    day = parse_date(date_) if date_ else local_today(dev.tz)
    from_utc, to_utc = local_day_bounds(day, dev.tz)
    states, rec_type = _filters(types)
    if states or rec_type:
        # A filter recomputes the per-hour totals, so the summary lines stay
        # honest after a chip toggle.
        rows = st.repo.recordings_in_window(
            device_id=dev.device_id,
            from_utc=from_utc,
            to_utc=to_utc,
            states=states,
            rec_type=rec_type,
            limit=100000,
        )
        hours = _bucket_by_local_hour(rows, dev.tz)
    else:
        hours = {
            b.hour: HourOut(hour=b.hour, total=b.total, bytes=b.bytes)
            for b in st.repo.hour_buckets(
                device_id=dev.device_id, from_utc=from_utc, to_utc=to_utc, tz=dev.tz
            )
        }
    shown = [hours[h] for h in sorted(hours) if hours[h].total]
    return DayOut(
        date=day.isoformat(),
        today=local_today(dev.tz).isoformat(),
        total=sum(h.total for h in shown),
        total_bytes=sum(h.bytes for h in shown),
        hours=shown,
        available_types=st.repo.distinct_rec_types(
            device_id=dev.device_id, from_utc=from_utc, to_utc=to_utc
        ),
    )


@router.get("/day/hour")
def get_day_hour(
    dev: Device,
    st: State,
    date_: str,
    hour: Annotated[int, Query(ge=0, le=23)],
    types: str = "",
    after: str = "",
) -> Page[RecordingOut]:
    day = parse_date(date_)
    from_utc, day_end = local_day_bounds(day, dev.tz)
    # Fetch the whole day and filter to the local hour here, rather than
    # recomputing a per-hour UTC window (which would duplicate the DST
    # segmentation inside Repository.hour_buckets). One day is bounded.
    states, rec_type = _filters(types)
    rows = st.repo.recordings_in_window(
        device_id=dev.device_id,
        from_utc=from_utc,
        to_utc=day_end,
        states=states,
        rec_type=rec_type,
        limit=100000,
    )
    zone = ZoneInfo(dev.tz)
    matching = sorted(
        (r for r in rows if parse_iso_utc(r.start_utc).astimezone(zone).hour == hour),
        key=lambda r: (r.start_utc, r.id),
    )
    cursor = parse_cursor(after)
    if cursor:
        matching = [r for r in matching if (r.start_utc, r.id) > cursor]
    page = matching[:_HOUR_PAGE]
    more = len(matching) > _HOUR_PAGE
    return Page[RecordingOut](
        items=[recording_out(st, r, dev.tz) for r in page],
        next=encode_cursor(page[-1].start_utc, page[-1].id) if more else None,
    )


def _get_row(st: AppState, recording_id: int) -> RecordingRow:
    row = st.repo.get(recording_id)
    if row is None:
        raise HTTPException(404, "Recording not found.")
    return row


@router.get("/recordings/{recording_id}")
def get_recording(recording_id: int, st: State) -> RecordingOut:
    row = _get_row(st, recording_id)
    return recording_out(st, row, tz_of_device(st, row.device_id))


@router.post("/recordings/{recording_id}/retry")
def retry_recording(recording_id: int, st: State) -> RecordingOut:
    """Makes a failed recording eligible for the next run's pickup; it is
    not re-downloaded on the spot (see Repository.retry_recording)."""
    _get_row(st, recording_id)
    st.repo.retry_recording(recording_id)
    row = _get_row(st, recording_id)
    st.bus.publish("problems", device_id=row.device_id)
    return recording_out(st, row, tz_of_device(st, row.device_id))


@router.post("/recordings/{recording_id}/verify")
def verify_recording(recording_id: int, st: State) -> VerifyOut:
    row = _get_row(st, recording_id)
    if row.vault_path is None:
        raise HTTPException(404, "Recording has no vault file.")
    # ponytail: inline synchronous verify, ~100ms at a ~20MB clip. A queue
    # only earns its keep if clip sizes grow a lot. Uses the shared fleet
    # vault so it works for a disabled device's recording too.
    ok = st.fleet.vault.verify(row.vault_path)
    if not ok:
        st.bus.publish("problems", device_id=row.device_id)
    return VerifyOut(ok=ok, recording=recording_out(st, row, tz_of_device(st, row.device_id)))


@router.get("/recordings/{recording_id}/stream", response_class=StreamingResponse)
@router.head("/recordings/{recording_id}/stream", include_in_schema=False)
def stream_recording(request: Request, recording_id: int, st: State, download: int = 0) -> Response:
    row = st.repo.get(recording_id)
    if row is None or row.state != "archived" or row.vault_path is None:
        raise HTTPException(404, "Recording not found.")
    size = row.plaintext_size
    if size is None:
        try:
            size = st.fleet.vault.describe(row.vault_path).plaintext_size
        except Exception:  # noqa: BLE001
            raise HTTPException(404, "Recording not found.") from None

    parsed = parse_range(request.headers.get("range"), size)
    disposition = "attachment" if download else "inline"
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "video/mp4",
        "Cache-Control": "private, no-store",
        # nginx honors this per-response, so a reverse proxy never spools a
        # Range request through a temp file.
        "X-Accel-Buffering": "no",
        "Content-Disposition": (
            f'{disposition}; filename="{safe_download_filename(row.remote_name)}"'
        ),
    }
    if parsed == "unsatisfiable":
        headers = {**base_headers, "Content-Range": f"bytes */{size}"}
        return Response(status_code=416, headers=headers)

    byte_range = parsed if isinstance(parsed, ByteRange) else ByteRange(0, size - 1)
    status_code = 206 if isinstance(parsed, ByteRange) else 200
    headers = {**base_headers, "Content-Length": str(byte_range.end - byte_range.start + 1)}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{size}"
    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers)

    try:
        gen = iter_plaintext(
            st.fleet.vault,
            row.vault_path,
            byte_range.start,
            byte_range.end,
            st.settings.web.stream_chunk_bytes,
        )
        first_chunk = next(gen, b"")
    except FileNotFoundError as exc:
        # The vault file is gone (e.g. reconciliation raced this request).
        # Same public answer as an unknown id; path logged server-side only.
        logger.error("web.stream_vault_file_missing", recording_id=recording_id, error=str(exc))
        raise HTTPException(404, "Recording not found.") from None
    except (StreamTruncatedError, OSError, ValueError) as exc:
        logger.error("web.stream_precheck_failed", recording_id=recording_id, error=str(exc))
        raise HTTPException(500, "internal error") from None

    def body() -> Iterator[bytes]:
        if first_chunk:
            yield first_chunk
        try:
            yield from gen
        except StreamTruncatedError as exc:
            logger.error("web.stream_truncated", recording_id=recording_id, offset=exc.offset)
            raise

    return StreamingResponse(body(), status_code=status_code, headers=headers)
