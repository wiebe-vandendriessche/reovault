"""Auth enforcement: every non-public route requires a session, CSRF is
enforced on every mutating route, and login/rate-limiting behave.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import (
    WEB_TEST_PASSWORD,
    _extract_hidden_csrf,
    extract_body_csrf,
    make_web_app,
)

_PUBLIC_ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/manifest.webmanifest"),
    ("GET", "/sw.js"),
    ("GET", "/offline"),
}

_PATH_PARAM_FILLS = {"recording_id": "1", "run_id": "1"}


def _all_routes(app):
    routes = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue  # skip the /static mount, which has no `.methods`
        filled = path
        for name, value in _PATH_PARAM_FILLS.items():
            filled = filled.replace(f"{{{name}}}", value)
        for method in methods:
            if method == "HEAD":
                continue  # mirrors GET's auth requirement; not worth a second case
            routes.append((method, filled))
    return routes


@pytest.fixture
def app_and_client(env, web_settings, web_device, web_password_hash):
    app = make_web_app(env, web_settings, web_device)
    return app, TestClient(app, follow_redirects=False)


# -- every route requires auth ----------------------------------------------


def test_every_non_public_route_requires_auth(app_and_client):
    app, client = app_and_client
    routes = _all_routes(app)
    assert len(routes) > 15, "sanity: route enumeration should find the real route table"

    for method, path in routes:
        if (method, path) in _PUBLIC_ROUTES:
            continue
        response = client.request(method, path)
        detail = f"{method} {path} returned {response.status_code} unauthenticated"
        assert response.status_code in (303, 401), detail
        if response.status_code == 303:
            assert response.headers["location"].startswith("/login")


def test_hx_request_gets_401_with_hx_redirect_not_a_login_page_body(app_and_client):
    _app, client = app_and_client
    response = client.get("/", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"
    assert "<form" not in response.text  # never a login page swapped into a fragment target


def test_static_and_healthz_are_reachable_without_auth(app_and_client):
    _app, client = app_and_client
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/app.css").status_code == 200


# -- login / logout / session ----------------------------------------------


def test_login_sets_secure_cookie_attributes(env, web_settings, web_device, web_password_hash):
    web_settings.web.cookie_secure = True  # exercise the real attribute this once
    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app, follow_redirects=False)
    csrf = _extract_hidden_csrf(client.get("/login").text)

    response = client.post(
        "/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"}
    )

    assert response.status_code == 303
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert f"Max-Age={30 * 86400}" in set_cookie


def test_login_omits_secure_cookie_attribute_when_disabled(app_and_client):
    """cookie_secure=False is what a direct-port, plain-HTTP deployment
    needs: a browser silently drops a Secure cookie sent over HTTP, which
    would otherwise bounce every login straight back to /login with no
    error. `web_settings` already defaults to False (tests run over plain
    http), so `app_and_client` exercises exactly that configuration."""
    _app, client = app_and_client
    csrf = _extract_hidden_csrf(client.get("/login").text)

    response = client.post(
        "/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"}
    )

    assert response.status_code == 303
    set_cookie = response.headers["set-cookie"]
    assert "Secure" not in set_cookie


def test_wrong_password_no_cookie_and_error_shown(app_and_client):
    _app, client = app_and_client
    csrf = _extract_hidden_csrf(client.get("/login").text)

    response = client.post("/login", data={"password": "wrong", "csrf_token": csrf, "next": "/"})

    assert response.status_code == 200
    assert "rv_session" not in response.cookies
    assert "Wrong email or password" in response.text


def test_six_failed_attempts_are_rate_limited(app_and_client):
    _app, client = app_and_client
    for _ in range(6):
        csrf = _extract_hidden_csrf(client.get("/login").text)
        response = client.post(
            "/login", data={"password": "wrong", "csrf_token": csrf, "next": "/"}
        )
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_tampered_cookie_is_unauthenticated(app_and_client):
    _app, client = app_and_client
    csrf = _extract_hidden_csrf(client.get("/login").text)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})
    good_cookie = client.cookies["rv_session"]
    client.cookies.set("rv_session", good_cookie[:-1] + ("A" if good_cookie[-1] != "A" else "B"))

    response = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_expired_cookie_is_unauthenticated(env, web_settings, web_device, web_password_hash):
    from reovault.web import security
    from reovault.web.auth import load_or_create_session_key

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app, follow_redirects=False)
    key = load_or_create_session_key(web_settings.web.session_key_path)
    expired_token = security.sign_session(key, jti=security.new_jti(), max_age_secs=10, now=1000)
    client.cookies.set("rv_session", expired_token)

    response = client.get("/", headers={"HX-Request": "true"})

    assert response.status_code == 401


def test_logout_clears_cookie_and_next_request_redirects(app_and_client):
    _app, client = app_and_client
    csrf = _extract_hidden_csrf(client.get("/login").text)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})
    home = client.get("/")
    body_csrf = extract_body_csrf(home.text)

    logout = client.post("/logout", headers={"X-CSRF-Token": body_csrf})
    assert logout.status_code == 200
    assert logout.headers["hx-redirect"] == "/login"

    response = client.get("/")
    assert response.status_code == 303


def test_no_password_configured_returns_503_but_healthz_still_ok(env, web_settings, web_device):
    app = make_web_app(env, web_settings, web_device)  # no web_password_hash fixture
    client = TestClient(app)

    assert client.get("/").status_code == 503
    assert client.get("/healthz").status_code == 200


# -- CSRF ---------------------------------------------------------------


@pytest.fixture
def logged_in_client(app_and_client):
    _app, client = app_and_client
    csrf = _extract_hidden_csrf(client.get("/login").text)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})
    return client


_MUTATING_ROUTES = [
    ("POST", "/actions/reconcile"),
    ("POST", "/recordings/1/retry"),
    ("POST", "/recordings/1/verify"),
]


@pytest.mark.parametrize(("method", "path"), _MUTATING_ROUTES)
def test_mutating_route_without_csrf_header_is_forbidden(logged_in_client, method, path):
    response = logged_in_client.request(method, path)
    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path"), _MUTATING_ROUTES)
def test_mutating_route_with_wrong_session_csrf_is_forbidden(logged_in_client, method, path):
    response = logged_in_client.request(
        method, path, headers={"X-CSRF-Token": "totally-bogus-token"}
    )
    assert response.status_code == 403


def test_mutating_route_with_correct_csrf_succeeds_and_has_a_real_effect(
    env, web_settings, web_device, web_password_hash
):
    from reovault.models import RemoteRecording

    device_id = env.device_id
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
    rid, _ = env.repository.discover_recording(device_id=device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    from reovault.models import ErrorClass

    env.repository.mark_failed(
        rid, error="boom", error_class=ErrorClass.NETWORK, next_attempt_at=None
    )

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    csrf = _extract_hidden_csrf(client.get("/login").text)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})
    body_csrf = extract_body_csrf(client.get("/").text)

    response = client.post(f"/recordings/{rid}/retry", headers={"X-CSRF-Token": body_csrf})

    assert response.status_code == 200
    row = env.repository.get(rid)
    assert row.state == "failed"
    assert row.attempts == 0  # the actual side effect, not just a 200


def test_login_form_without_hidden_csrf_field_is_forbidden(app_and_client):
    _app, client = app_and_client
    response = client.post("/login", data={"password": WEB_TEST_PASSWORD, "next": "/"})
    assert response.status_code == 403


def test_cross_site_origin_is_forbidden(logged_in_client):
    response = logged_in_client.post(
        "/actions/reconcile",
        headers={"X-CSRF-Token": "irrelevant", "Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
