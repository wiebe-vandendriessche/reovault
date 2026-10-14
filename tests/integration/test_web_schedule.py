"""The schedule API: GET returns the simple-mode fields; PUT writes the
camera's own `[devices.schedule]` (or the global `[schedule]`) into
reovault.toml, keeping its comments, and reschedules the live scheduler; an
env-pinned schedule reports itself read-only and refuses a save.
"""

import tomllib

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient

from tests.integration.conftest import login, make_web_app

BODY = {
    "archive_enabled": True,
    "archive_time": "06:30",
    "archive_overlap_hours": 24,
    "backfill_enabled": True,
    "backfill_dow": 2,
    "backfill_time": "03:00",
    "backfill_days": 14,
    "reconcile_enabled": True,
    "reconcile_interval_hours": 12,
    "integrity_scan_enabled": True,
    "integrity_scan_sample_pct": 10,
}


def _toml(env) -> dict:
    return tomllib.loads((env.tmp_path / "reovault.toml").read_text())


def _scheduled_client(env, web_settings, web_device):
    scheduler = BackgroundScheduler()
    app = make_web_app(env, web_settings, web_device, scheduler=scheduler)
    scheduler.start(paused=True)
    return login(TestClient(app)), scheduler


def test_schedule_get_returns_the_defaults(env, auth_client):
    r = auth_client.get("/api/v1/schedule")

    assert r.status_code == 200
    body = r.json()
    assert body["archive_time"] == "05:00"  # default archive_cron = "0 5 * * *"
    assert body["backfill_dow"] == 6  # default backfill_cron "0 4 * * 0" is Sunday
    assert body["backfill_time"] == "04:00"
    assert body["env_pinned"] is False


def test_schedule_save_persists_and_reschedules_live(
    env, web_settings, web_device, web_password_hash
):
    client, scheduler = _scheduled_client(env, web_settings, web_device)
    try:
        r = client.put("/api/v1/schedule", json=BODY)

        assert r.status_code == 200
        assert r.json()["archive_time"] == "06:30"
        assert r.json()["backfill_dow"] == 2
        assert client.get("/api/v1/schedule").json()["archive_time"] == "06:30"

        own = _toml(env)["devices"][0]["schedule"]
        assert own["archive_cron"] == "30 6 * * *"
        assert own["backfill_cron"] == "0 3 * * 3"  # Python Wednesday (2) is cron's 3
        assert (env.tmp_path / "reovault.toml").read_text().startswith("# test config")
        assert r.json()["inherits"] is False
    finally:
        scheduler.shutdown(wait=False)


def test_schedule_disabling_a_job_removes_it_from_the_live_scheduler(
    env, web_settings, web_device, web_password_hash
):
    client, scheduler = _scheduled_client(env, web_settings, web_device)
    try:
        r = client.put("/api/v1/schedule", json={**BODY, "archive_enabled": False})
        assert r.status_code == 200

        job_ids = {job.id for job in scheduler.get_jobs()}
        assert f"scheduled_archive:{env.device_id}" not in job_ids
        assert f"deep_backfill:{env.device_id}" in job_ids
    finally:
        scheduler.shutdown(wait=False)


def test_schedule_is_read_only_and_refuses_save_when_env_pinned(env, auth_client, monkeypatch):
    monkeypatch.setenv("REOVAULT_SCHEDULE__ARCHIVE_CRON", "0 3 * * *")
    before = (env.tmp_path / "reovault.toml").read_text()

    assert auth_client.get("/api/v1/schedule").json()["env_pinned"] is True

    r = auth_client.put("/api/v1/schedule", json={**BODY, "archive_time": "09:00"})
    assert r.status_code == 409
    assert "environment variables" in r.json()["detail"]
    assert (env.tmp_path / "reovault.toml").read_text() == before  # never written


def test_schedule_save_with_bad_input_is_422_not_500(env, auth_client):
    for bad in (
        {"archive_time": "not-a-time"},
        {"archive_time": "24:00"},
        {"backfill_dow": 7},
        {"archive_overlap_hours": 0},
    ):
        r = auth_client.put("/api/v1/schedule", json={**BODY, **bad})
        assert r.status_code == 422, bad


def test_inherit_drops_the_cameras_own_schedule(env, auth_client):
    auth_client.put("/api/v1/schedule", json=BODY)
    r = auth_client.put("/api/v1/schedule", json={**BODY, "inherit": True})
    assert r.status_code == 200
    assert r.json()["inherits"] is True
    assert r.json()["archive_time"] == "05:00"  # back on the global default
    assert "schedule" not in _toml(env)["devices"][0]


def test_default_schedule_is_the_global_table(env, auth_client):
    r = auth_client.put("/api/v1/schedule/default", json={**BODY, "archive_time": "07:15"})
    assert r.status_code == 200
    assert _toml(env)["schedule"]["archive_cron"] == "15 7 * * *"
    # A camera without its own schedule follows it.
    assert auth_client.get("/api/v1/schedule").json()["archive_time"] == "07:15"
