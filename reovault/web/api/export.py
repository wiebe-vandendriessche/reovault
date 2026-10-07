"""Bulk export: a date range of one camera's archived clips, decrypted and
streamed as a zip. No temp files: `zipfile` writes into a buffer that the
response drains after every chunk, so memory stays at about one chunk no
matter how big the export is.

`ZIP_STORED`, not deflate: the clips are already-compressed video, so
deflating them only burns CPU. A corrupt clip aborts the transfer (the
browser reports the download as failed) rather than shipping a zip with a
silently short file inside.
"""

from __future__ import annotations

import io
import threading
import weakref
from collections.abc import Iterator
from datetime import date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reovault.db.repository import RecordingRow, parse_iso_utc
from reovault.logging import get_logger
from reovault.timeline import local_day_bounds
from reovault.web.api.common import parse_date
from reovault.web.deps import AppState, Device, DeviceCtx, State
from reovault.web.streaming import iter_plaintext, safe_download_filename

logger = get_logger(__name__)

router = APIRouter(prefix="/export", tags=["export"])

MAX_DAYS = 31
MAX_CLIPS = 5000
# ponytail: one export at a time, process-wide. Each one is a full-speed
# decrypt of the whole range; two at once on a small box starve the
# archiver. Per-user slots only matter with more than one user.
_export_lock = threading.Lock()


class ExportPreviewOut(BaseModel):
    count: int
    bytes: int


class _Sink(io.RawIOBase):
    """Write-only, non-seekable: `zipfile` then writes data descriptors
    after each member instead of seeking back to patch its header."""

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


def _select(
    st: AppState, dev: DeviceCtx, from_: str, to: str, types: str
) -> tuple[date, date, list[RecordingRow]]:
    start, end = parse_date(from_), parse_date(to)
    if start > end:
        raise HTTPException(422, "The start date must not be after the end date.")
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(422, f"Export at most {MAX_DAYS} days at a time.")
    from_utc, _ = local_day_bounds(start, dev.tz)
    _, to_utc = local_day_bounds(end, dev.tz)
    rows = st.repo.recordings_in_window(
        device_id=dev.device_id,
        from_utc=from_utc,
        to_utc=to_utc,
        states=["archived"],
        rec_type=types or None,
        limit=MAX_CLIPS + 1,
    )
    rows = [r for r in rows if r.vault_path is not None]
    if len(rows) > MAX_CLIPS:
        raise HTTPException(422, f"More than {MAX_CLIPS} clips; pick a shorter range.")
    return start, end, rows


@router.get("/preview")
def preview(dev: Device, st: State, from_: str, to: str, types: str = "") -> ExportPreviewOut:
    _, _, rows = _select(st, dev, from_, to, types)
    return ExportPreviewOut(count=len(rows), bytes=sum(r.plaintext_size or 0 for r in rows))


@router.get("", response_class=StreamingResponse, responses={409: {}, 422: {}})
def export(dev: Device, st: State, from_: str, to: str, types: str = "") -> StreamingResponse:
    import zipfile

    start, end, rows = _select(st, dev, from_, to, types)
    if not rows:
        raise HTTPException(422, "No archived clips in that range.")
    if not _export_lock.acquire(blocking=False):
        raise HTTPException(409, "Another export is already running.")
    zone = ZoneInfo(dev.tz)
    vault = st.fleet.vault
    chunk = st.settings.web.stream_chunk_bytes
    device = st.repo.get_device(dev.device_id)
    alias = safe_download_filename(device.alias if device else "camera")[: -len(".mp4")]

    released = threading.Event()

    def release() -> None:
        if not released.is_set():
            released.set()
            _export_lock.release()

    def body() -> Iterator[bytes]:
        sink = _Sink()
        try:
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED) as zf:
                for row in rows:
                    assert row.vault_path is not None
                    local = parse_iso_utc(row.start_utc).astimezone(zone)
                    info = zipfile.ZipInfo(
                        f"{local:%Y-%m-%d}/{local:%H%M%S}_{row.id}_"
                        f"{safe_download_filename(row.remote_name)}",
                        date_time=local.timetuple()[:6],
                    )
                    size = row.plaintext_size or vault.describe(row.vault_path).plaintext_size
                    with zf.open(info, "w", force_zip64=True) as member:
                        for data in iter_plaintext(vault, row.vault_path, 0, size - 1, chunk):
                            member.write(data)
                            yield sink.drain()
                    yield sink.drain()
            yield sink.drain()  # central directory
        except Exception as exc:
            logger.error("web.export_failed", device_id=dev.device_id, error=str(exc))
            raise
        finally:
            release()

    stream = body()
    # A generator that never started (client gone before the first byte)
    # never runs its `finally`; this releases the slot when it's collected.
    weakref.finalize(stream, release)
    filename = f"reovault_{alias}_{start:%Y%m%d}-{end:%Y%m%d}.zip"
    return StreamingResponse(
        stream,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
        },
    )
