"""Range downloads: an hour, a day, several days; one camera or several; all or
some detection types; from the primary or from a copy."""

from __future__ import annotations

import io
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from reovault.web import app as web_app
from reovault.web.api import download
from reovault.web.app import create_app
from reovault.worker import Worker
from tests.integration.conftest import WEB_TEST_EMAIL, WEB_TEST_PASSWORD, login, make_runtime

BRUSSELS = ZoneInfo("Europe/Brussels")


def _local(days_ago: int = 0) -> str:
    return (datetime.now(BRUSSELS) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture(scope="module")
def two_cameras(tmp_path_factory):
    """Two demo cameras, really backed up once and shared by this file's tests
    (each backup runs restic, so doing it per test would take minutes)."""
    import os

    from reovault.config import load_settings
    from reovault.web import security
    from reovault.web.auth import write_password_file

    restic = shutil.which("restic")
    if restic is None:
        pytest.fail("restic is not on PATH (python3 scripts/fetch_tools.py .tools)")
    tmp = tmp_path_factory.mktemp("download")
    (tmp / "pw").write_text("pw")
    toml = tmp / "reovault.toml"
    toml.write_text(
        f"""
[[cameras]]
id = "demo"
timezone = "Europe/Brussels"
demo = true

[[cameras]]
id = "yard"
timezone = "Europe/Brussels"
demo = true

[[destinations]]
name = "local"
repository = "{tmp / "repos"}"

[backup]
password_file = "{tmp / "pw"}"

[web]
email = "{WEB_TEST_EMAIL}"
cookie_secure = false
password_file = "{tmp / "web_password"}"
session_key_path = "{tmp / "session.key"}"

[storage]
data_dir = "{tmp / "data"}"
staging_dir = "{tmp / "staging"}"

[tools]
restic = "{restic}"
"""
    )
    write_password_file(tmp / "web_password", security.hash_password(WEB_TEST_PASSWORD.encode()))
    with pytest.MonkeyPatch.context() as mp:
        for key in [k for k in os.environ if k.startswith("REOVAULT_")]:
            mp.delenv(key)
        settings = load_settings(toml)
    rt = make_runtime(SimpleNamespace(toml=toml), settings)
    for cam_id in ("demo", "yard"):
        w = Worker(cam_id, rt.settings, rt.db, rt.gateway, rt.bus.publish)
        rt.workers.by_id[cam_id] = w
        w._backup(rt.settings(), rt.settings().camera(cam_id))
    client = login(TestClient(create_app(rt, dashboard_dir=tmp / "no-dist")))
    return rt, client


def _zip(client, **params) -> zipfile.ZipFile:
    r = client.get("/api/v1/download/zip", params=params)
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content))


def test_a_span_is_one_zip_with_a_folder_per_camera_and_day(two_cameras):
    _rt, client = two_cameras
    day = _local(1)
    p = {"cameras": "demo", "start": f"{day}T10:00", "end": f"{day}T12:00"}
    preview = client.get("/api/v1/download/preview", params=p).json()
    z = _zip(client, **p)
    assert z.testzip() is None
    names = z.namelist()
    assert len(names) == preview["clips"] == 8  # the demo card has a clip every 15 minutes
    assert all(n.startswith(f"demo/{day}/") and n.endswith(".mp4") for n in names)
    assert sum(i.file_size for i in z.infolist()) == preview["bytes"]
    assert names == sorted(names)  # in time order


def test_both_cameras_come_in_separate_folders(two_cameras):
    _rt, client = two_cameras
    day = _local(1)
    p = {"cameras": "demo,yard", "start": f"{day}T10:00", "end": f"{day}T11:00"}
    z = _zip(client, **p)
    assert {n.split("/")[0] for n in z.namelist()} == {"demo", "yard"}
    assert len(z.namelist()) == 8
    assert all(n.split("/")[1] == day for n in z.namelist())


def test_several_days_are_counted_across_the_midnights(two_cameras):
    _rt, client = two_cameras
    p = {"cameras": "demo,yard", "start": f"{_local(2)}T00:00", "end": f"{_local(-1)}T00:00"}
    preview = client.get("/api/v1/download/preview", params=p).json()
    per_camera = {c["camera"]: c["clips"] for c in preview["cameras"]}
    assert per_camera["demo"] == per_camera["yard"] > 2 * 96  # at least two full days
    assert preview["clips"] == sum(per_camera.values())


def test_only_the_chosen_detection_types(two_cameras):
    _rt, client = two_cameras
    day = _local(1)
    base = {"cameras": "demo", "start": f"{day}T00:00", "end": f"{_local()}T00:00"}
    everything = client.get("/api/v1/download/preview", params=base).json()["clips"]
    people = client.get("/api/v1/download/preview", params={**base, "types": "people"}).json()
    vehicles = client.get("/api/v1/download/preview", params={**base, "types": "vehicle"}).json()
    both = client.get("/api/v1/download/preview", params={**base, "types": "people,vehicle"}).json()
    assert 0 < people["clips"] < everything and 0 < vehicles["clips"] < everything
    assert both["clips"] >= max(people["clips"], vehicles["clips"])  # either type counts


def test_a_span_with_nothing_backed_up_is_refused_not_an_empty_zip(two_cameras):
    _rt, client = two_cameras
    p = {"cameras": "demo", "start": "2020-01-01T00:00", "end": "2020-01-02T00:00"}
    assert client.get("/api/v1/download/preview", params=p).json()["clips"] == 0
    assert client.get("/api/v1/download/zip", params=p).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"cameras": "", "start": "2026-11-01T00:00", "end": "2026-11-02T00:00"},
        {"cameras": "nope", "start": "2026-11-01T00:00", "end": "2026-11-02T00:00"},
        {"cameras": "demo", "start": "2026-11-02T00:00", "end": "2026-11-01T00:00"},
        {"cameras": "demo", "start": "garbage", "end": "2026-11-01T00:00"},
    ],
)
def test_bad_spans_are_refused(two_cameras, params):
    _rt, client = two_cameras
    assert client.get("/api/v1/download/preview", params=params).status_code in (404, 422)


def test_too_many_clips_asks_for_a_shorter_span(two_cameras, monkeypatch):
    _rt, client = two_cameras
    monkeypatch.setattr(download, "MAX_CLIPS", 10)
    p = {"cameras": "demo", "start": f"{_local(2)}T00:00", "end": f"{_local(-1)}T00:00"}
    r = client.get("/api/v1/download/preview", params=p)
    assert r.status_code == 422 and "shorter" in r.json()["detail"]


def test_clock_changes_are_handled_in_camera_time(two_cameras):
    """The day of the 25 h fall-back (2026-10-25 in Brussels) is 25 hours."""
    rt, client = two_cameras
    from reovault.db import iso

    rt.db.upsert_clips(
        "demo", [("c-dst", datetime(2026, 10, 25, 0, 30, tzinfo=UTC), None, 10, "md")]
    )
    (c,) = rt.db.clips_by_name("demo", ["c-dst"]).values()
    rt.db.mark_backed_up("demo", "local", "s-dst", datetime.now(UTC), {c.id: "/b/c-dst.mp4"})
    p = {"cameras": "demo", "start": "2026-10-25T00:00", "end": "2026-10-26T00:00"}
    assert client.get("/api/v1/download/preview", params=p).json()["clips"] == 1
    assert iso(datetime(2026, 10, 25, 0, 30, tzinfo=UTC)) == c.start_utc


def test_a_clip_only_a_copy_still_holds_is_downloaded_from_the_copy(
    env, web_settings, web_password_hash, tmp_path
):
    """The primary forgot a snapshot (quota or age) that the copy kept."""
    from reovault.config import load_settings
    from reovault.destinations import DestinationWorker, restic_for

    folder = tmp_path / "repo2"
    env.toml.write_text(
        env.toml.read_text().replace(
            '[[destinations]]\nname = "local"',
            '[[destinations]]\nname = "local"\nprimary = true',
        )
        + f'\n[[destinations]]\nname = "second"\nrepository = "{folder}"\n'
    )
    settings = load_settings(env.toml)
    rt = make_runtime(env, settings)
    w = Worker("demo", rt.settings, rt.db, rt.gateway, rt.bus.publish)
    rt.workers.by_id["demo"] = w
    w._backup(settings, settings.camera("demo"))
    DestinationWorker("second", rt.settings, rt.db, lambda *a, **k: None).run_round()
    # The primary forgets everything (as if quota trimmed it); the copy keeps it.
    primary = restic_for(settings, "demo", settings.primary())
    snaps = primary.snapshots()
    primary.forget_ids([s.id for s in snaps[:-1]])
    rt.db.set_locations("demo", "local", [(s.root, s.id, s.time) for s in primary.snapshots()])
    client = login(TestClient(create_app(rt, dashboard_dir=tmp_path / "x")))
    day = _local(1)
    p = {"cameras": "demo", "start": f"{day}T00:00", "end": f"{_local(-1)}T00:00"}
    preview = client.get("/api/v1/download/preview", params=p).json()
    assert preview["clips"] > 0
    assert preview["cameras"][0]["on_unconnected_destination"] == 0
    small = {**p, "start": f"{day}T10:00", "end": f"{day}T11:00"}
    z = _zip(client, **small)
    assert z.testzip() is None and len(z.namelist()) == 4
    assert shutil.which("restic")  # real restic read these out of the copy


def test_downloads_need_a_login(env, web_settings, web_password_hash):
    client = TestClient(
        create_app(make_runtime(env, web_settings), dashboard_dir=env.tmp_path / "x")
    )
    p = {"cameras": "demo", "start": "2026-11-01T00:00", "end": "2026-11-02T00:00"}
    assert client.get("/api/v1/download/preview", params=p).status_code == 401
    assert client.get("/api/v1/download/zip", params=p).status_code == 401
    assert web_app is not None
