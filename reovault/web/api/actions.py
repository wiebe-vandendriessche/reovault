"""Manual controls. Each answers with the device's current activity, so the
dashboard can show "queued" immediately. A click while a run is in progress
queues behind it rather than being rejected."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from reovault.scheduler import queue_manual_archive, queue_manual_backfill, queue_manual_reconcile
from reovault.web.api.health import MAX_BACKFILL_DAYS, ActivityOut, activity_out
from reovault.web.deps import AppState, Device, State

router = APIRouter(prefix="/actions", tags=["actions"])


class BackfillIn(BaseModel):
    # Wall-clock "YYYY-MM-DDTHH:MM" in the camera's own timezone, exactly
    # what an `<input type="datetime-local">` produces.
    from_local: str
    to_local: str


def _require_scheduler(st: AppState) -> None:
    if st.scheduler is None:
        raise HTTPException(503, "The scheduler isn't running on this server.")


def _local_to_utc(s: str, tz: str) -> datetime:
    try:
        naive = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "Enter both a start and an end.") from None
    if naive.tzinfo is not None:
        raise HTTPException(422, "Times are local to the camera, without an offset.")
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


@router.post("/run")
def action_run(dev: Device, st: State) -> ActivityOut:
    _require_scheduler(st)
    assert st.scheduler is not None
    now = datetime.now(UTC)
    # Same window as `archive_overlap_hours`'s default: a manual run doesn't
    # try to be smarter than the scheduled one.
    queue_manual_archive(st.scheduler, dev.archiver, from_utc=now - timedelta(hours=48), to_utc=now)
    st.bus.publish("activity", device_id=dev.device_id)
    return activity_out(st, dev)


@router.post("/stop")
def action_stop(dev: Device, st: State) -> ActivityOut:
    """Cooperative: the Archiver checks the flag between recordings, so a
    fetch or encrypt in flight finishes or fails on its own rather than
    being torn down mid-write. The run then closes out as `aborted`."""
    if st.repo.running_run(dev.device_id) is not None:
        dev.archiver.request_cancel()
    st.bus.publish("activity", device_id=dev.device_id)
    return activity_out(st, dev)


@router.post("/backfill")
def action_backfill(body: BackfillIn, dev: Device, st: State) -> ActivityOut:
    _require_scheduler(st)
    assert st.scheduler is not None
    from_utc = _local_to_utc(body.from_local, dev.tz)
    to_utc = min(_local_to_utc(body.to_local, dev.tz), datetime.now(UTC))
    if from_utc >= to_utc:
        raise HTTPException(422, "The start must be before the end (and in the past).")
    if to_utc - from_utc > timedelta(days=MAX_BACKFILL_DAYS):
        raise HTTPException(422, f"Range too wide; max {MAX_BACKFILL_DAYS} days.")
    queue_manual_backfill(st.scheduler, dev.archiver, from_utc=from_utc, to_utc=to_utc)
    st.bus.publish("activity", device_id=dev.device_id)
    return activity_out(st, dev)


@router.post("/reconcile")
def action_reconcile(dev: Device, st: State) -> ActivityOut:
    _require_scheduler(st)
    assert st.scheduler is not None
    queue_manual_reconcile(st.scheduler, dev.archiver)
    st.bus.publish("activity", device_id=dev.device_id)
    return activity_out(st, dev)
