"""`GET /api/v1/events`: the dashboard's one live-update stream, replacing
every polling loop. See `reovault/web/events.py` for the bus."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from reovault.web.deps import State

router = APIRouter(tags=["events"])

HEARTBEAT_SECS = 15.0


@router.get("/events", response_class=StreamingResponse)
async def events(request: Request, st: State) -> StreamingResponse:
    queue = st.bus.subscribe()
    if queue is None:
        raise HTTPException(503, "Too many open dashboards.")

    async def stream() -> AsyncIterator[str]:
        try:
            # Reconnect after 5s on a drop; `hello` tells the client the
            # stream is (re)established, so it refetches its snapshots.
            yield "retry: 5000\nevent: hello\ndata: {}\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECS)
                except TimeoutError:
                    # A comment line: keeps proxies from timing out an idle
                    # connection, and lets a dead peer surface as a failed
                    # write.
                    yield ": ping\n\n"
                    continue
                yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
        finally:
            st.bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
