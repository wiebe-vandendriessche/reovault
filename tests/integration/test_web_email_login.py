"""Email on the login: with one account it adds no real security, but the
check must still never short-circuit (a wrong email should cost the same
latency as a wrong password), and the usual normalization (strip/casefold)
should make case and whitespace not matter.
"""

import pytest
from fastapi.testclient import TestClient

from reovault.config import Settings, WebConfig
from reovault.web import security
from reovault.web.auth import write_password_file
from tests.integration.conftest import Env, _extract_hidden_csrf, make_web_app

TEST_EMAIL = "user@example.com"
TEST_PASSWORD = "hunter2-hunter2"  # noqa: S105 - test fixture


@pytest.fixture
def web_settings_with_email(env: Env) -> Settings:
    from reovault.config import StorageConfig

    storage = StorageConfig(
        config_dir=env.tmp_path / "config",
        vault_dir=env.tmp_path / "vault",
        staging_dir=env.staging_dir,
        db_path=env.tmp_path / "reovault.db",
        master_key_path=env.tmp_path / "master.key",
    )
    web = WebConfig(
        email=TEST_EMAIL,
        password_file=env.tmp_path / "web_password",
        session_key_path=env.tmp_path / "web_session.key",
        cookie_secure=False,
        stream_chunk_bytes=16,
    )
    return Settings(storage=storage, web=web)


@pytest.fixture
def web_password_hash(web_settings_with_email: Settings) -> str:
    phc = security.hash_password(TEST_PASSWORD.encode())
    write_password_file(web_settings_with_email.web.password_file, phc)
    return phc


@pytest.fixture
def client(env, web_settings_with_email, web_device, web_password_hash) -> TestClient:
    app = make_web_app(env, web_settings_with_email, web_device)
    return TestClient(app, follow_redirects=False)


def test_correct_email_and_password_logs_in(client):
    csrf = _extract_hidden_csrf(client.get("/login").text)
    r = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": csrf, "next": "/"},
    )
    assert r.status_code == 303
    assert "rv_session" in r.cookies


def test_wrong_email_with_correct_password_fails(client):
    csrf = _extract_hidden_csrf(client.get("/login").text)
    r = client.post(
        "/login",
        data={
            "email": "someone-else@example.com",
            "password": TEST_PASSWORD,
            "csrf_token": csrf,
            "next": "/",
        },
    )
    assert r.status_code == 200
    assert "rv_session" not in r.cookies
    assert "Wrong email or password" in r.text


def test_correct_email_with_wrong_password_fails(client):
    csrf = _extract_hidden_csrf(client.get("/login").text)
    r = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": "wrong", "csrf_token": csrf, "next": "/"},
    )
    assert r.status_code == 200
    assert "rv_session" not in r.cookies


def test_email_is_normalized_by_case_and_whitespace(client):
    csrf = _extract_hidden_csrf(client.get("/login").text)
    r = client.post(
        "/login",
        data={
            "email": f"  {TEST_EMAIL.upper()}  ",
            "password": TEST_PASSWORD,
            "csrf_token": csrf,
            "next": "/",
        },
    )
    assert r.status_code == 303
    assert "rv_session" in r.cookies


def test_missing_email_field_fails(client):
    csrf = _extract_hidden_csrf(client.get("/login").text)
    r = client.post("/login", data={"password": TEST_PASSWORD, "csrf_token": csrf, "next": "/"})
    assert r.status_code == 200
    assert "rv_session" not in r.cookies


def test_login_form_has_autocomplete_hints_for_password_managers(client):
    html = client.get("/login").text
    assert 'autocomplete="username"' in html
    assert 'autocomplete="current-password"' in html


def test_wrong_email_always_runs_password_verification(client, monkeypatch):
    """The anti-timing property itself: verify_password must be called even
    when the email is wrong, not skipped by a short-circuited `and`."""
    calls = []
    original = security.verify_password

    def spy(password, password_hash):
        calls.append(1)
        return original(password, password_hash)

    monkeypatch.setattr(security, "verify_password", spy)
    csrf = _extract_hidden_csrf(client.get("/login").text)
    client.post(
        "/login",
        data={
            "email": "wrong@example.com",
            "password": TEST_PASSWORD,
            "csrf_token": csrf,
            "next": "/",
        },
    )
    assert calls == [1]
