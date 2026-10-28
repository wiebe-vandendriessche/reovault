"""ReoVault web server: the `/api/v1` JSON API, `/healthz`, and the built
Svelte dashboard (a static SPA, see `dashboard/`).

One auth + CSRF + security-headers middleware, fail-closed: an API route
added later is protected by default. Route handlers are sync `def`s, so
blocking SQLite and subprocess calls run in Starlette's threadpool.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from reovault.logging import get_logger
from reovault.runtime import Runtime
from reovault.web import security
from reovault.web.api import alerts, auth, cameras, clips, events
from reovault.web.api import config as config_api
from reovault.web.api.auth import SESSION_COOKIE
from reovault.web.auth import load_or_create_session_key, load_password_hash
from reovault.web.deps import AppState
from reovault.web.ratelimit import LoginLimiter

logger = get_logger(__name__)

API_PREFIX = "/api/v1"
DEFAULT_DASHBOARD_DIR = Path(__file__).parent / "dist"  # built by the Dockerfile

_PUBLIC_API = frozenset({f"{API_PREFIX}/session", f"{API_PREFIX}/login"})
_MAX_BODY_BYTES = 64 * 1024
_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _csp(script_hashes: list[str]) -> str:
    scripts = " ".join(["'self'", *(f"'sha256-{h}'" for h in script_hashes)])
    return (
        f"default-src 'self'; script-src {scripts}; style-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    )


def _inline_script_hashes(index_html: bytes) -> list[str]:
    """SvelteKit's SPA page boots from one inline script; hashing it from the
    file actually served keeps 'unsafe-inline' out of script-src."""
    return [
        base64.b64encode(hashlib.sha256(m.group(1)).digest()).decode()
        for m in _INLINE_SCRIPT.finditer(index_html)
    ]


def _origin_matches_host(origin: str, request: Request) -> bool:
    return urlparse(origin).netloc == request.headers.get("host", "")


def create_app(rt: Runtime, *, dashboard_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="ReoVault", docs_url=None, redoc_url=None, openapi_url=None)
    web = rt.settings().web
    st = AppState(
        rt=rt,
        session_key=load_or_create_session_key(web.session_key_path),
        limiter=LoginLimiter(
            max_attempts=web.login_max_attempts,
            window_secs=web.login_window_secs,
            lockout_secs=web.login_lockout_secs,
        ),
    )
    app.state.rv = st

    def refresh_limiter(old: Any, new: Any) -> None:
        keys = ("login_max_attempts", "login_window_secs", "login_lockout_secs")
        if any(getattr(old.web, k) != getattr(new.web, k) for k in keys):
            st.limiter = LoginLimiter(
                max_attempts=new.web.login_max_attempts,
                window_secs=new.web.login_window_secs,
                lockout_secs=new.web.login_lockout_secs,
            )

    rt.config.subscribe(refresh_limiter)

    dist = (dashboard_dir or DEFAULT_DASHBOARD_DIR).resolve()
    index_path = dist / "index.html"
    index_html = index_path.read_bytes() if index_path.is_file() else None
    csp = _csp(_inline_script_hashes(index_html) if index_html else [])

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:
        """Never a stack trace or a filesystem path to the client."""
        logger.error("web.unhandled_exception", path=request.url.path, error=str(exc), exc_info=exc)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    @app.middleware("http")
    async def security_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        request.state.session_jti = None
        if path.startswith(f"{API_PREFIX}/"):
            if load_password_hash(web) is None:
                return JSONResponse(
                    {
                        "detail": "ReoVault has no web password configured yet. "
                        "Run `reovault web set-password` on the server."
                    },
                    status_code=503,
                )
            # fetch() never sends a chunked JSON body; refuse rather than count.
            if "chunked" in request.headers.get("transfer-encoding", "").lower():
                return JSONResponse({"detail": "length required"}, status_code=411)
            length = request.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > _MAX_BODY_BYTES):
                return JSONResponse({"detail": "payload too large"}, status_code=413)

            payload = None
            cookie = request.cookies.get(SESSION_COOKIE)
            if cookie:
                payload = security.verify_session(st.session_key, cookie)
                if payload is not None and payload.epoch != st.session_epoch():
                    payload = None  # revoked by a logout
            if payload is None and path not in _PUBLIC_API:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            request.state.session_jti = payload.jti if payload else None

            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                origin = request.headers.get("origin")
                if (
                    web.check_origin
                    and origin is not None
                    and not _origin_matches_host(origin, request)
                ):
                    return JSONResponse({"detail": "origin mismatch"}, status_code=403)
                jti = payload.jti if payload else security.ANON_JTI
                if not security.verify_csrf(
                    st.session_key, request.headers.get("X-CSRF-Token", ""), jti=jti
                ):
                    return JSONResponse(
                        {"detail": "missing or invalid CSRF token"}, status_code=403
                    )

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Public, for the Docker healthcheck: the status code is the answer."""
        try:
            rt.db.get_setting("session_epoch")
        except Exception:  # noqa: BLE001 - report, never raise
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok"})

    for module in (auth, cameras, clips, config_api, alerts, events):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.api_route(
        f"{API_PREFIX}/{{rest:path}}",
        methods=["GET", "POST", "PUT", "DELETE"],
        include_in_schema=False,
    )
    def api_not_found(rest: str) -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def dashboard(path: str) -> Response:
        """A real file under `dist` if one exists, else `index.html`, so
        client-side routes survive a reload."""
        if index_html is None:
            return PlainTextResponse(
                "The dashboard isn't built. Run `npm run build` in dashboard/.", status_code=503
            )
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist):
            immutable = candidate.is_relative_to(dist / "_app" / "immutable")
            cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return Response(index_html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    return app


def openapi_schema(app: FastAPI) -> dict[str, Any]:
    """For `scripts/export_openapi.py` (the dashboard's generated types)."""
    from fastapi.openapi.utils import get_openapi

    return get_openapi(title="ReoVault", version="1", routes=app.routes)
