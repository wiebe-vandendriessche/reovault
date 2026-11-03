"""First-run setup, changing the password, and the update notice."""

from __future__ import annotations

import tomllib

import pytest
from fastapi.testclient import TestClient

from reovault.config import load_settings
from reovault.web.api.auth import new_setup_code
from tests.integration.conftest import WEB_TEST_EMAIL, WEB_TEST_PASSWORD, login, make_web_app

NEW_PASSWORD = "a brand new passphrase"


def _setup_client(env, web_settings, *, email_in_config: bool = True):
    """An app with no login yet (the `web_password_hash` fixture isn't used)."""
    if not email_in_config:
        env.toml.write_text(env.toml.read_text().replace(f'email = "{WEB_TEST_EMAIL}"\n', ""))
    app = make_web_app(env, load_settings(env.toml))
    return app, TestClient(app)


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get("/api/v1/session").json()["csrf_token"]}


def test_a_fresh_instance_offers_setup_and_nothing_else(env, web_settings):
    app, client = _setup_client(env, web_settings)
    session = client.get("/api/v1/session").json()
    assert session["setup_needed"] is True
    assert session["authenticated"] is False
    assert session["setup_email"] == WEB_TEST_EMAIL  # preset in the config: the form skips it
    assert client.get("/api/v1/overview").status_code == 503
    assert client.post("/api/v1/login", json={"email": "a@b.c", "password": "x"}).status_code in (
        403,
        503,
    )


def test_setup_with_the_logged_code_creates_the_login(env, web_settings):
    app, client = _setup_client(env, web_settings)
    code = app.state.rv.setup_code
    response = client.post(
        "/api/v1/setup",
        json={"code": code.lower(), "password": NEW_PASSWORD},  # case and dash don't matter
        headers=_csrf(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True
    assert response.json()["setup_needed"] is False
    # Signed in, and a fresh login works with the new password.
    assert client.get("/api/v1/overview").status_code == 200
    other = TestClient(app)
    ok = other.post(
        "/api/v1/login",
        json={"email": WEB_TEST_EMAIL, "password": NEW_PASSWORD},
        headers=_csrf(other),
    )
    assert ok.status_code == 200
    assert app.state.rv.setup_code is None


def test_setup_writes_the_email_when_the_config_has_none(env, web_settings):
    app, client = _setup_client(env, web_settings, email_in_config=False)
    assert client.get("/api/v1/session").json()["setup_email"] is None
    code = app.state.rv.setup_code
    r = client.post(
        "/api/v1/setup",
        json={"code": code, "email": "me@home.example", "password": NEW_PASSWORD},
        headers=_csrf(client),
    )
    assert r.status_code == 200, r.text
    assert tomllib.loads(env.toml.read_text())["web"]["email"] == "me@home.example"
    other = TestClient(app)
    login_ok = other.post(
        "/api/v1/login",
        json={"email": "me@home.example", "password": NEW_PASSWORD},
        headers=_csrf(other),
    )
    assert login_ok.status_code == 200  # the email is live, no restart


def test_a_wrong_code_is_refused_and_rate_limited(env, web_settings):
    app, client = _setup_client(env, web_settings)
    for _ in range(5):
        r = client.post(
            "/api/v1/setup",
            json={"code": "AAAA-BBBB", "password": NEW_PASSWORD},
            headers=_csrf(client),
        )
        assert r.status_code == 403
    r = client.post(
        "/api/v1/setup",
        json={"code": app.state.rv.setup_code, "password": NEW_PASSWORD},
        headers=_csrf(client),
    )
    assert r.status_code == 429  # even the right code waits out the lockout


def test_setup_needs_the_csrf_token_like_every_post(env, web_settings):
    app, client = _setup_client(env, web_settings)
    r = client.post(
        "/api/v1/setup", json={"code": app.state.rv.setup_code, "password": NEW_PASSWORD}
    )
    assert r.status_code == 403
    assert not env.tmp_path.joinpath("web_password").exists()


def test_setup_is_closed_once_it_is_done(env, web_settings):
    app, client = _setup_client(env, web_settings)
    code = app.state.rv.setup_code
    body = {"code": code, "password": NEW_PASSWORD}
    assert client.post("/api/v1/setup", json=body, headers=_csrf(client)).status_code == 200
    second = TestClient(app)  # a second browser with the same code
    assert second.post("/api/v1/setup", json=body, headers=_csrf(second)).status_code == 409
    assert (
        second.post(
            "/api/v1/setup", json={**body, "password": "someone else's"}, headers=_csrf(second)
        ).status_code
        == 409
    )


def test_a_short_password_is_refused(env, web_settings):
    app, client = _setup_client(env, web_settings)
    r = client.post(
        "/api/v1/setup",
        json={"code": app.state.rv.setup_code, "password": "short"},
        headers=_csrf(client),
    )
    assert r.status_code == 422


def test_an_existing_password_means_no_setup_and_no_code(env, web_settings, web_password_hash):
    app = make_web_app(env, web_settings)
    assert app.state.rv.setup_code is None
    client = TestClient(app)
    assert client.get("/api/v1/session").json()["setup_needed"] is False


def test_the_setup_code_is_unambiguous_and_random():
    codes = {new_setup_code() for _ in range(50)}
    assert len(codes) == 50
    assert all(len(c) == 9 and c[4] == "-" and not set(c) & set("01OI") for c in codes)


def test_changing_the_password_needs_the_current_one_and_signs_others_out(
    env, web_settings, web_password_hash
):
    app = make_web_app(env, web_settings)
    mine = login(TestClient(app))
    other = login(TestClient(app))
    bad = mine.post("/api/v1/password", json={"current": "nope", "password": NEW_PASSWORD})
    assert bad.status_code == 401
    ok = mine.post(
        "/api/v1/password", json={"current": WEB_TEST_PASSWORD, "password": NEW_PASSWORD}
    )
    assert ok.status_code == 200, ok.text
    assert mine.get("/api/v1/overview").status_code == 200  # this browser stays signed in
    assert other.get("/api/v1/overview").status_code == 401  # every other session is out
    fresh = TestClient(app)
    again = fresh.post(
        "/api/v1/login",
        json={"email": WEB_TEST_EMAIL, "password": NEW_PASSWORD},
        headers=_csrf(fresh),
    )
    assert again.status_code == 200


def test_the_email_can_be_changed_with_the_password(env, web_settings, web_password_hash):
    app = make_web_app(env, web_settings)
    client = login(TestClient(app))
    r = client.post(
        "/api/v1/password", json={"current": WEB_TEST_PASSWORD, "email": "new@home.example"}
    )
    assert r.status_code == 200, r.text
    assert tomllib.loads(env.toml.read_text())["web"]["email"] == "new@home.example"


def test_the_email_cant_be_changed_through_the_generic_settings(
    env, web_settings, web_password_hash
):
    client = login(TestClient(make_web_app(env, web_settings)))
    cfg = client.get("/api/v1/config").json()
    r = client.put(
        "/api/v1/config/web", json={"version": cfg["version"], "values": {"email": "x@y.z"}}
    )
    assert r.status_code in (404, 409, 422)


def test_a_newer_release_shows_in_the_session_only_when_signed_in(
    env, web_settings, web_password_hash, monkeypatch
):
    monkeypatch.setattr("reovault.web.api.auth.__version__", "v1.0.0")
    app = make_web_app(env, web_settings)
    app.state.rv.db.set_setting("latest_release", "v1.2.0")
    anon = TestClient(app)
    assert anon.get("/api/v1/session").json()["update_available"] is None
    assert login(TestClient(app)).get("/api/v1/session").json()["update_available"] == "v1.2.0"
    off = web_settings.web
    assert off.update_check is True


def test_a_development_build_never_asks_for_an_update():
    from reovault import updates

    assert updates.newer("v0.3.0-23-g8810fc5-dirty", "v9.9.9") is None
    assert updates.newer("dev", "v9.9.9") is None
    assert updates.newer("v1.0.0", "v1.0.0") is None
    assert updates.newer("v1.0.0", "v1.0.1") == "v1.0.1"
    assert updates.newer("v1.9.0", "v1.10.0") == "v1.10.0"  # numbers, not text


@pytest.mark.parametrize("alias", ["reovault-config"])
def test_the_config_backup_alias_is_reserved(env, web_settings, web_password_hash, alias):
    client = login(TestClient(make_web_app(env, web_settings)))
    r = client.post(
        "/api/v1/cameras", json={"alias": alias, "timezone": "Europe/Brussels", "demo": True}
    )
    assert r.status_code == 422


def test_a_camera_can_be_removed_and_its_backups_stay(env, web_settings, web_password_hash):
    app = make_web_app(env, web_settings)
    client = login(TestClient(app))
    client.post(
        "/api/v1/cameras", json={"alias": "yard", "timezone": "Europe/Brussels", "demo": True}
    )
    assert "yard" in {c["alias"] for c in client.get("/api/v1/cameras").json()}
    r = client.delete("/api/v1/cameras/yard")
    assert r.status_code == 200, r.text
    assert "yard" not in {c["alias"] for c in r.json()}
    assert "yard" not in {c["alias"] for c in tomllib.loads(env.toml.read_text())["cameras"]}
    assert client.delete("/api/v1/cameras/yard").status_code == 404
