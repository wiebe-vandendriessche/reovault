"""The manual-runs card: a start/end time range, and queueing another run
while one is already going (both broken on the first deployment)."""

from datetime import UTC, datetime

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient

from tests.integration.conftest import extract_body_csrf, login, make_web_app


@pytest.fixture
def paused_scheduler():
    s = BackgroundScheduler(timezone=UTC)
    s.start(paused=True)  # jobs stay in the store where the test can see them
    yield s
    s.shutdown(wait=False)


@pytest.fixture
def client(env, web_settings, web_device, web_password_hash, paused_scheduler):
    app = make_web_app(env, web_settings, web_device, scheduler=paused_scheduler)
    c = login(TestClient(app))
    c.headers["X-CSRF-Token"] = extract_body_csrf(c.get("/schedule").text)
    return c


def _backfill_windows(scheduler):
    return [
        (j.kwargs["from_utc"], j.kwargs["to_utc"])
        for j in scheduler.get_jobs()
        if j.id.startswith("manual_backfill-")
    ]


def test_backfill_takes_a_start_and_end_time_in_the_cameras_timezone(client, paused_scheduler):
    r = client.post(
        "/actions/backfill", data={"from": "2026-09-18T01:30", "to": "2026-09-21T13:45"}
    )
    assert r.status_code == 200
    assert 'id="run-status"' in r.text
    assert "Starting" in r.text
    # Europe/Brussels is UTC+2 in September.
    assert _backfill_windows(paused_scheduler) == [
        (datetime(2026, 9, 17, 23, 30, tzinfo=UTC), datetime(2026, 9, 21, 11, 45, tzinfo=UTC))
    ]


@pytest.mark.parametrize(
    "form",
    [
        {"from": "", "to": "2026-09-21T13:45"},
        {"from": "2026-09-21T13:45", "to": "2026-09-18T01:30"},
        {"from": "2026-01-01T00:00", "to": "2026-09-21T00:00"},
        {"from": "2026-09-18T01:30+02:00", "to": "2026-09-21T13:45"},
    ],
)
def test_backfill_rejects_bad_ranges(client, paused_scheduler, form):
    r = client.post("/actions/backfill", data=form)
    assert 'class="field-error"' in r.text
    assert _backfill_windows(paused_scheduler) == []


def test_a_second_run_queues_while_one_is_in_progress(env, client, paused_scheduler):
    env.repository.start_run(
        device_id=env.device_id,
        trigger="schedule",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )
    r = client.post("/actions/run")
    assert "Run in progress" in r.text
    assert "starting when this one finishes" in r.text
    assert [j.id for j in paused_scheduler.get_jobs() if j.id.startswith("manual_archive-")]
    # The controls live outside the polled block, so a poll can't wipe a
    # half-typed range.
    status = client.get("/fragments/activity/status").text
    assert "backfill-from" not in status
    assert 'hx-trigger="every 3s"' in status
