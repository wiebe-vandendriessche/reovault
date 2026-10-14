"""The SSE stream: auth-gated, says hello on connect, relays bus events,
and the scheduler listener turns job lifecycle events into hints."""

import asyncio
import threading
from types import SimpleNamespace

import httpx2 as httpx
import pytest

from reovault.web.events import Event, EventBus
from tests.integration.conftest import login, make_recording


def _read_events(lines, want: int) -> list[tuple[str, str]]:
    """Reads `want` complete `event:`/`data:` frames, skipping comments."""
    frames, current = [], {}
    for line in lines:
        if line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = line[6:]
        elif line == "" and current:
            frames.append((current.get("event", ""), current.get("data", "")))
            current = {}
            if len(frames) == want:
                return frames
    return frames


def test_events_requires_auth(client):
    assert client.get("/api/v1/events").status_code == 401


@pytest.fixture
def live_server(web_app):
    """A real uvicorn on a free loopback port. `TestClient` and httpx's ASGI
    transport both buffer the whole body, so an endless SSE stream can only
    be read over a real socket."""
    import socket
    import time

    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(web_app, host="127.0.0.1", port=port, log_config=None, lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_stream_says_hello_then_relays_published_events(live_server, web_app):
    client = login(httpx.Client(base_url=live_server, timeout=10))
    bus = web_app.state.rv.bus
    with client.stream("GET", "/api/v1/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        assert "content-security-policy" in response.headers  # middleware still applies
        lines = response.iter_lines()
        assert next(lines) == "retry: 5000"
        assert _read_events(lines, 1) == [("hello", "{}")]
        # Published from another thread, like a scheduler job would.
        threading.Thread(target=bus.publish, args=("problems",), kwargs={"device_id": 7}).start()
        frames = _read_events(lines, 1)
    assert frames == [("problems", '{"device_id": 7}')]


def test_retry_publishes_a_problems_hint(auth_client, web_app, env, monkeypatch):
    seen = []
    monkeypatch.setattr(web_app.state.rv.bus, "publish", lambda t, **d: seen.append((t, d)))
    rec_id, _ = env.repository.discover_recording(
        device_id=env.device_id, channel=0, recording=make_recording()
    )
    assert auth_client.post(f"/api/v1/recordings/{rec_id}/retry").status_code == 200
    assert ("problems", {"device_id": env.device_id}) in seen


def test_scheduler_listener_maps_job_to_device():
    bus = EventBus()
    seen = []
    bus.publish = lambda t, **d: seen.append((t, d))  # type: ignore[method-assign]
    bus.apscheduler_listener(SimpleNamespace(job_id="scheduled_archive:3"))
    assert seen == [("activity", {"device_id": 3})]


def test_subscriber_cap():
    async def go():
        bus = EventBus(max_subscribers=1)
        first = bus.subscribe()
        assert first is not None
        assert bus.subscribe() is None
        bus.unsubscribe(first)
        assert bus.subscribe() is not None

    asyncio.run(go())


def test_slow_subscriber_gets_one_resync_instead_of_unbounded_backlog():
    async def go():
        bus = EventBus()
        queue = bus.subscribe()
        assert queue is not None
        for i in range(150):
            bus.publish("activity", device_id=i)
        await asyncio.sleep(0)  # let call_soon_threadsafe callbacks run
        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait())
        assert len(drained) <= 100
        assert Event("resync") in drained

    asyncio.run(go())


@pytest.mark.parametrize("job_id", ["manual_archive-5:2", "integrity_scan:2"])
def test_listener_accepts_every_job_id_shape(job_id):
    bus = EventBus()
    seen = []
    bus.publish = lambda t, **d: seen.append((t, d))  # type: ignore[method-assign]
    bus.apscheduler_listener(SimpleNamespace(job_id=job_id))
    assert seen == [("activity", {"device_id": 2})]
