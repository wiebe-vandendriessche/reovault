"""Auth enforcement on the `/api/v1` JSON API: every non-public route
requires a session, CSRF and Origin are enforced on every mutating route,
login/logout/revocation behave, and failed logins are rate limited.
"""

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from reovault.web import security
from reovault.web.auth import load_or_create_session_key
from tests.integration.conftest import (
    WEB_TEST_EMAIL,
    WEB_TEST_PASSWORD,
    csrf_token,
    login,
    make_web_app,
)

# The SPA catch-all is public by design: the built dashboard holds no data,
# only the code that asks the API for it, and the login screen is part of it.
_PUBLIC_ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/api/v1/session"),
    ("POST", "/api/v1/login"),
    ("GET", "/{path:path}"),
}

_PATH_PARAM_FILLS = {"recording_id": "1", "run_id": "1", "device_id": "1", "rest": "x"}

_MUTATING = ("POST", "PUT", "PATCH", "DELETE")


def _all_routes(app) -> list[tuple[str, str, str]]:
    """(method, route template, filled path). The template is what the
    public allowlist is keyed on, so a new route can't sneak in by
    happening to fill to a public-looking path.

    `iter_route_contexts` rather than `app.routes`: since FastAPI 0.14x an
    included router is one opaque `app.routes` entry, and walking only the
    top level would silently skip every `/api/v1` router."""
    routes = []
    for route in iter_route_contexts(app.routes):
        methods = route.methods
        path = route.path
        if not methods or not path:
            continue
        filled = route.path_format
        for name, value in _PATH_PARAM_FILLS.items():
            filled = filled.replace(f"{{{name}}}", value)
        for method in methods:
            if method == "HEAD":
                continue  # mirrors GET's auth requirement; not worth a second case
            routes.append((method, path, filled))
    return routes


def _mutating_api_routes(app) -> list[tuple[str, str]]:
    return sorted(
        {
            (method, filled)
            for method, template, filled in _all_routes(app)
            if method in _MUTATING and template.startswith("/api/v1/")
        }
    )


def _login_json(password: str = WEB_TEST_PASSWORD) -> dict[str, str]:
    return {"email": WEB_TEST_EMAIL, "password": password}


@pytest.fixture
def app_and_client(env, web_settings, web_device, web_password_hash):
    app = make_web_app(env, web_settings, web_device)
    return app, TestClient(app)


# -- every route requires auth ----------------------------------------------


def test_every_non_public_route_requires_auth(app_and_client):
    app, client = app_and_client
    routes = _all_routes(app)
    assert len(routes) > 20, "sanity: route enumeration should find the real route table"

    for method, template, path in routes:
        if (method, template) in _PUBLIC_ROUTES:
            continue
        response = client.request(method, path)
        detail = f"{method} {path} returned {response.status_code} unauthenticated"
        assert response.status_code == 401, detail
        assert response.json() == {"detail": "unauthorized"}, detail


def test_public_allowlist_matches_real_routes(app_and_client):
    """Guards the test above against a stale allowlist: an entry that no
    longer names a real route would silently stop meaning anything."""
    app, _client = app_and_client
    real = {(method, template) for method, template, _ in _all_routes(app)}
    assert real >= _PUBLIC_ROUTES


def test_session_and_healthz_are_reachable_without_auth(app_and_client):
    _app, client = app_and_client
    assert client.get("/healthz").status_code == 200
    body = client.get("/api/v1/session").json()
    assert body["authenticated"] is False
    assert body["csrf_token"]


# -- login / logout / session ----------------------------------------------


def test_login_sets_secure_cookie_attributes(env, web_settings, web_device, web_password_hash):
    web_settings.web.cookie_secure = True  # exercise the real attribute this once
    client = TestClient(make_web_app(env, web_settings, web_device))

    response = client.post(
        "/api/v1/login", json=_login_json(), headers={"X-CSRF-Token": csrf_token(client)}
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    set_cookie = response.headers["set-cookie"]
    assert "rv_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert f"Max-Age={30 * 86400}" in set_cookie


def test_login_omits_secure_cookie_attribute_when_disabled(app_and_client):
    """cookie_secure=False is what a direct-port, plain-HTTP deployment
    needs: a browser silently drops a Secure cookie sent over HTTP, which
    would otherwise bounce every login straight back to the login screen
    with no error. `web_settings` already defaults to False (tests run over
    plain http), so `app_and_client` exercises exactly that configuration."""
    _app, client = app_and_client

    response = client.post(
        "/api/v1/login", json=_login_json(), headers={"X-CSRF-Token": csrf_token(client)}
    )

    assert response.status_code == 200
    assert "Secure" not in response.headers["set-cookie"]


def test_session_reports_authenticated_after_login(app_and_client):
    _app, client = app_and_client
    login(client)

    assert client.get("/api/v1/session").json()["authenticated"] is True


def test_wrong_password_no_cookie_and_error_shown(app_and_client):
    _app, client = app_and_client

    response = client.post(
        "/api/v1/login", json=_login_json("wrong"), headers={"X-CSRF-Token": csrf_token(client)}
    )

    assert response.status_code == 401
    assert "rv_session" not in response.cookies
    assert response.json() == {"detail": "Wrong email or password."}


def test_six_failed_attempts_are_rate_limited(app_and_client):
    _app, client = app_and_client
    for _ in range(6):
        response = client.post(
            "/api/v1/login",
            json=_login_json("wrong"),
            headers={"X-CSRF-Token": csrf_token(client)},
        )
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_rate_limit_blocks_even_the_correct_password(app_and_client):
    """A lockout that a correct guess could slip through would tell an
    attacker which guess was right."""
    _app, client = app_and_client
    for _ in range(6):
        client.post(
            "/api/v1/login",
            json=_login_json("wrong"),
            headers={"X-CSRF-Token": csrf_token(client)},
        )

    response = client.post(
        "/api/v1/login", json=_login_json(), headers={"X-CSRF-Token": csrf_token(client)}
    )

    assert response.status_code == 429
    assert "rv_session" not in response.cookies


def test_tampered_cookie_is_unauthenticated(app_and_client):
    """Flips a character in the middle of the token, not the last one: the
    signature is base64url of a 32-byte HMAC, whose final encoded
    character carries two bits base64 doesn't validate, so some values of
    that last character decode to the same bytes and wouldn't actually
    tamper anything. A real, if narrow, base64 property, not a signature
    weakness."""
    _app, client = app_and_client
    login(client)
    good_cookie = client.cookies["rv_session"]
    mid = len(good_cookie) // 2
    tampered = (
        good_cookie[:mid] + ("A" if good_cookie[mid] != "A" else "B") + good_cookie[mid + 1 :]
    )
    client.cookies.set("rv_session", tampered)

    assert client.get("/api/v1/runs").status_code == 401
    assert client.get("/api/v1/session").json()["authenticated"] is False


def test_expired_cookie_is_unauthenticated(env, web_settings, web_device, web_password_hash):
    client = TestClient(make_web_app(env, web_settings, web_device))
    key = load_or_create_session_key(web_settings.web.session_key_path)
    expired_token = security.sign_session(key, jti=security.new_jti(), max_age_secs=10, now=1000)
    client.cookies.set("rv_session", expired_token)

    assert client.get("/api/v1/runs").status_code == 401


def test_logout_clears_cookie_and_next_request_is_unauthorized(app_and_client):
    _app, client = app_and_client
    login(client)
    assert client.get("/api/v1/runs").status_code == 200

    logout = client.post("/api/v1/logout")

    assert logout.status_code == 200
    assert logout.json()["authenticated"] is False
    assert 'rv_session=""' in logout.headers["set-cookie"]
    assert client.get("/api/v1/runs").status_code == 401


def test_logout_revokes_a_copied_cookie(env, web_settings, web_device, web_password_hash):
    """Logout bumps a server-side epoch rather than only clearing this
    browser's cookie: a cookie copied off the machine beforehand, and any
    other browser's session, must stop working too."""
    app = make_web_app(env, web_settings, web_device)
    browser = login(TestClient(app))
    other_browser = login(TestClient(app))
    stolen = TestClient(app)
    stolen.cookies.set("rv_session", browser.cookies["rv_session"])
    assert stolen.get("/api/v1/runs").status_code == 200

    assert browser.post("/api/v1/logout").status_code == 200

    assert stolen.get("/api/v1/runs").status_code == 401
    assert other_browser.get("/api/v1/runs").status_code == 401
    # A fresh login after the logout gets the new epoch and works again.
    assert login(TestClient(app)).get("/api/v1/runs").status_code == 200


def test_no_password_configured_returns_503_but_healthz_still_ok(env, web_settings, web_device):
    client = TestClient(make_web_app(env, web_settings, web_device))  # no web_password_hash

    for path in ("/api/v1/session", "/api/v1/runs"):
        response = client.get(path)
        assert response.status_code == 503, path
        assert "set-password" in response.json()["detail"], path
    assert client.get("/healthz").status_code == 200


def test_oversized_body_is_rejected_with_413(app_and_client):
    """Checked before auth, so an unauthenticated client can't make the
    server buffer a large body either."""
    _app, client = app_and_client
    body = b"x" * (64 * 1024 + 1)

    response = client.post(
        "/api/v1/login", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "payload too large"}


def test_body_at_the_limit_is_not_rejected_as_too_large(app_and_client):
    _app, client = app_and_client
    body = b" " * (64 * 1024)

    response = client.post(
        "/api/v1/login",
        content=body,
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_token(client)},
    )

    assert response.status_code != 413


# -- CSRF / Origin -----------------------------------------------------------


@pytest.fixture
def logged_in_client(app_and_client):
    """Logged in, but without `login`'s default CSRF header, so each test
    controls exactly what token (if any) a request carries."""
    _app, client = app_and_client
    login(client)
    del client.headers["X-CSRF-Token"]
    return client


def test_mutating_route_enumeration_is_not_empty(app_and_client):
    app, _client = app_and_client
    routes = _mutating_api_routes(app)
    assert ("POST", "/api/v1/recordings/1/retry") in routes
    assert ("PUT", "/api/v1/schedule") in routes
    assert len(routes) > 10


def test_every_mutating_route_without_csrf_header_is_forbidden(app_and_client, logged_in_client):
    app, _ = app_and_client
    for method, path in _mutating_api_routes(app):
        response = logged_in_client.request(method, path)
        assert response.status_code == 403, f"{method} {path}"
        assert response.json() == {"detail": "missing or invalid CSRF token"}, f"{method} {path}"


def test_every_mutating_route_with_bogus_csrf_is_forbidden(app_and_client, logged_in_client):
    app, _ = app_and_client
    for method, path in _mutating_api_routes(app):
        response = logged_in_client.request(
            method, path, headers={"X-CSRF-Token": "totally-bogus-token"}
        )
        assert response.status_code == 403, f"{method} {path}"


def test_every_mutating_route_rejects_the_anonymous_csrf_token(app_and_client, logged_in_client):
    """The token `/session` hands out before login is bound to the
    anonymous jti, good only for the login POST; it must not authorize a
    logged-in session's mutations."""
    app, _ = app_and_client
    key = load_or_create_session_key(app.state.rv.settings.web.session_key_path)
    anon = security.sign_csrf(key, jti=security.ANON_JTI)
    for method, path in _mutating_api_routes(app):
        if path == "/api/v1/login":
            continue
        response = logged_in_client.request(method, path, headers={"X-CSRF-Token": anon})
        assert response.status_code == 403, f"{method} {path}"


def test_csrf_token_from_another_session_is_forbidden(
    env, web_settings, web_device, web_password_hash
):
    app = make_web_app(env, web_settings, web_device)
    mine = login(TestClient(app))
    theirs = login(TestClient(app))

    response = mine.post(
        "/api/v1/recordings/999/retry",
        headers={"X-CSRF-Token": theirs.headers["X-CSRF-Token"]},
    )

    assert response.status_code == 403


def test_every_mutating_route_rejects_a_cross_site_origin(app_and_client, logged_in_client):
    """A valid token plus a foreign Origin still fails: Origin is checked
    independently, so a leaked token alone isn't enough from another site."""
    app, _ = app_and_client
    token = csrf_token(logged_in_client)
    for method, path in _mutating_api_routes(app):
        response = logged_in_client.request(
            method,
            path,
            headers={"X-CSRF-Token": token, "Origin": "https://evil.example.com"},
        )
        assert response.status_code == 403, f"{method} {path}"
        assert response.json() == {"detail": "origin mismatch"}, f"{method} {path}"


def test_same_origin_header_is_accepted(logged_in_client):
    response = logged_in_client.post(
        "/api/v1/recordings/999/retry",
        headers={"X-CSRF-Token": csrf_token(logged_in_client), "Origin": "http://testserver"},
    )
    assert response.status_code == 404  # past CSRF/Origin, into the handler


def test_login_without_csrf_header_is_forbidden(app_and_client):
    _app, client = app_and_client
    response = client.post("/api/v1/login", json=_login_json())
    assert response.status_code == 403
    assert "rv_session" not in response.cookies


def test_session_csrf_token_lasts_the_sessions_lifetime(app_and_client, monkeypatch):
    """The dashboard fetches its token once from `/session` and reuses it
    for as long as the tab is open, so the authenticated token must live as
    long as the session (30 days), not the anonymous token's 10 minutes."""
    _app, client = app_and_client
    login(client)
    real_now = time.time()
    later = SimpleNamespace(time=lambda: real_now + 29 * 86400)
    monkeypatch.setattr(security, "time", later)

    response = client.post("/api/v1/recordings/999/retry")

    assert response.status_code == 404  # CSRF and session both still valid


def test_anonymous_csrf_token_is_short_lived(app_and_client, monkeypatch):
    _app, client = app_and_client
    token = csrf_token(client)
    real_now = time.time()
    monkeypatch.setattr(security, "time", SimpleNamespace(time=lambda: real_now + 11 * 60))

    response = client.post("/api/v1/login", json=_login_json(), headers={"X-CSRF-Token": token})

    assert response.status_code == 403


def test_mutating_route_with_correct_csrf_succeeds_and_has_a_real_effect(
    env, web_settings, web_device, web_password_hash
):
    from reovault.models import ErrorClass, RemoteRecording

    rec = RemoteRecording(
        remote_name="a",
        start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=10,
        raw_metadata={},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.mark_failed(
        rid, error="boom", error_class=ErrorClass.NETWORK, next_attempt_at=None
    )
    client = login(TestClient(make_web_app(env, web_settings, web_device)))

    response = client.post(f"/api/v1/recordings/{rid}/retry")

    assert response.status_code == 200
    row = env.repository.get(rid)
    assert row.state == "failed"
    assert row.attempts == 0  # the actual side effect, not just a 200


def test_chunked_body_is_refused_before_it_is_read(client):
    """A chunked body has no Content-Length for the 64KB cap to check, so
    it's refused outright (fetch never sends one for JSON)."""

    def body():
        yield b"{" + b" " * 100_000 + b"}"

    response = client.post(
        "/api/v1/login",
        content=body(),
        headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
    )
    assert response.status_code == 411
