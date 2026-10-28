"""Browsing the index and getting clips back out of restic: a calendar, a
day grouped by hour, one clip, or a whole day as a zip. Plus the activity
log. Downloads stream straight from `restic dump`, no temp files."""

from __future__ import annotations

import io
import os
import re
import threading
import weakref
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reovault.backup import MAX_ATTEMPTS
from reovault.db import Clip, local_day_bounds, parse_iso
from reovault.logging import get_logger
from reovault.restic import Restic
from reovault.web.api.cameras import RunOut, run_out
from reovault.web.deps import AppState, State, camera_or_404
from reovault.worker import restic_env

logger = get_logger(__name__)
router = APIRouter(tags=["clips"])

_CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
# ponytail: one zip at a time, process-wide; each is a full-speed restore.
_zip_lock = threading.Lock()


def safe_filename(name: str) -> str:
    """From the clip's card name only; also neutralises header injection."""
    return f"{_UNSAFE.sub('_', name)[:100] or 'clip'}.mp4"


class ClipOut(BaseModel):
    id: int
    camera: str
    name: str
    start: datetime  # camera-local, with offset
    end: datetime | None
    size: int | None
    rec_type: str | None
    state: str
    attempts: int
    last_error: str | None


def clip_out(st: AppState, c: Clip, tz: ZoneInfo) -> ClipOut:
    return ClipOut(
        id=c.id,
        camera=c.camera,
        name=c.name,
        start=parse_iso(c.start_utc).astimezone(tz),
        end=parse_iso(c.end_utc).astimezone(tz) if c.end_utc else None,
        size=c.size,
        rec_type=c.rec_type,
        state=c.state,
        attempts=c.attempts,
        last_error=st.redact(c.last_error),
    )


def _types_match(clip: Clip, types: set[str]) -> bool:
    return not types or bool(types & set((clip.rec_type or "").split(",")))


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"Not a date: {value!r}") from None


class CalendarDayOut(BaseModel):
    day: date
    backed_up: int
    pending: int
    lost: int


class CalendarOut(BaseModel):
    month: str
    today: date
    days: list[CalendarDayOut]


@router.get("/clips/calendar")
def calendar(camera: str, month: str, st: State) -> CalendarOut:
    cfg = camera_or_404(st, camera)
    tz = ZoneInfo(cfg.timezone)
    first = _parse_date(f"{month}-01")
    nxt = (first + timedelta(days=32)).replace(day=1)
    start, _ = local_day_bounds(first, cfg.timezone)
    end, _ = local_day_bounds(nxt, cfg.timezone)
    days: dict[date, CalendarDayOut] = {}
    for c in st.db.clips_between(camera, start, end):
        d = parse_iso(c.start_utc).astimezone(tz).date()
        row = days.setdefault(d, CalendarDayOut(day=d, backed_up=0, pending=0, lost=0))
        if c.state == "backed_up":
            row.backed_up += 1
        elif c.state == "pending":
            row.pending += 1
        elif c.state == "lost":
            row.lost += 1
    return CalendarOut(
        month=month,
        today=datetime.now(tz).date(),
        days=sorted(days.values(), key=lambda r: r.day),
    )


class HourOut(BaseModel):
    hour: int
    clips: list[ClipOut]


class DayOut(BaseModel):
    day: date
    today: date
    types: list[str]
    hours: list[HourOut]


def _day_clips(st: AppState, camera: str, day: date) -> tuple[ZoneInfo, list[Clip]]:
    cfg = camera_or_404(st, camera)
    start, end = local_day_bounds(day, cfg.timezone)
    return ZoneInfo(cfg.timezone), st.db.clips_between(camera, start, end)


@router.get("/clips/day")
def day(camera: str, st: State, date_: str | None = None, types: str = "") -> DayOut:
    tz0 = ZoneInfo(camera_or_404(st, camera).timezone)
    today = datetime.now(tz0).date()
    d = _parse_date(date_) if date_ else today
    tz, clips = _day_clips(st, camera, d)
    wanted = {t for t in types.split(",") if t}
    all_types = sorted({t for c in clips for t in (c.rec_type or "").split(",") if t})
    hours: dict[int, list[ClipOut]] = {}
    for c in clips:
        if _types_match(c, wanted):
            out = clip_out(st, c, tz)
            hours.setdefault(out.start.hour, []).append(out)
    return DayOut(
        day=d,
        today=today,
        types=all_types,
        hours=[HourOut(hour=h, clips=cs) for h, cs in sorted(hours.items(), reverse=True)],
    )


def _restic_for_clip(st: AppState, clip: Clip) -> Restic:
    """The repository the clip was backed up to, even after a destination
    change."""
    assert clip.repository is not None
    env = {**os.environ, **restic_env(st.settings)}
    return Restic(st.settings.tools.restic, clip.repository, env)


def _dump(st: AppState, clip: Clip) -> Iterator[bytes]:
    """The clip's bytes from restic, or an aborted response: a broken dump
    must never look like a complete download."""
    assert clip.snapshot_id and clip.path
    proc = _restic_for_clip(st, clip).dump(clip.snapshot_id, clip.path)
    assert proc.stdout is not None
    try:
        while data := proc.stdout.read(_CHUNK):
            yield data
        if proc.wait() != 0:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RuntimeError(f"restic dump failed: {err[-500:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _backed_up(st: AppState, clip_id: int) -> Clip:
    clip = st.db.get_clip(clip_id)
    if clip is None or clip.state != "backed_up" or not clip.snapshot_id or not clip.path:
        raise HTTPException(404, "That clip isn't in the backup.")
    return clip


@router.get("/clips/{clip_id}/download", response_class=StreamingResponse)
def download(clip_id: int, st: State) -> StreamingResponse:
    clip = _backed_up(st, clip_id)
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_filename(clip.name)}"',
        "Cache-Control": "private, no-store",
        "X-Accel-Buffering": "no",
    }
    if clip.size is not None:
        headers["Content-Length"] = str(clip.size)
    return StreamingResponse(_dump(st, clip), media_type="video/mp4", headers=headers)


class _Sink(io.RawIOBase):
    """Write-only and non-seekable, so `zipfile` streams data descriptors
    instead of seeking back: memory stays at about one chunk."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b: bytes) -> int:  # type: ignore[override]
        self.buf += b
        return len(b)

    def drain(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


@router.get("/clips/day.zip", response_class=StreamingResponse, responses={409: {}, 422: {}})
def day_zip(camera: str, date_: str, st: State, types: str = "") -> StreamingResponse:
    d = _parse_date(date_)
    tz, clips = _day_clips(st, camera, d)
    wanted = {t for t in types.split(",") if t}
    chosen = [c for c in clips if c.state == "backed_up" and _types_match(c, wanted)]
    if not chosen:
        raise HTTPException(422, "No backed-up clips on that day.")
    if not _zip_lock.acquire(blocking=False):
        raise HTTPException(409, "Another download is already being prepared.")
    released = threading.Event()

    def release() -> None:
        if not released.is_set():
            released.set()
            _zip_lock.release()

    def body() -> Iterator[bytes]:
        sink = _Sink()
        try:
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED) as zf:
                for c in chosen:
                    local = parse_iso(c.start_utc).astimezone(tz)
                    info = zipfile.ZipInfo(
                        f"{local:%H%M%S}_{safe_filename(c.name)}", date_time=local.timetuple()[:6]
                    )
                    with zf.open(info, "w", force_zip64=True) as member:
                        for data in _dump(st, c):
                            member.write(data)
                            yield sink.drain()
            yield sink.drain()  # the central directory
        except Exception as exc:
            logger.error("web.zip_failed", camera=camera, error=str(exc))
            raise
        finally:
            release()

    zip_name = f"reovault_{safe_filename(camera)[:-4]}_{d:%Y%m%d}.zip"
    stream = body()
    weakref.finalize(stream, release)  # a never-started generator never runs `finally`
    return StreamingResponse(
        stream,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
        },
    )


class ActivityOut(BaseModel):
    runs: list[RunOut]
    failing: list[ClipOut]
    lost: list[ClipOut]


@router.get("/activity")
def activity(st: State, camera: str | None = None) -> ActivityOut:
    """Recent runs, clips that keep failing, and clips lost in the last week."""
    cams = [camera_or_404(st, camera)] if camera else st.settings.cameras
    since = datetime.now(UTC) - timedelta(days=7)
    failing: list[ClipOut] = []
    lost: list[ClipOut] = []
    for cfg in cams:
        tz = ZoneInfo(cfg.timezone)
        failing += [clip_out(st, c, tz) for c in st.db.failing(cfg.alias, MAX_ATTEMPTS)]
        lost += [clip_out(st, c, tz) for c in st.db.lost_clips(cfg.alias, since)]
    return ActivityOut(
        runs=[run_out(st, r) for r in st.db.runs(camera, limit=100)],
        failing=failing,
        lost=lost,
    )
