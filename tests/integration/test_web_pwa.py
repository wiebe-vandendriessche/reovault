"""PWA shell: manifest, service worker, cache headers, and first-run empty
states.
"""

import json

from fastapi.testclient import TestClient

from tests.integration.conftest import WEB_TEST_PASSWORD, make_web_app


def test_manifest_is_valid_json_with_required_keys(
    env, web_settings, web_device, web_password_hash
):
    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)

    r = client.get("/manifest.webmanifest")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    body = json.loads(r.text)
    assert body["name"] == "ReoVault"
    assert body["display"] == "standalone"
    assert "orientation" not in body  # a fixed orientation would fight landscape video
    assert len(body["icons"]) >= 3
    assert any(icon["purpose"] == "maskable" for icon in body["icons"])


def test_manifest_is_reachable_without_auth(env, web_settings, web_device, web_password_hash):
    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    assert client.get("/manifest.webmanifest").status_code == 200


def test_sw_js_renders_with_current_asset_version(env, web_settings, web_device, web_password_hash):
    from reovault.web.app import _STATIC_DIR
    from reovault.web.assets import compute_asset_version

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)

    r = client.get("/sw.js")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["service-worker-allowed"] == "/"
    expected_version = compute_asset_version(_STATIC_DIR)
    assert f'VERSION = "{expected_version}"' in r.text


def test_sw_js_navigate_branch_never_calls_cache_put(
    env, web_settings, web_device, web_password_hash
):
    """A five-line static check: the navigate branch (authenticated HTML)
    must have no code path that could cache a page."""
    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    sw_text = client.get("/sw.js").text

    navigate_branch_start = sw_text.index('mode === "navigate"')
    navigate_branch = sw_text[navigate_branch_start : navigate_branch_start + 300]
    assert "cache.put" not in navigate_branch


def test_offline_page_reachable_without_auth(env, web_settings, web_device, web_password_hash):
    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    r = client.get("/offline")
    assert r.status_code == 200
    assert "ReoVault" in r.text


def test_authenticated_pages_are_never_under_static_or_the_sw_shell_list(
    env, web_settings, web_device, web_password_hash
):
    """Every authenticated page/fragment must be invisible to the service
    worker's whitelist (see test_sw_js_navigate_branch_never_calls_cache_put
    above): none of them live under /static/, and none appear in the
    hand-maintained SHELL_URLS list."""
    import re

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})

    sw_text = client.get("/sw.js").text
    for path in ("/", "/fragments/health", "/recordings", "/runs", "/problems"):
        assert not path.startswith("/static/")
        assert f'"{path}"' not in sw_text


# -- first-run / empty states -----------------------------------------------


def test_overview_renders_with_zero_recordings(env, web_settings, web_device, web_password_hash):
    import re

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})

    r = client.get("/")
    assert r.status_code == 200
    r2 = client.get("/fragments/health")
    assert r2.status_code == 200
    assert "No run yet." in r2.text


def test_day_view_renders_with_no_recordings_on_that_day(
    env, web_settings, web_device, web_password_hash
):
    import re

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})

    r = client.get("/fragments/day", params={"date_": "2026-09-16"})
    assert r.status_code == 200
    assert "No recordings on this day" in r.text


def test_problems_page_renders_with_zero_problems(env, web_settings, web_device, web_password_hash):
    import re

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})

    r = client.get("/problems")
    assert r.status_code == 200
    assert "No problems" in r.text


def test_runs_page_renders_with_zero_runs(env, web_settings, web_device, web_password_hash):
    import re

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})

    r = client.get("/runs")
    assert r.status_code == 200
    assert "No runs yet" in r.text
