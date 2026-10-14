"""Run history, run detail, upcoming scheduled runs, and the Problems list."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from reovault.scheduler import next_run_times
from reovault.web.api.common import (
    Page,
    RecordingOut,
    RunOut,
    encode_cursor,
    parse_cursor,
    recording_out,
    run_out,
)
from reovault.web.deps import Device, State

router = APIRouter(tags=["runs"])

JOB_LABELS = {
    "scheduled_archive": "Download new clips",
    "deep_backfill": "Weekly deep catch-up",
    "reconcile": "Check the vault against the database",
    "integrity_scan": "Spot-check stored clips for corruption",
}


class ScheduledRunOut(BaseModel):
    job: str
    label: str
    next_run_utc: datetime | None


class RunsOut(BaseModel):
    items: list[RunOut]
    next: str | None  # `before_id` for the next page
    scheduled: list[ScheduledRunOut]
    timezone: str


class RunDetailOut(BaseModel):
    run: RunOut
    timezone: str


@router.get("/runs")
def list_runs(dev: Device, st: State, before_id: int | None = None) -> RunsOut:
    size = st.settings.web.page_size
    # One extra row says whether a next page exists, so a full last page
    # doesn't hand out a cursor to an empty one.
    rows = st.repo.recent_runs(device_id=dev.device_id, limit=size + 1, before_id=before_id)
    more, rows = len(rows) > size, rows[:size]
    times = next_run_times(st.scheduler).get(dev.device_id, {}) if st.scheduler else {}
    return RunsOut(
        items=[run_out(st, r) for r in rows],
        next=str(rows[-1].id) if more else None,
        scheduled=[
            ScheduledRunOut(job=job, label=label, next_run_utc=times.get(job))
            for job, label in JOB_LABELS.items()
        ],
        timezone=dev.tz,
    )


@router.get("/runs/{run_id}")
def get_run(run_id: int, st: State) -> RunDetailOut:
    # No device scoping: run_id is a global key; its own device's timezone
    # is looked up from the row.
    row = st.repo.get_run(run_id)
    if row is None:
        raise HTTPException(404, "Run not found.")
    device = st.repo.get_device(row.device_id) if row.device_id is not None else None
    return RunDetailOut(run=run_out(st, row), timezone=device.timezone if device else "UTC")


class RetriedOut(BaseModel):
    count: int


@router.post("/problems/retry")
def retry_all_problems(dev: Device, st: State) -> RetriedOut:
    """Re-queues every failed or quarantined recording of this camera for
    the next run, the bulk form of `POST /recordings/{id}/retry`."""
    count = st.repo.retry_all_problems(dev.device_id)
    st.bus.publish("problems", device_id=dev.device_id)
    return RetriedOut(count=count)


@router.get("/problems")
def list_problems(dev: Device, st: State, after: str = "") -> Page[RecordingOut]:
    size = st.settings.web.page_size
    rows = st.repo.problems(device_id=dev.device_id, limit=size + 1, after=parse_cursor(after))
    more, rows = len(rows) > size, rows[:size]
    return Page[RecordingOut](
        items=[recording_out(st, r, dev.tz) for r in rows],
        next=encode_cursor(rows[-1].start_utc, rows[-1].id) if more else None,
    )
