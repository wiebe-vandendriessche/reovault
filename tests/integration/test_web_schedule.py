"""The Schedule tab: GET renders, POST saves and reschedules the live
scheduler, and an env-pinned schedule renders read-only and refuses a save.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient

from tests.integration.conftest import extract_body_csrf, login, make_web_app


def test_schedule_page_renders(env, auth_client):
    r = auth_client.get("/schedule")

    assert r.status_code == 200
    assert "Download new clips" in r.text
    assert "05:00" in r.text  # default archive_cron = "0 5 * * *"


def test_schedule_save_persists_and_reschedules_live(
    env, web_settings, web_device, web_password_hash
):
    scheduler = BackgroundScheduler()
    app = make_web_app(env, web_settings, web_device, scheduler=scheduler)
    scheduler.start(paused=True)

    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/schedule").text)

    r = client.post(
        "/schedule",
        data={
            "archive_enabled": "on",
            "archive_time": "06:30",
            "archive_overlap_hours": "24",
            "backfill_enabled": "on",
            "backfill_dow": "2",
            "backfill_time": "03:00",
            "backfill_days": "14",
            "reconcile_enabled": "on",
            "reconcile_interval_hours": "12",
            "integrity_scan_enabled": "on",
            "integrity_scan_sample_pct": "10",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 200
    assert "Saved." in r.text
    assert "06:30" in r.text

    from reovault.scheduler import _schedule_key

    stored = env.repository.get_setting(_schedule_key(env.device_id))
    assert stored is not None
    assert '"archive_cron":"30 6 * * *"' in stored

    scheduler.shutdown(wait=False)


def test_schedule_disabling_a_job_removes_it_from_the_live_scheduler(
    env, web_settings, web_device, web_password_hash
):
    scheduler = BackgroundScheduler()
    app = make_web_app(env, web_settings, web_device, scheduler=scheduler)
    scheduler.start(paused=True)

    client = TestClient(app)
    login(client)
    csrf = extract_body_csrf(client.get("/schedule").text)

    client.post(
        "/schedule",
        data={
            "archive_time": "05:00",
            "archive_overlap_hours": "48",
            # archive_enabled deliberately omitted: unchecked box
            "backfill_enabled": "on",
            "backfill_dow": "0",
            "backfill_time": "04:00",
            "backfill_days": "30",
            "reconcile_enabled": "on",
            "reconcile_interval_hours": "6",
            "integrity_scan_enabled": "on",
            "integrity_scan_sample_pct": "5",
        },
        headers={"X-CSRF-Token": csrf},
    )

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert f"scheduled_archive:{env.device_id}" not in job_ids
    assert f"deep_backfill:{env.device_id}" in job_ids

    scheduler.shutdown(wait=False)


def test_schedule_is_read_only_and_refuses_save_when_env_pinned(env, auth_client, monkeypatch):
    monkeypatch.setenv("REOVAULT_SCHEDULE__ARCHIVE_CRON", "0 3 * * *")

    r_get = auth_client.get("/schedule")
    assert "environment variables" in r_get.text

    csrf = extract_body_csrf(auth_client.get("/schedule").text)
    r_post = auth_client.post(
        "/schedule",
        data={"archive_time": "09:00", "archive_overlap_hours": "48"},
        headers={"X-CSRF-Token": csrf},
    )
    assert "environment variables" in r_post.text

    from reovault.scheduler import SCHEDULE_SETTING_KEY

    assert env.repository.get_setting(SCHEDULE_SETTING_KEY) is None  # never written


def test_schedule_save_with_bad_input_shows_an_error_not_a_500(env, auth_client):
    csrf = extract_body_csrf(auth_client.get("/schedule").text)

    r = auth_client.post(
        "/schedule",
        data={"archive_time": "not-a-time", "archive_overlap_hours": "48"},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 200
    assert "field-error" in r.text
