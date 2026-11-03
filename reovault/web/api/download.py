"""Download backed-up footage for a span of time: an hour, a day, several days;
one camera or several; everything or only some detection types. One zip, a
folder per camera, streamed straight out of restic (no temporary files)."""

from __future__ import annotations

import threading
import weakref
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reovault.config import CameraConfig
from reovault.db import Clip, parse_iso
from reovault.logging import get_logger
from reovault.web.api.clips import _dump, _Sink, _types_match, locate, safe_filename
from reovault.web.deps import AppState, State, camera_or_404

logger = get_logger(__name__)
router = APIRouter(prefix="/download", tags=["download"])

MAX_CLIPS = 10_000
FromQ = Annotated[str, Query(alias="from")]
# ponytail: one zip at a time, process-wide; each one reads at full speed.
_zip_lock = threading.Lock()


def _instant(value: str, tz: ZoneInfo) -> datetime:
    """`2026-11-02T17:00` is that wall-clock time on the camera's own clock; a
    value with an offset or Z is an exact instant."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"Not a date and time: {value!r}") from None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=tz).astimezone(UTC)


@dataclass
class _Pick:
    camera: CameraConfig
    clips: list[Clip]
    slow: int  # only on a destination other than the fastest one connected
    unreachable: int  # backed up, but only on a destination that isn't connected now


def _select(st: AppState, cameras: str, from_: str, to: str, types: str) -> list[_Pick]:
    aliases = [a for a in cameras.split(",") if a]
    if not aliases:
        raise HTTPException(422, "Pick at least one camera.")
    wanted = {t for t in types.split(",") if t}
    out: list[_Pick] = []
    for alias in aliases:
        cam = camera_or_404(st, alias)
        tz = ZoneInfo(cam.timezone)
        start, end = _instant(from_, tz), _instant(to, tz)
        if start >= end:
            raise HTTPException(422, "The start must be before the end.")
        chosen, slow, unreachable = [], 0, 0
        for clip in st.db.clips_between(alias, start, end):
            if clip.state != "backed_up" or not clip.path or not _types_match(clip, wanted):
                continue
            where = locate(st, clip)
            if where is None:
                unreachable += 1
                continue
            chosen.append(clip)
            slow += where[0].repository.startswith("rclone:")
        out.append(_Pick(cam, chosen, slow, unreachable))
    if sum(len(p.clips) for p in out) > MAX_CLIPS:
        raise HTTPException(422, f"More than {MAX_CLIPS} clips: pick a shorter span.")
    return out


class PickOut(BaseModel):
    camera: str
    clips: int
    bytes: int
    on_slow_destination: int
    on_unconnected_destination: int


class DownloadPreviewOut(BaseModel):
    clips: int
    bytes: int
    cameras: list[PickOut]


@router.get("/preview")
def preview(cameras: str, from_: FromQ, to: str, st: State, types: str = "") -> DownloadPreviewOut:
    """How much a download would hold, so the dialog can show it first."""
    picks = _select(st, cameras, from_, to, types)
    cams = [
        PickOut(
            camera=p.camera.alias,
            clips=len(p.clips),
            bytes=sum(c.size or 0 for c in p.clips),
            on_slow_destination=p.slow,
            on_unconnected_destination=p.unreachable,
        )
        for p in picks
    ]
    return DownloadPreviewOut(
        clips=sum(c.clips for c in cams), bytes=sum(c.bytes for c in cams), cameras=cams
    )


@router.get("/zip", response_class=StreamingResponse, responses={409: {}, 422: {}})
def download_zip(
    cameras: str, from_: FromQ, to: str, st: State, types: str = ""
) -> StreamingResponse:
    picks = [p for p in _select(st, cameras, from_, to, types) if p.clips]
    if not picks:
        raise HTTPException(422, "Nothing backed up in that span.")
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
                for pick in picks:
                    tz = ZoneInfo(pick.camera.timezone)
                    folder = safe_filename(pick.camera.alias)[:-4]
                    for c in pick.clips:
                        local = parse_iso(c.start_utc).astimezone(tz)
                        info = zipfile.ZipInfo(
                            f"{folder}/{local:%Y-%m-%d}/{local:%H%M%S}_{safe_filename(c.name)}",
                            date_time=local.timetuple()[:6],
                        )
                        with zf.open(info, "w", force_zip64=True) as member:
                            for data in _dump(st, c):
                                member.write(data)
                                yield sink.drain()
            yield sink.drain()  # the central directory
        except Exception as exc:
            logger.error("web.zip_failed", error=str(exc))
            raise
        finally:
            release()

    first, last = from_[:10], to[:10]
    name = f"reovault_{first}_{last}.zip" if first != last else f"reovault_{first}.zip"
    stream = body()
    weakref.finalize(stream, release)  # a never-started generator never runs `finally`
    return StreamingResponse(
        stream,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename(name)[:-4]}.zip"',
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
        },
    )
