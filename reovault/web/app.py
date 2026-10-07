"""ReoVault web server: the `/api/v1` JSON API, `/healthz`, and the built
Svelte dashboard (a static SPA, see `dashboard/`).

One auth+CSRF+security-headers middleware, fail-closed: an API route added
later is protected by default, which a per-route `Depends` cannot promise.
Routers live in `reovault/web/api/`, shared state in `deps.py`.

Every route handler is a sync `def`: Starlette dispatches sync routes to its
threadpool, so blocking SQLite and blocking AES-GCM decryption never touch
the event loop.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
)
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from reovault import health
from reovault.config import Settings
from reovault.db.repository import Repository
from reovault.fleet import Fleet
from reovault.logging import get_logger
from reovault.notify import Notifier
from reovault.providers.base import CameraRegistry
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.scheduler import add_alerts_job
from reovault.web import security
from reovault.web.activity import ActivityLog
from reovault.web.api import actions, alerts, auth, devices, events, export, footage, runs
from reovault.web.api import health as health_api
from reovault.web.api import settings as settings_api
from reovault.web.api.auth import SESSION_COOKIE
from reovault.web.auth import load_or_create_session_key, load_password_hash
from reovault.web.deps import AppState
from reovault.web.events import EventBus
from reovault.web.ratelimit import LoginLimiter

logger = get_logger(__name__)

API_PREFIX = "/api/v1"
# Built by `npm run build` in dashboard/, copied here by the Dockerfile.
DEFAULT_DASHBOARD_DIR = Path(__file__).parent / "dist"

_PUBLIC_API = frozenset({f"{API_PREFIX}/session", f"{API_PREFIX}/login"})
_MAX_BODY_BYTES = 64 * 1024
_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _csp(script_hashes: list[str]) -> str:
    scripts = " ".join(["'self'", *(f"'sha256-{h}'" for h in script_hashes)])
    return (
        f"default-src 'self'; script-src {scripts}; style-src 'self'; "
        "img-src 'self' data:; media-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    )


def _inline_script_hashes(index_html: bytes) -> list[str]:
    """SvelteKit's SPA fallback page boots from one inline `<script>`.
    Hashing it here, from the file actually being served, keeps
    `script-src` free of 'unsafe-inline' without depending on SvelteKit's
    own CSP mode."""
    return [
        base64.b64encode(hashlib.sha256(m.group(1)).digest()).decode()
        for m in _INLINE_SCRIPT.finditer(index_html)
    ]


def _origin_matches_host(origin: str, request: Request) -> bool:
    return urlparse(origin).netloc == request.headers.get("host", "")


def create_app(
    fleet: Fleet,
    *,
    settings: Settings,
    repository: Repository,
    scheduler: BackgroundScheduler | None = None,
    registry: CameraRegistry | None = None,
    dashboard_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="ReoVault", docs_url=None, redoc_url=None, openapi_url=None)
    web = settings.web

    activity_log = ActivityLog()
    bus = EventBus()
    if scheduler is not None:
        mask = EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
        scheduler.add_listener(activity_log.apscheduler_listener, mask)
        scheduler.add_listener(bus.apscheduler_listener, mask)

    st = AppState(
        fleet=fleet,
        settings=settings,
        repo=repository,
        scheduler=scheduler,
        registry=registry,
        activity_log=activity_log,
        session_key=load_or_create_session_key(web.session_key_path),
        limiter=LoginLimiter(
            max_attempts=web.login_max_attempts,
            window_secs=web.login_window_secs,
            lockout_secs=web.login_lockout_secs,
        ),
        bus=bus,
        notifier=Notifier(
            settings=settings, repository=repository, fleet=fleet, publish=bus.publish
        ),
    )
    if scheduler is not None:
        add_alerts_job(scheduler, st.notifier.evaluate)
    app.state.rv = st

    dist = (dashboard_dir or DEFAULT_DASHBOARD_DIR).resolve()
    index_path = dist / "index.html"
    index_html = index_path.read_bytes() if index_path.is_file() else None
    csp = _csp(_inline_script_hashes(index_html) if index_html else [])

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        """An unexpected exception never reaches the client as text or a
        stack trace, which could carry a filesystem path."""
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
            # A chunked body carries no Content-Length to check, and fetch()
            # never sends one for a JSON body, so refuse it outright instead
            # of counting bytes as they arrive.
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
                token = request.headers.get("X-CSRF-Token", "")
                if not security.verify_csrf(st.session_key, token, jti=jti):
                    return JSONResponse(
                        {"detail": "missing or invalid CSRF token"}, status_code=403
                    )

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/healthz")
    def healthz(request: Request, verbose: bool = False) -> JSONResponse:
        """Public, for the Docker healthcheck, which only needs the status
        code. `?verbose=1` adds gateway state, per-device coverage alarms
        and database error text, so it needs a logged-in session: behind a
        reverse-proxy hostname that detail would otherwise be public."""
        if verbose:
            cookie = request.cookies.get(SESSION_COOKIE)
            payload = security.verify_session(st.session_key, cookie) if cookie else None
            verbose = payload is not None and payload.epoch == st.session_epoch()
        checks: dict[str, Any] = {}
        healthy = True
        try:
            repository.ping()
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001 - report, this endpoint must not raise
            checks["database"] = f"error: {exc}"
            healthy = False
        status = {"status": "ok" if healthy else "error"}
        code = 200 if healthy else 503
        if not verbose:
            return JSONResponse(status, status_code=code)

        # One gateway serves the whole fleet, so it's checked once.
        for did in fleet.ids():
            arch = fleet.get(did)
            if arch is not None and isinstance(arch.provider, ReolinkCliProvider):
                checks["gateway"] = "listening" if arch.provider.gateway.is_listening() else "down"
                break
        devices_status: dict[str, Any] = {}
        for did in fleet.ids():
            try:
                cov = health.coverage_margin(repository=repository, device_id=did)
                devices_status[str(did)] = {"coverage_alarm": cov.alarm}
            except Exception as exc:  # noqa: BLE001
                devices_status[str(did)] = {"coverage_alarm": f"error: {exc}"}
        checks["devices"] = devices_status
        return JSONResponse({**status, "checks": checks}, status_code=code)

    for module in (
        auth,
        health_api,
        footage,
        runs,
        actions,
        settings_api,
        devices,
        events,
        alerts,
        export,
    ):
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
        """Serves the built SPA: a real file if one exists under `dist`,
        otherwise `index.html` so client-side routes like `/runs/12` work on
        a reload or a bookmark."""
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
    """The schema `scripts/export_openapi.py` writes for the dashboard's
    generated types. Not served: `openapi_url=None` above."""
    from fastapi.openapi.utils import get_openapi

    return get_openapi(title="ReoVault", version="1", routes=app.routes)
