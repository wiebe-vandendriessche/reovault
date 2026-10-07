"""Problems list and count, retrying from it, and the manual-run Stop action
(see `Archiver.request_cancel`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from reovault.models import ErrorClass, RemoteRecording
from tests.integration.conftest import login, make_web_app


def _seed_failed_recording(env, start: datetime | None = None, *, name: str | None = None) -> int:
    start = start or datetime(2026, 9, 18, 20, 59, 12, tzinfo=UTC)
    name = name or f"01{start:%Y%m%d%H%M%S}"
    rec = RemoteRecording(
        remote_name=name,
        start_utc=start,
        end_utc=None,
        duration_s=None,
        rec_type="md,vehicle",
        stream="mainStream",
        remote_size=21905004,
        raw_metadata={"fileName": name},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.mark_failed(
        rid, error="exit code -2", error_class=ErrorClass.DEVICE, next_attempt_at=None
    )
    return rid


def _start_run(env) -> int:
    return env.repository.start_run(
        device_id=env.device_id,
        trigger="manual",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_problems_lists_failed_and_quarantined_newest_first(env, auth_client):
    older = _seed_failed_recording(env, datetime(2026, 9, 1, tzinfo=UTC))
    newer = _seed_failed_recording(env, datetime(2026, 9, 2, tzinfo=UTC))
    env.repository.mark_quarantined(newer, error="bad", error_class=ErrorClass.PROTOCOL)

    body = auth_client.get("/api/v1/problems").json()

    assert [p["id"] for p in body["items"]] == [newer, older]
    assert [p["state"] for p in body["items"]] == ["quarantined", "failed"]
    assert body["next"] is None
    assert auth_client.get("/api/v1/problems/count").json() == {"count": 2}


def test_healthy_device_has_no_problems(auth_client):
    assert auth_client.get("/api/v1/problems").json() == {"items": [], "next": None}
    assert auth_client.get("/api/v1/problems/count").json() == {"count": 0}


def test_problems_keyset_paginates(env, web_settings, web_device, web_password_hash):
    """Two problems share a start_utc so the page boundary needs the id
    tiebreak; the last full page hands out no cursor (one row is overfetched
    to know), so there's never a trailing empty page."""
    web_settings.web.page_size = 2
    client = login(TestClient(make_web_app(env, web_settings, web_device)))
    base = datetime(2026, 9, 1, tzinfo=UTC)
    ids = [
        _seed_failed_recording(env, base, name="a"),
        _seed_failed_recording(env, base + timedelta(hours=1), name="b"),
        _seed_failed_recording(env, base + timedelta(hours=1), name="c"),
        _seed_failed_recording(env, base + timedelta(hours=2), name="d"),
    ]

    seen: list[int] = []
    cursor = ""
    pages = 0
    while True:
        body = client.get("/api/v1/problems", params={"after": cursor}).json()
        pages += 1
        seen += [p["id"] for p in body["items"]]
        if body["next"] is None:
            break
        cursor = body["next"]

    assert pages == 2
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))
    assert seen[0] == ids[3] and seen[-1] == ids[0]


def test_retrying_from_problems_keeps_the_error_and_stays_a_problem(env, auth_client):
    """Retry only makes the row eligible for the next run: it stays in the
    Problems list with its explanation until a run actually fixes it."""
    rid = _seed_failed_recording(env)

    r = auth_client.post(f"/api/v1/recordings/{rid}/retry")

    assert r.status_code == 200
    assert r.json()["last_error"] == "exit code -2"
    listed = auth_client.get("/api/v1/problems").json()["items"]
    assert [p["id"] for p in listed] == [rid]
    assert auth_client.get("/api/v1/problems/count").json() == {"count": 1}


def test_activity_shows_the_running_run(env, auth_client):
    assert auth_client.get("/api/v1/activity").json()["running"] is None

    run_id = _start_run(env)

    running = auth_client.get("/api/v1/activity").json()["running"]
    assert running is not None
    assert running["id"] == run_id


def test_actions_stop_sets_the_archivers_cancel_flag(env, web_app, auth_client):
    """`/actions/stop` must reach the exact same Archiver instance the
    scheduler would be running the job on (see fleet.Fleet: one persistent
    Archiver per device, reused across every job), not a throwaway copy."""
    archiver = web_app.state.rv.fleet.get(env.device_id)
    run_id = _start_run(env)
    assert not archiver.cancel_requested.is_set()

    r = auth_client.post("/api/v1/actions/stop")

    assert r.status_code == 200
    assert archiver.cancel_requested.is_set()
    assert r.json()["running"]["id"] == run_id


def test_actions_stop_is_a_no_op_with_nothing_running(env, web_app, auth_client):
    r = auth_client.post("/api/v1/actions/stop")

    assert r.status_code == 200
    assert r.json()["running"] is None
    assert not web_app.state.rv.fleet.get(env.device_id).cancel_requested.is_set()


def test_actions_stop_requires_csrf(auth_client):
    r = auth_client.post("/api/v1/actions/stop", headers={"X-CSRF-Token": "nope"})
    assert r.status_code == 403
