"""reovault.toml as the two-way source of truth: dashboard writes keep the
file's comments, are validated before they touch disk, and are applied live;
hand edits are picked up the same way; env-set and infrastructure fields
are locked; pre-0.4 dashboard state is migrated into the file exactly once.
"""

import os
import threading
import time
import tomllib

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient

from reovault.config import AlertRules, ScheduleConfig
from reovault.config_store import (
    ConfigConflictError,
    ConfigInvalidError,
    ConfigStore,
    migrate_db_settings,
    set_fields,
    table_at,
)
from tests.integration.conftest import login, make_web_app

COMMENTED = """# My ReoVault config. Keep this comment.

[[devices]]
alias = "doorbell"  # the front door
timezone = "Europe/Brussels"

# Keep footage for three months.
[retention]
max_age_days = 90
"""


@pytest.fixture
def toml_path(env):
    path = env.tmp_path / "reovault.toml"
    path.write_text(COMMENTED)
    return path


@pytest.fixture
def store(toml_path, web_settings):
    return ConfigStore(toml_path, web_settings)


def _toml(path) -> dict:
    return tomllib.loads(path.read_text())


def test_update_keeps_comments_and_layout(store, toml_path):
    store.update(lambda doc: set_fields(table_at(doc, "retention"), {"max_age_days": 30}))
    text = toml_path.read_text()
    assert "# My ReoVault config. Keep this comment." in text
    assert 'alias = "doorbell"  # the front door' in text
    assert "# Keep footage for three months." in text
    assert "max_age_days = 30" in text
    assert store.current.retention.max_age_days == 30
    assert (toml_path.parent / "reovault.toml.bak").read_text() == COMMENTED


def test_an_invalid_update_never_touches_the_file(store, toml_path):
    with pytest.raises(ConfigInvalidError, match="max_age_days"):
        store.update(lambda doc: set_fields(table_at(doc, "retention"), {"max_age_days": 0}))
    assert toml_path.read_text() == COMMENTED
    assert store.current.retention.max_age_days is None  # still the startup settings


def test_a_stale_version_is_refused(store, toml_path):
    version = store.version
    toml_path.write_text(COMMENTED + '\n[schedule]\narchive_cron = "0 6 * * *"\n')
    with pytest.raises(ConfigConflictError):
        store.update(lambda doc: None, expected_version=version)


def test_a_hand_edit_is_applied_and_announced(store, toml_path):
    seen = []
    store.subscribe(lambda old, new: seen.append(new.retention.max_age_days))
    toml_path.write_text(COMMENTED.replace("max_age_days = 90", "max_age_days = 14"))
    assert store.reload() is True
    assert store.current.retention.max_age_days == 14
    assert seen == [14]
    assert store.last_origin == "file"  # announced as a hand edit
    store.update(lambda doc: set_fields(table_at(doc, "retention"), {"max_age_days": 21}))
    assert store.last_origin == "dashboard"


def test_our_own_write_is_not_applied_twice(store):
    seen = []
    store.subscribe(lambda old, new: seen.append(1))
    store.update(lambda doc: set_fields(table_at(doc, "retention"), {"max_age_days": 30}))
    assert store.reload() is True  # the watcher firing for our own write
    assert seen == [1]


def test_a_broken_hand_edit_keeps_the_last_valid_settings(store, toml_path):
    store.update(lambda doc: set_fields(table_at(doc, "retention"), {"max_age_days": 30}))
    toml_path.write_text("[retention\nmax_age_days = 5\n")
    assert store.reload() is False
    assert "Not valid TOML" in store.error
    assert store.current.retention.max_age_days == 30
    toml_path.write_text(COMMENTED)
    assert store.reload() is True
    assert store.error is None


def test_infrastructure_changes_wait_for_a_restart(store, toml_path, web_settings):
    toml_path.write_text(COMMENTED + "\n[web]\nport = 9999\npage_size = 7\n")
    store.reload()
    assert store.current.web.port == web_settings.web.port  # still the startup value
    assert store.current.web.page_size == 7  # safe field: live
    assert store.restart_required == ["web.port"]


def test_the_watcher_picks_up_an_edit(store, toml_path):
    stop = threading.Event()
    store.watch(stop)
    try:
        time.sleep(0.3)
        toml_path.write_text(COMMENTED.replace("max_age_days = 90", "max_age_days = 7"))
        deadline = time.monotonic() + 10
        while store.current.retention.max_age_days != 7:
            assert time.monotonic() < deadline, "watcher did not reload"
            time.sleep(0.1)
    finally:
        stop.set()


def test_read_only_config_is_reported_and_refused(env, web_settings, web_device, web_password_hash):
    client = login(TestClient(make_web_app(env, web_settings, web_device)))
    path = env.tmp_path / "reovault.toml"
    os.chmod(path, 0o444)
    os.chmod(env.tmp_path, 0o555)
    try:
        body = client.get("/api/v1/config").json()
        assert body["writable"] is False
        r = client.put("/api/v1/retention", json={"max_age_days": 30, "max_vault_gb": None})
        assert r.status_code == 409
        assert "read-only" in r.json()["detail"]
    finally:
        os.chmod(env.tmp_path, 0o755)
        os.chmod(path, 0o644)


# -- the API view -------------------------------------------------------


def test_config_view_shows_sources_and_locks(auth_client, monkeypatch):
    monkeypatch.setenv("REOVAULT_RETENTION__MAX_AGE_DAYS", "10")
    body = auth_client.get("/api/v1/config").json()
    sections = {".".join(s["path"]): {f["key"]: f for f in s["fields"]} for s in body["sections"]}
    assert sections["retention"]["max_age_days"]["source"] == "env"
    assert sections["retention"]["max_age_days"]["editable"] is False
    assert sections["web"]["port"]["editable"] is False  # infrastructure
    assert sections["web"]["page_size"]["editable"] is True
    assert "password_hash" not in sections["web"]  # a credential, never shown
    assert sections["storage"]["vault_dir"]["restart"] is True


def test_generic_section_edit_and_locks(auth_client, env):
    version = auth_client.get("/api/v1/config").json()["version"]
    r = auth_client.put(
        "/api/v1/config/alerts",
        json={"version": version, "values": {"ntfy_url": "https://ntfy.sh/x", "smtp_port": 465}},
    )
    assert r.status_code == 200, r.text
    assert _toml(env.tmp_path / "reovault.toml")["alerts"]["ntfy_url"] == "https://ntfy.sh/x"

    version = r.json()["version"]
    stale = auth_client.put(
        "/api/v1/config/web", json={"version": "0" * 64, "values": {"page_size": 5}}
    )
    assert stale.status_code == 409
    locked = auth_client.put("/api/v1/config/web", json={"version": version, "values": {"port": 1}})
    assert locked.status_code == 409
    bad = auth_client.put(
        "/api/v1/config/web", json={"version": version, "values": {"page_size": "lots"}}
    )
    assert bad.status_code == 422


def test_alert_rules_are_written_to_the_file(auth_client, env):
    rules = {**AlertRules().model_dump(), "renotify_hours": 6}
    assert auth_client.put("/api/v1/alerts/rules", json=rules).status_code == 200
    assert _toml(env.tmp_path / "reovault.toml")["alerts"]["rules"] == {"renotify_hours": 6}


# -- devices follow the file ----------------------------------------------


def test_cameras_follow_the_file(env, web_settings, web_device, web_password_hash):
    scheduler = BackgroundScheduler()
    app = make_web_app(env, web_settings, web_device, scheduler=scheduler)
    scheduler.start(paused=True)
    st = app.state.rv
    path = env.tmp_path / "reovault.toml"
    try:
        # Off from the dashboard: written as enabled = false, leaves the fleet.
        client = login(TestClient(app))
        r = client.put(f"/api/v1/devices/{env.device_id}/enabled", json={"enabled": False})
        assert r.status_code == 200
        assert _toml(path)["devices"][0]["enabled"] is False
        assert st.fleet.get(env.device_id) is None

        # Back on by hand: the key removed from the file.
        path.write_text(path.read_text().replace("enabled = false\n", ""))
        st.config.reload()
        assert st.fleet.get(env.device_id) is not None
        assert f"scheduled_archive:{env.device_id}" in {j.id for j in scheduler.get_jobs()}

        # Removed from the file entirely: disabled, never deleted.
        path.write_text("# no cameras\n")
        st.config.reload()
        assert st.fleet.get(env.device_id) is None
        row = env.repository.get_device(env.device_id)
        assert row is not None and row.enabled is False
    finally:
        scheduler.shutdown(wait=False)


# -- migration from the pre-0.4 database settings ---------------------------


def test_database_settings_move_into_the_file_once(env, store, toml_path):
    repo = env.repository
    repo.set_setting("schedule", ScheduleConfig(archive_cron="0 6 * * *").model_dump_json())
    repo.set_setting(
        f"schedule:{env.device_id}", ScheduleConfig(archive_cron="0 9 * * *").model_dump_json()
    )
    repo.set_setting("retention", '{"max_age_days": 30, "max_vault_gb": null}')
    repo.set_setting("alert_rules", AlertRules(renotify_hours=3).model_dump_json())
    garage = repo.upsert_device(alias="garage", channel=0, timezone="UTC", name="Garage")
    repo.set_device_enabled(garage, False)

    migrated = migrate_db_settings(store, repo)

    data = _toml(toml_path)
    assert data["schedule"]["archive_cron"] == "0 6 * * *"
    assert data["devices"][0]["schedule"]["archive_cron"] == "0 9 * * *"
    assert data["retention"] == {"max_age_days": 30}
    assert data["alerts"]["rules"] == {"renotify_hours": 3}
    assert data["devices"][1] == {
        "alias": "garage",
        "timezone": "UTC",
        "name": "Garage",
        "enabled": False,
    }
    assert "# My ReoVault config. Keep this comment." in toml_path.read_text()
    assert set(migrated) >= {"schedule", "retention", "alert_rules", "device:garage"}
    assert repo.get_setting("retention") is None
    assert migrate_db_settings(store, repo) == []  # exactly once


def test_migration_waits_for_a_writable_file(env, store, toml_path):
    env.repository.set_setting("retention", '{"max_age_days": 30, "max_vault_gb": null}')
    os.chmod(toml_path, 0o444)
    try:
        assert migrate_db_settings(store, env.repository) == []
        assert env.repository.get_setting("retention") is not None  # kept for later
    finally:
        os.chmod(toml_path, 0o644)


def test_camera_name_and_timezone_are_written_to_the_file(auth_client, env):
    r = auth_client.put(
        f"/api/v1/devices/{env.device_id}/settings",
        json={"name": "Front door", "timezone": "Europe/Paris"},
    )
    assert r.status_code == 200, r.text
    entry = _toml(env.tmp_path / "reovault.toml")["devices"][0]
    assert entry["name"] == "Front door" and entry["timezone"] == "Europe/Paris"
    assert env.repository.get_device(env.device_id).timezone == "Europe/Paris"
    bad = auth_client.put(
        f"/api/v1/devices/{env.device_id}/settings", json={"timezone": "Mars/Base"}
    )
    assert bad.status_code == 422


def test_a_broken_file_still_gives_a_config_view(auth_client, env):
    path = env.tmp_path / "reovault.toml"
    path.write_text("[retention\n")
    st = auth_client.app.state.rv
    assert st.config.reload() is False
    body = auth_client.get("/api/v1/config").json()
    assert "Not valid TOML" in body["error"]


def test_writes_stay_tidy(auth_client, env):
    """No default values added to camera entries, and a camera's own
    schedule lists only what differs from the defaults."""
    body = {
        "archive_enabled": True,
        "archive_time": "06:30",
        "archive_overlap_hours": 48,
        "backfill_enabled": True,
        "backfill_dow": 6,
        "backfill_time": "04:00",
        "backfill_days": 30,
        "reconcile_enabled": True,
        "reconcile_interval_hours": 6,
        "integrity_scan_enabled": True,
        "integrity_scan_sample_pct": 5,
    }
    assert auth_client.put("/api/v1/schedule", json=body).status_code == 200
    entry = _toml(env.tmp_path / "reovault.toml")["devices"][0]
    assert entry["schedule"] == {"archive_cron": "30 6 * * *"}
    assert "enabled" not in entry


def test_devices_report_an_own_schedule(auth_client, env):
    assert auth_client.get("/api/v1/devices").json()["devices"][0]["has_own_schedule"] is False
    body = {
        "archive_enabled": True,
        "archive_time": "06:30",
        "archive_overlap_hours": 48,
        "backfill_enabled": True,
        "backfill_dow": 6,
        "backfill_time": "04:00",
        "backfill_days": 30,
        "reconcile_enabled": True,
        "reconcile_interval_hours": 6,
        "integrity_scan_enabled": True,
        "integrity_scan_sample_pct": 5,
    }
    auth_client.put("/api/v1/schedule", json=body)
    assert auth_client.get("/api/v1/devices").json()["devices"][0]["has_own_schedule"] is True
