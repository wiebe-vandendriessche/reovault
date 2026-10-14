"""In-process fan-out for the dashboard's single SSE stream.

Publishers are plain threads (APScheduler jobs, threadpool route handlers);
subscribers are asyncio generators on the server's event loop. `publish`
hops onto each subscriber's loop with `call_soon_threadsafe`, so it's safe
from anywhere and never blocks the caller.

Events are invalidation hints ("device 3's activity changed"), not state:
the dashboard refetches the matching snapshot endpoint. A dropped event is
therefore never fatal, and the client also refetches after every reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass, field
from typing import Any

_QUEUE_MAX = 100


@dataclass(frozen=True, slots=True)
class Event:
    type: str  # activity | problems | devices | alert | resync
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self, max_subscribers: int = 32) -> None:
        self._lock = threading.Lock()
        self._subs: dict[asyncio.Queue[Event], asyncio.AbstractEventLoop] = {}
        self.max_subscribers = max_subscribers

    def subscribe(self) -> asyncio.Queue[Event] | None:
        """Call from the event loop. `None` when at capacity, so a runaway
        client (or many open tabs) can't pin unbounded memory."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            if len(self._subs) >= self.max_subscribers:
                return None
            self._subs[queue] = asyncio.get_running_loop()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with self._lock:
            self._subs.pop(queue, None)

    def publish(self, type_: str, **data: Any) -> None:
        event = Event(type_, data)
        with self._lock:
            subs = list(self._subs.items())
        for queue, loop in subs:
            # RuntimeError: the loop closed under us (shutdown); the
            # generator's own `finally` will unsubscribe.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_offer, queue, event)

    def apscheduler_listener(self, event: Any) -> None:
        """Every job lifecycle event becomes an `activity` hint for its
        device: a run starting or finishing changes the activity card, the
        Health verdict, the runs list and possibly the problems count."""
        from reovault.scheduler import device_of_job_id

        device_id = device_of_job_id(event.job_id)
        if device_id is not None:  # skip fleet-wide housekeeping (alerts)
            self.publish("activity", device_id=device_id)


def _offer(queue: asyncio.Queue[Event], event: Event) -> None:
    """A subscriber too slow to drain 100 hints gets its backlog replaced by
    one `resync`, which tells the client to refetch everything."""
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(Event("resync"))
