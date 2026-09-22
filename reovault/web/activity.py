"""Bounded in-memory activity ring. Fills the gap between "queued" and the
first `archive_runs` row appearing: transient scheduler events (submitted,
skipped because the lock was held, errored) never reach the database, and
without this the manual-run button can appear to silently do nothing.
Display only, lost on restart.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    at: datetime
    job_id: str
    kind: str  # submitted|executed|error|missed|skipped_already_running
    detail: str | None = None


class ActivityLog:
    def __init__(self, maxlen: int = 20) -> None:
        self._lock = threading.Lock()
        self._events: deque[ActivityEvent] = deque(maxlen=maxlen)

    def record(self, job_id: str, kind: str, detail: str | None = None) -> None:
        with self._lock:
            self._events.appendleft(
                ActivityEvent(at=datetime.now(UTC), job_id=job_id, kind=kind, detail=detail)
            )

    def recent(self) -> list[ActivityEvent]:
        with self._lock:
            return list(self._events)

    def apscheduler_listener(self, event: Any) -> None:
        """Pass to `scheduler.add_listener(log.apscheduler_listener, mask)`
        with `mask = EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED |
        EVENT_JOB_ERROR | EVENT_JOB_MISSED`."""
        from apscheduler.events import (
            EVENT_JOB_ERROR,
            EVENT_JOB_EXECUTED,
            EVENT_JOB_MISSED,
            EVENT_JOB_SUBMITTED,
        )

        kind = {
            EVENT_JOB_SUBMITTED: "submitted",
            EVENT_JOB_EXECUTED: "executed",
            EVENT_JOB_ERROR: "error",
            EVENT_JOB_MISSED: "missed",
        }.get(event.code, "unknown")
        detail = str(exc) if (exc := getattr(event, "exception", None)) else None
        self.record(event.job_id, kind, detail)
