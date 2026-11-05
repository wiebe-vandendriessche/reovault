"""Connecting, limiting, switching and disconnecting destinations from the API."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from reovault import rclone
from reovault.config import load_settings
from reovault.web.app import create_app
from tests.integration.conftest import WEB_TEST_PASSWORD, login, make_runtime

T = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def api(env, web_settings, web_password_hash):
    rt = make_runtime(env, web_settings)
    client = login(TestClient(create_app(rt, dashboard_dir=env.tmp_path / "no-dist")))
    return rt, client, env


def _toml(env) -> dict:
    return tomllib.loads(env.toml.read_text())


def _names(client) -> dict[str, str]:
    return {d["name"]: d["role"] for d in client.get("/api/v1/destinations").json()}


def test_the_configured_destination_is_listed_as_primary(api):
    _rt, client, _env = api
    (dest,) = client.get("/api/v1/destinations").json()
    assert (dest["name"], dest["role"]) == ("local", "primary")
    assert dest["keep_days"] == 0 and dest["max_gb"] == 0 and dest["error"] is None


def test_a_local_folder_connects_as_a_copy_with_limits(api, tmp_path):
    _rt, client, env = api
    folder = tmp_path / "disk2"
    folder.mkdir()
    r = client.post(
        "/api/v1/destinations",
        json={"name": "usb", "kind": "local", "path": str(folder), "keep_days": 30, "max_gb": 200},
    )
    assert r.status_code == 201, r.text
    assert _names(client) == {"local": "primary", "usb": "copy"}
    entry = next(d for d in _toml(env)["destinations"] if d["name"] == "usb")
    assert entry == {"name": "usb", "repository": str(folder), "keep_days": 30, "max_gb": 200}


def test_a_missing_folder_is_refused_not_created(api, tmp_path):
    _rt, client, _env = api
    r = client.post(
        "/api/v1/destinations",
        json={"name": "usb", "kind": "local", "path": str(tmp_path / "nope")},
    )
    assert r.status_code == 422 and "unmounted" in r.json()["detail"]
    assert not (tmp_path / "nope").exists()


@pytest.mark.parametrize(
    "body",
    [
        {"name": "local", "kind": "local", "path": "/tmp"},  # taken
        {"name": "bad name!", "kind": "local", "path": "/tmp"},
        {"name": "x", "kind": "local", "path": "relative/path"},
        {"name": "x", "kind": "nonsense"},
    ],
)
def test_bad_connect_requests_are_refused(api, body):
    _rt, client, env = api
    before = env.toml.read_text()
    assert client.post("/api/v1/destinations", json=body).status_code in (409, 422)
    assert env.toml.read_text() == before


def test_a_dead_remote_is_reported_and_leaves_nothing_behind(api, monkeypatch, tmp_path):
    rt, client, env = api
    conf = tmp_path / "rclone.conf"
    conf.write_text("")
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    import shutil

    monkeypatch.setattr(rt.config.current.tools, "rclone", shutil.which("rclone") or "rclone")
    r = client.post(
        "/api/v1/destinations",
        json={
            "name": "dead",
            "kind": "nextcloud",
            "values": {
                "url": "http://127.0.0.1:9/",
                "user": "u",
                "pass": "topsecret",
                "vendor": "other",
            },
        },
    )
    assert r.status_code == 422
    assert "topsecret" not in r.text
    assert "reovault-dead" not in conf.read_text()  # the half-made remote was removed
    assert "dead" not in _names(client)
    assert "dead" not in {d["name"] for d in _toml(env)["destinations"]}


def test_tightening_a_limit_needs_the_password_loosening_does_not(api):
    _rt, client, env = api
    base = {"keep_days": 0, "max_gb": 0, "enabled": True}
    assert (
        client.put("/api/v1/destinations/local", json={**base, "keep_days": 30}).status_code == 401
    )
    assert "keep_days" not in _toml(env)["destinations"][0]
    ok = client.put(
        "/api/v1/destinations/local",
        json={**base, "keep_days": 30, "confirm_password": WEB_TEST_PASSWORD},
    )
    assert ok.status_code == 200
    # Raising or removing a limit deletes nothing: no confirmation.
    assert (
        client.put("/api/v1/destinations/local", json={**base, "keep_days": 60}).status_code == 200
    )
    assert client.put("/api/v1/destinations/local", json=base).status_code == 200
    assert "keep_days" not in _toml(env)["destinations"][0]


def test_a_wrong_password_counts_toward_the_lockout(api):
    _rt, client, _env = api
    base = {"keep_days": 5, "max_gb": 0, "enabled": True, "confirm_password": "wrong"}
    codes = [client.put("/api/v1/destinations/local", json=base).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and 429 in codes[5:]


def test_make_primary_waits_until_the_copy_has_everything(api, tmp_path):
    rt, client, env = api
    folder = tmp_path / "disk2"
    folder.mkdir()
    client.post("/api/v1/destinations", json={"name": "usb", "kind": "local", "path": str(folder)})
    rt.db.set_locations("demo", "local", [("s1", "s1", T)])  # the primary holds a snapshot ...
    confirm = {"confirm_password": WEB_TEST_PASSWORD}
    r = client.post("/api/v1/destinations/usb/primary", json=confirm)
    assert r.status_code == 409 and "still copying" in r.json()["detail"]
    assert _names(client)["local"] == "primary"
    rt.db.set_locations("demo", "usb", [("s1", "copy-of-s1", T)])  # ... and the copy caught up
    assert client.post("/api/v1/destinations/usb/primary", json={}).status_code == 401
    r = client.post("/api/v1/destinations/usb/primary", json=confirm)
    assert r.status_code == 200, r.text
    assert _names(client) == {"local": "copy", "usb": "primary"}
    assert [d["name"] for d in _toml(env)["destinations"] if d.get("primary")] == ["usb"]


def test_the_primary_cant_be_disconnected_while_copies_depend_on_it(api, tmp_path):
    _rt, client, _env = api
    folder = tmp_path / "disk2"
    folder.mkdir()
    client.post("/api/v1/destinations", json={"name": "usb", "kind": "local", "path": str(folder)})
    r = client.request(
        "DELETE", "/api/v1/destinations/local", json={"confirm_password": WEB_TEST_PASSWORD}
    )
    assert r.status_code == 409
    assert "local" in _names(client)


def test_disconnecting_a_copy_needs_the_password_and_keeps_its_rows(api, tmp_path):
    rt, client, env = api
    folder = tmp_path / "disk2"
    folder.mkdir()
    client.post("/api/v1/destinations", json={"name": "usb", "kind": "local", "path": str(folder)})
    rt.db.set_locations("demo", "usb", [("s1", "x", T)])
    assert client.delete("/api/v1/destinations/usb").status_code == 401
    r = client.request(
        "DELETE", "/api/v1/destinations/usb", json={"confirm_password": WEB_TEST_PASSWORD}
    )
    assert r.status_code == 200
    assert _names(client) == {"local": "primary"}
    assert rt.db.location_roots("demo", "usb") == {"s1"}  # remembered: reconnecting picks it up
    assert folder.exists()  # and nothing on the destination was touched


def test_only_one_destination_may_be_primary(api, tmp_path):
    _rt, client, env = api
    for n in ("a", "b"):
        (tmp_path / n).mkdir()
        client.post(
            "/api/v1/destinations", json={"name": n, "kind": "local", "path": str(tmp_path / n)}
        )
    settings = load_settings(env.toml)
    assert [d.name for d in settings.copies()] == ["a", "b"]
    assert sum(d.primary for d in settings.destinations) == 1


def test_the_preview_says_what_a_limit_would_delete(api):
    rt, client, _env = api
    rt.db.upsert_clips("demo", [("c1", T, None, 1000, "md")])
    (clip,) = rt.db.clips_by_name("demo", ["c1"]).values()
    old = datetime(2026, 9, 1, tzinfo=UTC)
    rt.db.mark_backed_up("demo", "local", "s-old", old, {clip.id: "/b/c1.mp4"})
    rt.db.upsert_clips("demo", [("c2", T, None, 500, "md")])
    (c2,) = rt.db.clips_by_name("demo", ["c2"]).values()
    rt.db.mark_backed_up("demo", "local", "s-new", T, {c2.id: "/b/c2.mp4"})
    r = client.get("/api/v1/destinations/local/preview", params={"keep_days": 14, "max_gb": 0})
    assert r.json() == {"snapshots": 1, "bytes": 1000}
    r = client.get("/api/v1/destinations/local/preview", params={"keep_days": 0, "max_gb": 0})
    assert r.json() == {"snapshots": 0, "bytes": 0}


def test_every_destination_route_needs_a_login(env, web_settings, web_password_hash):
    client = TestClient(
        create_app(make_runtime(env, web_settings), dashboard_dir=env.tmp_path / "x")
    )
    assert client.get("/api/v1/destinations").status_code == 401
    assert client.get("/api/v1/destinations/kinds").status_code == 401


def test_the_kinds_list_matches_what_rclone_creates():
    assert {t.id for t in rclone.TYPES} >= {"nextcloud", "sftp", "s3", "b2", "onedrive", "drive"}


def test_a_reused_name_does_not_inherit_the_old_destinations_memory(api, tmp_path):
    rt, client, _env = api
    old, new = tmp_path / "nas", tmp_path / "bucket"
    old.mkdir()
    new.mkdir()
    body = {"name": "offsite", "kind": "local"}
    client.post("/api/v1/destinations", json={**body, "path": str(old)})
    rt.db.set_locations("demo", "offsite", [("s1", "x", T)])
    rt.db.mark_repo_seen("offsite", "demo")
    client.request(
        "DELETE", "/api/v1/destinations/offsite", json={"confirm_password": WEB_TEST_PASSWORD}
    )
    client.post("/api/v1/destinations", json={**body, "path": str(new)})
    assert rt.db.location_roots("demo", "offsite") == set()
    assert not rt.db.repo_seen("offsite", "demo")


def test_turning_an_old_primary_back_on_does_not_make_two(api, tmp_path):
    rt, client, env = api
    (tmp_path / "b").mkdir()
    # Sole destination off (allowed: nothing depends on it), another becomes primary.
    assert (
        client.put(
            "/api/v1/destinations/local",
            json={
                "keep_days": 0,
                "max_gb": 0,
                "enabled": False,
                "confirm_password": WEB_TEST_PASSWORD,
            },
        ).status_code
        == 200
    )
    client.post(
        "/api/v1/destinations", json={"name": "b", "kind": "local", "path": str(tmp_path / "b")}
    )
    r = client.put(
        "/api/v1/destinations/local", json={"keep_days": 0, "max_gb": 0, "enabled": True}
    )
    assert r.status_code == 200, r.text
    assert sum(d.primary and d.enabled for d in load_settings(env.toml).destinations) <= 1
