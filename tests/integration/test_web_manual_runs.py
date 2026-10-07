"""Manual actions: a backfill takes a start/end time range in the camera's
timezone, and another run queues while one is already going (both broken on
the first deployment)."""

from datetime import UTC, datetime

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient

from reovault.scheduler import queued_manual_jobs
from tests.integration.conftest import login, make_web_app


@pytest.fixture
def paused_scheduler():
    s = BackgroundScheduler(timezone=UTC)
    s.start(paused=True)  # jobs stay in the store where the test can see them
    yield s
    s.shutdown(wait=False)


@pytest.fixture
def client(env, web_settings, web_device, web_password_hash, paused_scheduler):
    app = make_web_app(env, web_settings, web_device, scheduler=paused_scheduler)
    return login(TestClient(app))


def _jobs(scheduler, prefix: str) -> list:
    return [j for j in scheduler.get_jobs() if j.id.startswith(prefix)]


def _backfill_windows(scheduler):
    return [
        (j.kwargs["from_utc"], j.kwargs["to_utc"]) for j in _jobs(scheduler, "manual_backfill-")
    ]


def test_backfill_takes_a_start_and_end_time_in_the_cameras_timezone(env, client, paused_scheduler):
    # The queued counter is process-global, so compare against a baseline.
    before = queued_manual_jobs(env.device_id)

    r = client.post(
        "/api/v1/actions/backfill",
        json={"from_local": "2026-09-18T01:30", "to_local": "2026-09-21T13:45"},
    )

    assert r.status_code == 200
    assert r.json()["queued"] == before + 1
    assert r.json()["timezone"] == "Europe/Brussels"
    # Europe/Brussels is UTC+2 in September.
    assert _backfill_windows(paused_scheduler) == [
        (datetime(2026, 9, 17, 23, 30, tzinfo=UTC), datetime(2026, 9, 21, 11, 45, tzinfo=UTC))
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"from_local": "", "to_local": "2026-09-21T13:45"},
        {"from_local": "2026-09-21T13:45", "to_local": "2026-09-18T01:30"},
        {"from_local": "2026-01-01T00:00", "to_local": "2026-09-21T00:00"},  # > 90 days
        {"from_local": "2026-09-18T01:30+02:00", "to_local": "2026-09-21T13:45"},
    ],
)
def test_backfill_rejects_bad_ranges(client, paused_scheduler, body):
    r = client.post("/api/v1/actions/backfill", json=body)
    assert r.status_code == 422
    assert _backfill_windows(paused_scheduler) == []


def test_a_second_run_queues_while_one_is_in_progress(env, client, paused_scheduler):
    env.repository.start_run(
        device_id=env.device_id,
        trigger="schedule",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )
    before = queued_manual_jobs(env.device_id)

    r = client.post("/api/v1/actions/run")

    assert r.status_code == 200
    assert r.json()["running"] is not None
    assert r.json()["queued"] == before + 1
    assert _jobs(paused_scheduler, "manual_archive-")


def test_reconcile_is_queued(client, paused_scheduler):
    r = client.post("/api/v1/actions/reconcile")

    assert r.status_code == 200
    assert _jobs(paused_scheduler, "manual_reconcile-")


def test_actions_are_503_without_a_scheduler(env, web_settings, web_device, web_password_hash):
    # Not `auth_client`: it builds on this module's scheduler-backed `client`.
    no_scheduler = login(TestClient(make_web_app(env, web_settings, web_device)))
    for path, body in (
        ("run", None),
        ("reconcile", None),
        ("backfill", {"from_local": "2026-09-18T01:30", "to_local": "2026-09-21T13:45"}),
    ):
        r = no_scheduler.post(f"/api/v1/actions/{path}", json=body)
        assert r.status_code == 503, path
        assert r.json()["detail"]
