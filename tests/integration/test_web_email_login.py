"""Email on the login: with one account it adds no real security, but the
check must still never short-circuit (a wrong email should cost the same
latency as a wrong password), and the usual normalization (strip/casefold)
should make case and whitespace not matter.
"""

from reovault.web import security
from tests.integration.conftest import WEB_TEST_EMAIL, WEB_TEST_PASSWORD, csrf_token


def _login(client, body: dict):
    return client.post("/api/v1/login", json=body, headers={"X-CSRF-Token": csrf_token(client)})


def test_correct_email_and_password_logs_in(client):
    r = _login(client, {"email": WEB_TEST_EMAIL, "password": WEB_TEST_PASSWORD})
    assert r.status_code == 200
    assert "rv_session" in r.cookies


def test_wrong_email_with_correct_password_fails(client):
    r = _login(client, {"email": "someone-else@example.com", "password": WEB_TEST_PASSWORD})
    assert r.status_code == 401
    assert "rv_session" not in r.cookies
    assert r.json()["detail"] == "Wrong email or password."


def test_correct_email_with_wrong_password_fails(client):
    r = _login(client, {"email": WEB_TEST_EMAIL, "password": "wrong"})
    assert r.status_code == 401
    assert "rv_session" not in r.cookies
    # Same message either way: the response never says which field was wrong.
    assert r.json()["detail"] == "Wrong email or password."


def test_email_is_normalized_by_case_and_whitespace(client):
    r = _login(client, {"email": f"  {WEB_TEST_EMAIL.upper()}  ", "password": WEB_TEST_PASSWORD})
    assert r.status_code == 200
    assert "rv_session" in r.cookies


def test_missing_email_field_fails(client):
    r = _login(client, {"password": WEB_TEST_PASSWORD})
    assert r.status_code == 422
    assert "rv_session" not in r.cookies


def test_wrong_email_always_runs_password_verification(client, monkeypatch):
    """The anti-timing property itself: verify_password must be called even
    when the email is wrong, not skipped by a short-circuited `and`."""
    calls = []
    original = security.verify_password

    def spy(password, password_hash):
        calls.append(1)
        return original(password, password_hash)

    monkeypatch.setattr(security, "verify_password", spy)
    r = _login(client, {"email": "wrong@example.com", "password": WEB_TEST_PASSWORD})

    assert r.status_code == 401
    assert calls == [1]
