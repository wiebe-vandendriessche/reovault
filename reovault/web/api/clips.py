"""Browsing the index and getting clips back out of restic: a calendar, a
day grouped by hour, one clip, or a whole day as a zip. Plus the activity
log. Downloads stream straight from `restic dump`, no temp files."""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reovault.backup import MAX_ATTEMPTS
from reovault.db import Clip, local_day_bounds, parse_iso
from reovault.destinations import restic_for
from reovault.logging import get_logger
from reovault.restic import Restic
from reovault.web.api.cameras import RunOut, run_out
from reovault.web.deps import AppState, State, camera_or_404

logger = get_logger(__name__)
router = APIRouter(tags=["clips"])

_CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


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


def locate(st: AppState, clip: Clip) -> tuple[Restic, str] | None:
    """Where to read a clip from: a connected destination that holds its
    snapshot, preferring the primary, then local ones (no network), then the
    rest. (Restic for that camera on it, the snapshot's id there.)"""
    enabled = [d for d in st.settings.destinations if d.enabled]
    primary = st.settings.primary()
    order = sorted(enabled, key=lambda d: (d is not primary, d.repository.startswith("rclone:")))
    found = st.db.clip_location(clip, [d.name for d in order])
    if found is None:
        return None
    dest = st.settings.destination(found[0])
    assert dest is not None
    return restic_for(st.settings, clip.camera, dest), found[1]


def _dump(st: AppState, clip: Clip) -> Iterator[bytes]:
    """The clip's bytes from restic, or an aborted response: a broken dump
    must never look like a complete download."""
    assert clip.snapshot_id and clip.path
    where = locate(st, clip)
    if where is None:
        raise RuntimeError("that clip is not on a connected destination")
    restic, snapshot_id = where
    proc = restic.dump(snapshot_id, clip.path)
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
    if locate(st, clip) is None:
        raise HTTPException(404, "That clip is only on a destination that isn't connected now.")
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


class BatchOut(BaseModel):
    snapshot: str
    clips: int
    bytes: int
    download_secs: float | None
    upload_secs: float | None
    finished_at: datetime


class RunClipOut(ClipOut):
    outcome: str  # backed_up | failed | lost


class RunDetailOut(BaseModel):
    run: RunOut
    batches: list[BatchOut]
    clips: list[RunClipOut]
    truncated: bool


_RUN_CLIP_LIMIT = 500


@router.get("/activity/runs/{run_id}", responses={404: {}})
def run_detail(run_id: int, st: State) -> RunDetailOut:
    """One backup run: the batches it uploaded, and each clip it backed up,
    failed on or found lost. Read from the index (no destination contacted)."""
    run = st.db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Unknown run.")
    cfg = camera_or_404(st, run.camera)
    tz = ZoneInfo(cfg.timezone)
    end = parse_iso(run.finished_at) if run.finished_at else datetime.now(UTC)
    found = st.db.clips_touched(run.camera, parse_iso(run.started_at), end, _RUN_CLIP_LIMIT + 1)
    clips = []
    for c in found[:_RUN_CLIP_LIMIT]:
        outcome = {"backed_up": "backed_up", "lost": "lost"}.get(c.state, "failed")
        clips.append(RunClipOut(**clip_out(st, c, tz).model_dump(), outcome=outcome))
    return RunDetailOut(
        run=run_out(st, run),
        batches=[
            BatchOut(
                snapshot=b["snapshot_id"][:8],
                clips=b["clips"],
                bytes=b["bytes"],
                download_secs=b["download_secs"],
                upload_secs=b["upload_secs"],
                finished_at=parse_iso(b["finished_at"]),
            )
            for b in st.db.batches(run.id)
        ],
        clips=clips,
        truncated=len(found) > _RUN_CLIP_LIMIT,
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
