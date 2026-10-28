"""The /api/v1 surface the dashboard uses, over a demo camera that was really
backed up to a temp restic repository."""

from __future__ import annotations

import io
import tomllib
import zipfile
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
    rt.workers.by_alias["demo"] = worker  # registered, but no thread: runs inline
    worker._backup(rt.settings(), rt.settings().camera("demo"))
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
    assert ov["destination"]["clips"] == cam["backed_up_clips"]
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


def test_a_day_downloads_as_one_zip(backed_up):
    _rt, client = backed_up
    day = client.get("/api/v1/clips/day?camera=demo").json()
    backed = [c for h in day["hours"] for c in h["clips"] if c["state"] == "backed_up"]
    r = client.get(f"/api/v1/clips/day.zip?camera=demo&date_={day['day']}")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert len(z.namelist()) == len(backed)
    assert z.testzip() is None


def test_lost_clips_show_in_activity_and_cant_be_downloaded(backed_up):
    _rt, client = backed_up
    act = client.get("/api/v1/activity?camera=demo").json()
    assert act["runs"] and act["lost"]
    lost = act["lost"][0]
    assert client.get(f"/api/v1/clips/{lost['id']}/download").status_code == 404


def test_adding_cameras(backed_up, env):
    _rt, client = backed_up
    r = client.post(
        "/api/v1/cameras", json={"alias": "yard", "timezone": "Europe/Brussels", "demo": True}
    )
    assert r.status_code == 201, r.text
    assert [c["alias"] for c in r.json()] == ["demo", "yard"]
    assert tomllib.loads(env.toml.read_text())["cameras"][1] == {
        "alias": "yard",
        "timezone": "Europe/Brussels",
        "demo": True,
    }
    missing_creds = {"alias": "real", "timezone": "Europe/Brussels"}
    assert client.post("/api/v1/cameras", json=missing_creds).status_code == 422
    bad_tz = {"alias": "x", "timezone": "Mars/Base", "demo": True}
    assert client.post("/api/v1/cameras", json=bad_tz).status_code == 422
    assert client.post("/api/v1/cameras", json={**bad_tz, "alias": "../etc"}).status_code == 422


def test_destination_test_reports_each_camera(backed_up, env):
    _rt, client = backed_up
    ok = client.post("/api/v1/config/backup/test", json={"repository": str(env.tmp_path / "repos")})
    assert ok.json() == [{"camera": "demo", "state": "ok", "error": None}]
    new = client.post("/api/v1/config/backup/test", json={"repository": str(env.tmp_path / "new")})
    assert new.json()[0]["state"] == "missing"


def test_run_now_and_stop_need_a_running_worker(backed_up):
    _rt, client = backed_up
    assert client.post("/api/v1/cameras/demo/stop").status_code == 409  # nothing running
    assert client.post("/api/v1/cameras/nope/run").status_code == 404


def test_errors_never_leak_repository_credentials(backed_up):
    rt, _client = backed_up
    text = "restic open failed: rest:https://alice:s3cret@backup.lan/reovault/demo"
    assert "s3cret" not in rt.redact(text)
    assert "alice:s3cret" not in rt.scrub_credentials(text)
