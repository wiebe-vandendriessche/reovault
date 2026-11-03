"""The recovery tab: where the password lives (never its content), a plain-text
sheet, and the "I stored it" reminder."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from reovault.web.app import create_app
from tests.integration.conftest import login, make_runtime


@pytest.fixture
def api(env, web_settings, web_password_hash):
    rt = make_runtime(env, web_settings)
    return rt, login(TestClient(create_app(rt, dashboard_dir=env.tmp_path / "no-dist"))), env


SECRET = "S3cret-restic-pw-9f2c1e"


def test_it_names_the_password_file_but_never_shows_the_password(api):
    _rt, client, env = api
    (env.tmp_path / "restic_password").write_text(SECRET)
    r = client.get("/api/v1/recovery")
    body = r.json()
    assert body["password_file"] == str(env.tmp_path / "restic_password")
    assert body["password_file_found"] is True and body["confirmed_at"] is None
    assert [(d["name"], d["role"]) for d in body["destinations"]] == [("local", "primary")]
    assert SECRET not in r.text
    assert SECRET not in client.get("/api/v1/recovery/sheet").text


def test_the_sheet_is_a_plain_text_download_with_the_restore_steps(api):
    _rt, client, env = api
    r = client.get("/api/v1/recovery/sheet")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "attachment" in r.headers["content-disposition"]
    assert str(env.tmp_path / "repos") in r.text  # the repository to restore from
    assert "demo" in r.text and 'restic -r "' in r.text
    assert "reovault-config" in r.text  # how to get ReoVault itself back


def test_the_sheet_hides_repository_credentials(api):
    rt, client, _env = api
    from reovault.config_store import upsert_destination_entry

    def add(doc):
        upsert_destination_entry(doc, "local", primary=True)  # two destinations: say which
        upsert_destination_entry(
            doc, "rest", repository="rest:https://alice:hunter2@backup.example/x"
        )

    rt.config.update(add)
    text = client.get("/api/v1/recovery/sheet").text
    assert "hunter2" not in text and "backup.example" in text
    assert "hunter2" not in client.get("/api/v1/recovery").text


def test_confirming_hides_the_overview_notice(api):
    rt, client, _env = api
    now = datetime.now(UTC)
    rt.db.upsert_clips("demo", [("c1", now, None, 10, "md")])
    (clip,) = rt.db.clips_by_name("demo", ["c1"]).values()
    rt.db.mark_backed_up("demo", "local", "s1", now, {clip.id: "/b/c1.mp4"})

    def titles() -> list[str]:
        return [n["title"] for n in client.get("/api/v1/overview").json()["notices"]]

    assert titles() == ["Keep your restic password safe"]
    assert client.post("/api/v1/recovery/confirm").json()["confirmed_at"] is not None
    assert titles() == []


def test_a_fresh_install_has_no_notice_yet(api):
    _rt, client, _env = api
    assert client.get("/api/v1/overview").json()["notices"] == []  # nothing is backed up


def test_recovery_needs_a_login(env, web_settings, web_password_hash):
    client = TestClient(
        create_app(make_runtime(env, web_settings), dashboard_dir=env.tmp_path / "x")
    )
    assert client.get("/api/v1/recovery").status_code == 401
    assert client.get("/api/v1/recovery/sheet").status_code == 401
    assert client.post("/api/v1/recovery/confirm").status_code in (401, 403)
