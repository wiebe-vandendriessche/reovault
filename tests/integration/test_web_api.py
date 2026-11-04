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


def test_the_overview_lists_destinations_a_timeline_and_the_password_notice(backed_up):
    _rt, client = backed_up
    ov = client.get("/api/v1/overview").json()
    (dest,) = ov["destinations"]
    assert (dest["name"], dest["role"]) == ("local", "primary")
    assert dest["used_bytes"] > 0 and dest["snapshots"] >= 1
    (cam,) = ov["cameras"]
    assert len(cam["timeline"]) == 48
    assert sum(b["backed_up"] for b in cam["timeline"]) == cam["backed_up_clips"]
    assert [n["title"] for n in ov["notices"]] == ["Keep your restic password safe"]
    assert client.get("/api/v1/cameras").json()[0]["timeline"] is None  # only on the overview


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
