"""Serving the built Svelte dashboard: the SPA catch-all, its path
containment, cache headers, and the CSP derived from the served
`index.html`. The PWA manifest and service worker are part of the build
now, so they're plain files here, not routes.
"""

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import login, make_web_app

_BOOT_SCRIPT = b"\n  __sveltekit_boot({ base: '' });\n"
_INDEX = (
    b"<!doctype html><html><head>"
    b'<script type="module" src="/_app/immutable/entry/start.js"></script>'
    b"</head><body><div id=app></div><script>" + _BOOT_SCRIPT + b"</script></body></html>"
)
_SECRET = b"outside-the-build"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """A minimal build, with a secret file next to (not inside) it that no
    request may ever reach."""
    root = tmp_path / "web" / "dist"
    (root / "_app" / "immutable" / "entry").mkdir(parents=True)
    (root / "index.html").write_bytes(_INDEX)
    (root / "_app" / "immutable" / "entry" / "start.js").write_text("export const x = 1;")
    (root / "manifest.webmanifest").write_text('{"name": "ReoVault"}')
    (tmp_path / "web" / "secret.txt").write_bytes(_SECRET)
    return root


@pytest.fixture
def spa(env, web_settings, web_device, web_password_hash, dist) -> TestClient:
    return TestClient(make_web_app(env, web_settings, web_device, dashboard_dir=dist))


@pytest.mark.parametrize("path", ["/", "/runs/12", "/footage/2026-09-16", "/does/not/exist"])
def test_client_routes_fall_back_to_index_html(spa, path):
    """A reload or bookmark on a client-side route must get the app shell,
    unauthenticated: the shell itself holds no data."""
    r = spa.get(path)

    assert r.status_code == 200
    assert r.content == _INDEX
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"


def test_real_file_is_served(spa):
    r = spa.get("/manifest.webmanifest")

    assert r.status_code == 200
    assert r.text == '{"name": "ReoVault"}'
    assert r.headers["cache-control"] == "no-cache"


def test_immutable_assets_get_a_long_lived_cache_header(spa):
    r = spa.get("/_app/immutable/entry/start.js")

    assert r.status_code == 200
    assert r.text == "export const x = 1;"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "path",
    [
        "/..%2fsecret.txt",
        "/%2e%2e%2fsecret.txt",
        "/%2e%2e/secret.txt",
        "/_app/..%2f..%2f..%2fsecret.txt",
        "/..%2f..%2f..%2f..%2f..%2f..%2fetc%2fpasswd",
        "/..%2f..%2fetc/passwd",
        "/%2fetc%2fpasswd",
        "/..%5csecret.txt",
    ],
)
def test_path_traversal_never_escapes_the_build(spa, path):
    """Encoded `..` and absolute paths decode into the route's `path`
    parameter; whatever they resolve to outside the build, the response is
    the app shell, never the file."""
    r = spa.get(path)

    assert _SECRET not in r.content
    assert b"root:" not in r.content
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.content == _INDEX


def test_symlink_out_of_the_build_is_not_followed(spa, dist):
    (dist / "leak.txt").symlink_to(dist.parent / "secret.txt")

    r = spa.get("/leak.txt")

    assert _SECRET not in r.content
    assert r.content == _INDEX


def test_csp_allows_the_inline_boot_script_by_hash_not_unsafe_inline(spa):
    expected = base64.b64encode(hashlib.sha256(_BOOT_SCRIPT).digest()).decode()

    for path in ("/", "/_app/immutable/entry/start.js", "/api/v1/session", "/healthz"):
        csp = spa.get(path).headers["content-security-policy"]
        assert f"'sha256-{expected}'" in csp, path
        assert "unsafe-inline" not in csp, path
        assert "frame-ancestors 'none'" in csp, path


def test_csp_hashes_only_inline_scripts(spa):
    """The `src=` module script is covered by 'self'; only the one inline
    script gets a hash."""
    csp = spa.get("/").headers["content-security-policy"]
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert script_src.count("'sha256-") == 1


def test_missing_build_returns_503_text(env, web_settings, web_device, web_password_hash, tmp_path):
    empty = tmp_path / "no-build"
    empty.mkdir()
    client = TestClient(make_web_app(env, web_settings, web_device, dashboard_dir=empty))

    r = client.get("/runs")

    assert r.status_code == 503
    assert r.headers["content-type"].startswith("text/plain")
    assert "npm run build" in r.text
    # The API and healthz don't depend on the build.
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/session").status_code == 200


def test_unknown_api_path_is_json_404_not_the_app_shell(spa):
    """A typo'd API call must fail loudly as JSON, not return 200 HTML the
    dashboard would then fail to parse."""
    login(spa)

    for method in ("GET", "POST", "PUT", "DELETE"):
        r = spa.request(method, "/api/v1/unknown")
        assert r.status_code == 404, method
        assert r.json() == {"detail": "not found"}, method
        assert r.content != _INDEX


def test_unknown_api_path_unauthenticated_is_401_not_the_app_shell(spa):
    r = spa.get("/api/v1/unknown")

    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}
