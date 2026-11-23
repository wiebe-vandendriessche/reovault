"""The /api/v1 surface the dashboard uses, over a demo camera that was really
backed up to a temp restic repository."""

from __future__ import annotations

import tomllib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from reovault.web.app import create_app
from reovault.worker import Worker
from tests.integration.conftest import login, make_runtime


@pytest.fixture
def backed_up(env, web_settings, web_password_hash):
    rt = make_runtime(env, web_settings)
    worker = Worker("demo", rt.settings, rt.db, rt.gateway, rt.bus.publish)
    rt.workers.by_id["demo"] = worker  # registered, but no thread: runs inline
    worker._backup(rt.settings(), rt.settings().camera("demo"))
    # One clip the card overwrote before it could be backed up.
    from datetime import UTC, datetime, timedelta

    rt.db.upsert_clips(
        "demo", [("0199990101000000", datetime.now(UTC) - timedelta(days=6), None, 10, "md")]
    )
    (gone,) = rt.db.clips_by_name("demo", ["0199990101000000"]).values()
    rt.db.mark_lost(gone.id)
    client = login(TestClient(create_app(rt, dashboard_dir=env.tmp_path / "no-dist")))
    return rt, client


def test_overview_after_a_backup(backed_up):
    _rt, client = backed_up
    ov = client.get("/api/v1/overview").json()
    (cam,) = ov["cameras"]
    assert cam["verdict"] == "up_to_date"
    assert cam["backed_up_clips"] > 600
    assert cam["lost_7d"] >= 1
    assert cam["last_run"]["outcome"] == "ok"
    assert {i["title"] for i in ov["issues"]} == {"Alerts have nowhere to go"}


def test_calendar_day_and_a_byte_exact_download(backed_up):
    _rt, client = backed_up
    today = datetime.now().strftime("%Y-%m")
    cal = client.get(f"/api/v1/clips/calendar?camera=demo&month={today}").json()
    assert cal["days"] and all(d["backed_up"] >= 0 for d in cal["days"])
    day = client.get("/api/v1/clips/day?camera=demo").json()
    clip = next(c for h in day["hours"] for c in h["clips"] if c["state"] == "backed_up")
    r = client.get(f"/api/v1/clips/{clip['id']}/download")
    assert r.status_code == 200
    assert r.content == clip["name"].encode().ljust(4096, b"\0")  # what DemoCamera wrote
    assert f'filename="{clip["name"]}.mp4"' in r.headers["content-disposition"]


def test_a_type_filter_narrows_the_day(backed_up):
    _rt, client = backed_up
    everything = client.get("/api/v1/clips/day?camera=demo").json()
    people = client.get("/api/v1/clips/day?camera=demo&types=people").json()
    count = lambda d: sum(len(h["clips"]) for h in d["hours"])  # noqa: E731
    assert 0 < count(people) < count(everything)
    assert all("people" in c["rec_type"] for h in people["hours"] for c in h["clips"])


def test_lost_clips_show_in_activity_and_cant_be_downloaded(backed_up):
    _rt, client = backed_up
    act = client.get("/api/v1/runs?camera=demo").json()
    assert act["runs"] and act["lost"]
    lost = act["lost"][0]
    assert client.get(f"/api/v1/clips/{lost['id']}/download").status_code == 404


def test_adding_cameras(backed_up, env):
    _rt, client = backed_up
    r = client.post(
        "/api/v1/cameras", json={"id": "yard", "timezone": "Europe/Brussels", "demo": True}
    )
    assert r.status_code == 201, r.text
    assert [c["id"] for c in r.json()] == ["demo", "yard"]
    assert tomllib.loads(env.toml.read_text())["cameras"][1] == {
        "id": "yard",
        "timezone": "Europe/Brussels",
        "demo": True,
    }
    missing_creds = {"id": "real", "timezone": "Europe/Brussels"}
    assert client.post("/api/v1/cameras", json=missing_creds).status_code == 422
    bad_tz = {"id": "x", "timezone": "Mars/Base", "demo": True}
    assert client.post("/api/v1/cameras", json=bad_tz).status_code == 422
    assert client.post("/api/v1/cameras", json={**bad_tz, "id": "../etc"}).status_code == 422


def test_a_real_camera_is_saved_only_after_a_login_and_its_password_never_comes_back(
    backed_up, env, monkeypatch
):
    from reovault import camera as cam_mod

    _rt, client = backed_up
    body = {"id": "gate", "timezone": "UTC", "host": "192.0.2.7", "password": "s3cret-pw"}

    def refuse(*_a, **_k):
        raise cam_mod.ToolError("login failed: wrong password")

    monkeypatch.setattr(cam_mod, "check", refuse)
    r = client.post("/api/v1/cameras", json=body)
    assert r.status_code == 502 and "wrong password" in r.text
    assert "gate" not in env.toml.read_text()  # nothing saved

    monkeypatch.setattr(cam_mod, "check", lambda *_a, **_k: None)
    r = client.post("/api/v1/cameras", json=body)
    assert r.status_code == 201, r.text
    entry = tomllib.loads(env.toml.read_text())["cameras"][-1]
    assert (entry["host"], entry["password"]) == ("192.0.2.7", "s3cret-pw")
    assert "s3cret-pw" not in client.get("/api/v1/cameras").text
    assert "s3cret-pw" not in client.get("/api/v1/config").text


def test_run_now_and_stop_need_a_running_worker(backed_up):
    _rt, client = backed_up
    assert client.post("/api/v1/cameras/demo/stop").status_code == 409  # nothing running
    assert client.post("/api/v1/cameras/nope/run").status_code == 404


def test_errors_never_leak_repository_credentials(backed_up):
    rt, _client = backed_up
    text = "restic open failed: rest:https://alice:s3cret@backup.lan/reovault/demo"
    assert "s3cret" not in rt.redact(text)
    assert "alice:s3cret" not in rt.scrub_credentials(text)


def test_alert_channels_cant_be_changed_from_the_dashboard(backed_up):
    """A token file pointed at the restic password, sent to any URL, would
    leak it: channels change in reovault.toml only."""
    _rt, client = backed_up
    cfg = client.get("/api/v1/config").json()
    (alerts,) = [s for s in cfg["sections"] if s["path"] == ["alerts"]]
    assert not any(f["editable"] for f in alerts["fields"])
    r = client.put(
        "/api/v1/config/alerts",
        json={
            "version": cfg["version"],
            "values": {"ntfy_token_file": "/run/secrets/restic_password"},
        },
    )
    assert r.status_code == 404


def test_the_overview_lists_destinations_coverage_and_the_password_notice(backed_up):
    _rt, client = backed_up
    ov = client.get("/api/v1/overview", params={"span": "7"}).json()
    (dest,) = ov["destinations"]
    assert (dest["name"], dest["role"]) == ("local", "primary")
    assert dest["used_bytes"] > 0 and dest["snapshots"] >= 1
    (cam,) = ov["cameras"]
    rows = cam["coverage"]["rows"]
    assert [(r["kind"], r["name"]) for r in rows] == [("card", "SD card"), ("primary", "local")]
    held = sum(r["clips"] for r in rows[1]["runs"] if r["state"] == "safe")
    assert held == cam["backed_up_clips"]  # every backed-up clip of the week is on the primary
    assert "lost" in {r["state"] for r in rows[0]["runs"]}  # the clip the card overwrote
    assert [n["title"] for n in ov["notices"]] == ["Keep your restic password safe"]
    assert client.get("/api/v1/cameras").json()[0]["coverage"] is None  # only on the overview
    assert ov["oldest_backup"] is not None
    for span in ("card", "all", "365"):
        assert client.get("/api/v1/overview", params={"span": span}).status_code == 200
    for bad in ("0", "week", "-3"):
        assert client.get("/api/v1/overview", params={"span": bad}).status_code == 422


def test_the_device_endpoint_asks_the_camera_and_says_when_it_cant(backed_up, monkeypatch):
    from reovault import camera as cam_mod
    from reovault.web.api import cameras as api

    _rt, client = backed_up
    api._devices.clear()
    dev = client.get("/api/v1/cameras/demo/device").json()
    assert (dev["model"], dev["error"]) == ("Demo camera", None)
    assert dev["card"]["total_gb"] == 64.0

    def unreachable(self):
        raise cam_mod.ToolError("timed out")

    api._devices.clear()
    monkeypatch.setattr(cam_mod.DemoCamera, "info", unreachable)
    dev = client.get("/api/v1/cameras/demo/device").json()
    assert dev["model"] is None and dev["error"].startswith("Can't reach the camera")
    assert client.get("/api/v1/cameras/nope/device").status_code == 404


def test_a_run_shows_its_batches_and_clips(backed_up):
    rt, client = backed_up
    (run,) = rt.db.runs("demo")
    detail = client.get(f"/api/v1/runs/{run.id}").json()
    assert detail["run"]["outcome"] == "ok"
    assert detail["batches"] and detail["batches"][0]["clips"] > 0
    assert detail["batches"][0]["upload_secs"] is not None
    assert {c["outcome"] for c in detail["clips"]} == {"backed_up"}
    assert detail["truncated"] is (detail["run"]["clips"] > 500)
    assert client.get("/api/v1/runs/99999").status_code == 404


def test_the_restic_password_file_cant_be_moved_from_the_dashboard(backed_up):
    _rt, client = backed_up
    config = client.get("/api/v1/config").json()
    backup = next(s for s in config["sections"] if s["path"] == ["backup"])
    fields = {f["key"]: f for f in backup["fields"]}
    assert fields["interval_minutes"]["editable"] and not fields["password_file"]["editable"]
    r = client.put(
        "/api/v1/config/backup",
        json={"version": config["version"], "values": {"password_file": "/tmp/elsewhere"}},
    )
    assert r.status_code == 409, r.text


def test_a_new_camera_address_needs_the_password_again(backed_up, env, monkeypatch):
    from reovault import camera as cam_mod

    _rt, client = backed_up
    tried: list[str] = []
    monkeypatch.setattr(cam_mod, "check", lambda *_a, host, **_k: tried.append(host))
    body = {"id": "gate", "timezone": "UTC", "host": "192.0.2.7", "password": "s3cret-pw"}
    assert client.post("/api/v1/cameras", json=body).status_code == 201
    tried.clear()
    edit = {"timezone": "UTC", "enabled": True, "host": "203.0.113.66"}
    r = client.put("/api/v1/cameras/gate", json=edit)
    assert r.status_code == 422 and tried == []  # the stored password went nowhere
    assert "203.0.113.66" not in env.toml.read_text()
    r = client.put("/api/v1/cameras/gate", json={**edit, "password": "s3cret-pw"})
    assert r.status_code == 200 and tried == ["203.0.113.66"]


def test_closing_the_bus_ends_open_event_streams():
    """uvicorn waits for open responses on shutdown; an SSE stream must end
    when it starts, not be cancelled with a traceback after the timeout."""
    import asyncio
    from types import SimpleNamespace

    from reovault.web.api.events import events
    from reovault.web.events import EventBus

    async def run() -> list[str]:
        bus = EventBus()

        async def connected() -> bool:
            return False

        request = SimpleNamespace(is_disconnected=connected)
        response = await events(request, SimpleNamespace(bus=bus))
        body = response.body_iterator
        chunks = [await anext(body)]  # hello
        bus.close()
        chunks += [c async for c in body]
        return chunks

    chunks = asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert len(chunks) == 1 and "hello" in chunks[0]


def _issues(client: TestClient) -> dict[str, dict]:
    return {i["key"]: i for i in client.get("/api/v1/overview").json()["issues"]}


def _finished(rt, outcome: str, error: str | None = None, ago_hours: float = 0) -> None:
    run_id = rt.db.start_run("demo", "schedule")
    rt.db.finish_run(run_id, outcome, error)
    from datetime import UTC, datetime, timedelta

    from reovault.db import iso

    at = iso(datetime.now(UTC) - timedelta(hours=ago_hours))
    with rt.db.tx() as c:
        c.execute("UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?", (at, at, run_id))


def test_a_stopped_run_neither_hides_nor_resolves_failing_backups(backed_up):
    rt, client = backed_up
    _finished(rt, "error", "restic open failed: locked")
    _finished(rt, "stopped")  # a restart or the Stop button proves nothing
    _finished(rt, "error", "restic open failed: locked")
    issue = _issues(client)["failing:demo"]
    assert issue["actions"] == ["run"]
    assert "locked" in issue["message"]


def test_hours_without_a_backup_raise_one_stale_alert_instead_of_failing(backed_up):
    rt, client = backed_up
    with rt.db.tx() as c:  # the fixture's good run, made old
        c.execute("UPDATE runs SET started_at = ?, finished_at = ?", ("2026-01-01T00:00:00Z",) * 2)
    _finished(rt, "error", "boom")
    _finished(rt, "error", "boom")
    issues = _issues(client)
    assert "stale:demo" in issues and "failing:demo" not in issues
    assert "boom" in issues["stale:demo"]["message"]


def test_check_errors_are_per_destination_and_check_again_asks_for_a_check(backed_up):
    rt, client = backed_up
    rt.db.set_setting("check_errors:local:demo", "2")
    issue = _issues(client)["check_errors:local:demo"]
    assert issue["actions"] == ["check"]
    assert client.post("/api/v1/cameras/demo/check").status_code == 200
    assert rt.db.get_setting("check_requested:local:demo")


def test_a_destination_with_its_check_off_is_never_checked_or_alerted(backed_up):
    """Some servers ban an IP after the check's bulk download: once it's off,
    old findings stay quiet and Check again can't start one there."""
    rt, client = backed_up
    rt.db.set_setting("check_errors:local:demo", "2")
    off = {"keep_days": 0, "max_gb": 0, "enabled": True, "check": False}
    saved = client.put("/api/v1/destinations/local", json=off)
    assert saved.status_code == 200 and saved.json()[0]["check"] is False
    assert "check_errors:local:demo" not in _issues(client)
    # An older dashboard edits limits without the field: the check stays off.
    del off["check"]
    assert client.put("/api/v1/destinations/local", json=off).json()[0]["check"] is False
    (cam,) = client.get("/api/v1/cameras").json()
    assert cam["check_errors"] == 0
    assert client.post("/api/v1/cameras/demo/check").status_code == 409
    rt.db.delete_setting("check_requested:local:demo")
    rt.db.set_setting("check_next:local:demo", "2000-01-01T00:00:00Z")  # long overdue
    worker = rt.workers.by_id["demo"]
    worker._maintain(rt.settings())
    assert rt.db.get_setting("check:local:demo") is None  # never ran


def test_failing_clips_can_be_retried_or_given_up_from_the_dashboard(backed_up):
    rt, client = backed_up
    from datetime import UTC, datetime, timedelta

    rt.db.upsert_clips("demo", [("0120990202000000", datetime.now(UTC), None, 10, "md")])
    (clip,) = rt.db.clips_by_name("demo", ["0120990202000000"]).values()
    for _ in range(5):
        rt.db.record_failure(clip.id, "downloaded 3 bytes, expected 10")
    assert _issues(client)["clips_keep_failing:demo"]["actions"] == ["retry_clips", "give_up_clips"]
    r = client.post("/api/v1/cameras/demo/clips/retry", json={})
    assert r.json() == {"changed": 1}
    assert "clips_keep_failing:demo" not in _issues(client)
    for _ in range(5):
        rt.db.record_failure(clip.id, "downloaded 3 bytes, expected 10")
    r = client.post("/api/v1/cameras/demo/clips/give-up", json={"ids": [clip.id]})
    assert r.json() == {"changed": 1}
    assert rt.db.get_clip(clip.id).state == "lost"
    assert "clips_keep_failing:demo" not in _issues(client)
    _ = timedelta


def test_a_quota_notice_clears_once_the_limits_change(backed_up):
    rt, client = backed_up
    from reovault.destinations import update_status

    update_status(rt.db, "local", short_until="2099-01-01T00:00:00Z", short_for="0/0")
    assert "quota_retention_short:local:*" in _issues(client)
    update_status(rt.db, "local", short_for="14/500")  # cut under limits since changed
    assert "quota_retention_short:local:*" not in _issues(client)


def test_a_failing_settings_backup_is_raised_and_points_to_recovery(backed_up):
    rt, client = backed_up
    from reovault.destinations import update_status

    update_status(rt.db, "local", config_error="restic backup failed: boom")
    assert _issues(client)["config_backup:local:*"]["actions"] == ["recovery"]
    update_status(rt.db, "local", config_error=None)
    assert "config_backup:local:*" not in _issues(client)


def test_an_unreachable_destination_is_one_problem_not_one_per_camera(backed_up):
    rt, client = backed_up
    from reovault.destinations import update_status

    with rt.db.tx() as c:  # long since the last good backup
        c.execute("UPDATE runs SET started_at = ?, finished_at = ?", ("2026-01-01T00:00:00Z",) * 2)
    _finished(rt, "error", "restic open failed: error talking HTTP to rclone")
    _finished(rt, "error", "restic open failed: error talking HTTP to rclone")
    update_status(rt.db, "local", config_error="restic backup failed: connection refused")
    rt.db.set_setting("down_since:local", "2026-11-17T17:45:00Z")
    issues = _issues(client)
    assert set(issues) - {"no_channel:*"} == {"destination_unreachable:local:*"}
    assert "resume on their own" in issues["destination_unreachable:local:*"]["message"]
    rt.db.delete_setting("down_since:local")  # it answered again
    assert "destination_unreachable:local:*" not in _issues(client)
