"""ReoVault dashboard.

Route table, one auth+CSRF+security-headers middleware (fail-closed: a
route added later is protected by default, which a per-route `Depends`
cannot promise), and the app wiring. Auth/session/CSRF primitives live in
`security.py`, rate limiting in `ratelimit.py`, Range parsing in
`ranges.py`, the byte generator in `streaming.py`, local-day bucketing in
`reovault.timeline`, view-model helpers in `views.py`, the manual-run
activity ring in `activity.py`.

Every route handler is a sync `def`: Starlette dispatches sync routes to
its threadpool, so blocking SQLite (the Repository's own `RLock`) and
blocking AES-GCM decryption never touch the event loop. Only the security
middleware is `async`, and it does no I/O beyond an HMAC check and a small
file read.
"""

from __future__ import annotations

import contextlib
import hmac
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse
from zoneinfo import ZoneInfo

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
)
from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from jinja2.runtime import Context
from pydantic import BaseModel, Field, SecretStr
from pydantic import ValidationError as PydanticValidationError

from reovault import __version__, health
from reovault.archiver import Archiver
from reovault.config import ScheduleConfig, Settings
from reovault.db.repository import (
    DeviceRow,
    OnCardBytesRow,
    RecordingRow,
    Repository,
    RunRow,
    StorageSampleRow,
)
from reovault.fleet import Fleet
from reovault.logging import get_logger
from reovault.providers.base import CameraRegistry, ProviderError
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.scheduler import (
    add_device_jobs,
    device_of_job_id,
    is_schedule_env_pinned,
    load_effective_schedule,
    next_run_times,
    queue_manual_archive,
    queue_manual_backfill,
    queue_manual_reconcile,
    remove_device_jobs,
    reschedule_device,
    save_schedule,
)
from reovault.timeline import local_day_bounds
from reovault.web import security
from reovault.web.activity import ActivityLog
from reovault.web.assets import compute_asset_version
from reovault.web.auth import load_or_create_session_key, load_password_hash
from reovault.web.icons import icon_tag
from reovault.web.ranges import ByteRange, parse_range
from reovault.web.ratelimit import LoginLimiter
from reovault.web.streaming import StreamTruncatedError, iter_plaintext, safe_download_filename
from reovault.web.views import human_bytes, local_dt, redact_paths

logger = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

_PUBLIC_PATHS = frozenset(
    {"/healthz", "/login", "/manifest.webmanifest", "/sw.js", "/offline", "/favicon.ico"}
)
_MAX_LOGIN_FORM_BYTES = 8 * 1024
_MAX_BACKFILL_DAYS = 90

# The topbar/mobile-menu nav table. One list drives both the desktop inline
# links and the mobile <details> panel, so they can never drift out of sync.
# `desc` is the one-line explanation the mobile panel shows under each label.
_NAV_ITEMS = [
    {
        "id": "health",
        "href": "/",
        "icon": "activity",
        "label": "Health",
        "desc": "Status, coverage, and today's footage.",
    },
    {
        "id": "schedule",
        "href": "/schedule",
        "icon": "calendar-clock",
        "label": "Schedule",
        "desc": "When ReoVault checks for new clips, or run one now.",
    },
    {
        "id": "recordings",
        "href": "/recordings",
        "icon": "video",
        "label": "Footage",
        "desc": "Browse and play archived clips by date.",
    },
    {
        "id": "runs",
        "href": "/runs",
        "icon": "list-checks",
        "label": "Runs",
        "desc": "History of every archive run.",
    },
    {
        "id": "problems",
        "href": "/problems",
        "icon": "triangle-alert",
        "label": "Problems",
        "desc": "Recordings that failed and need attention.",
    },
]

# Devices is fleet-wide, not scoped to the selected camera, so it renders on
# the *other* side of the navbar from the five device-scoped tabs above and
# the device selector between them -- never in `_NAV_ITEMS`, never behind
# `durl()`.
_DEVICES_NAV_ITEM = {
    "id": "devices",
    "href": "/devices",
    "icon": "cctv",
    "label": "Devices",
    "desc": "Cameras ReoVault is watching, and adding a new one.",
}

# Remembers the last explicitly-picked device (see `_cookie_device_id`), so
# it survives a visit to a page that never carries `?device=` at all (a run
# or recording detail page, a bare bookmark). Not itself the source of
# truth for a page's own device scoping -- `?device=` in the URL always
# wins, this is only the fallback under it.
_DEVICE_COOKIE = "rv_device"


class AddDeviceRequest(BaseModel):
    """The Devices tab's add-camera form. Password is `SecretStr` so an
    accidental `repr()` anywhere, including a structlog kwarg, renders
    `**********` instead of the plaintext password. Channel and timezone are
    validated separately below rather than via a pydantic validator, so the
    error strings stay in the same plain-English style as every other form
    on this dashboard."""

    alias: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)
    channel: int | None = None
    timezone: str = Field(min_length=1)
    name: str | None = None


def create_app(
    fleet: Fleet,
    *,
    settings: Settings,
    repository: Repository,
    scheduler: BackgroundScheduler | None = None,
    registry: CameraRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="ReoVault", docs_url=None, redoc_url=None, openapi_url=None)

    web = settings.web
    repo = repository

    def _enabled_devices_for_picker() -> list[DeviceRow]:
        ids = fleet.ids()
        return [d for d in repo.list_devices() if d.id in ids]

    def _device_picker_links(request: Request, devices: list[DeviceRow]) -> dict[int, str]:
        """A relative `path?query` per device, carrying every OTHER query
        param the current request already had (so recovering from e.g.
        `/recordings?date_=...` doesn't also lose the date) with `device`
        added or replaced. Built server-side rather than in the template:
        `request.url` is absolute (`http://testserver/...`), and a picker
        link must stay relative to work the same behind any reverse proxy
        or hostname."""
        params = dict(parse_qsl(request.url.query))
        links = {}
        for d in devices:
            params["device"] = str(d.id)
            links[d.id] = f"{request.url.path}?{urlencode(params)}"
        return links

    def _device_ctx(request: Request) -> tuple[Archiver, str] | Response:
        """Resolves the device this request is scoped to: `?device=<id>` if
        present, else the last device remembered via `_cookie_device_id` (so
        a bare bookmark, or a nav click from a global-PK page like
        `/runs/{id}`, still stays on the camera that was last picked instead
        of falling into the "which camera?" picker), else the fleet's sole
        enabled device. Multiple devices with no `?device=` and no usable
        cookie is an error. Returns `(archiver, timezone)` on success, or a
        `Response` the caller must return as-is."""
        raw = request.query_params.get("device")
        if raw is not None:
            try:
                device_id = int(raw)
            except ValueError:
                return PlainTextResponse("bad device id", status_code=400)
            archiver = fleet.get(device_id)
            device_row = repo.get_device(device_id)
            if archiver is None or device_row is None:
                # A stale/bookmarked link to a device that no longer exists
                # or was disabled. Rendered in the shell (with nav), not a
                # bare 404, so there's still a way to pick a different
                # camera.
                devices = _enabled_devices_for_picker()
                return render(
                    request,
                    "no_device.html",
                    {
                        "reason": "unknown",
                        "devices": devices,
                        "device_links": _device_picker_links(request, devices),
                    },
                    status_code=404,
                )
            return archiver, device_row.timezone
        cookie_device_id = _cookie_device_id(request)
        if cookie_device_id is not None:
            archiver = fleet.get(cookie_device_id)
            device_row = repo.get_device(cookie_device_id)
            if archiver is not None and device_row is not None:
                return archiver, device_row.timezone
        ids = fleet.ids()
        if len(ids) == 1:
            device_id = ids[0]
        elif not ids:
            # Rendered in the shell, with a real link back to Devices, not a
            # bare response -- this is exactly how someone gets here (they
            # just disabled their only camera), so there needs to be a way
            # back to re-enable it.
            return render(request, "no_device.html", {"reason": "none"}, status_code=503)
        else:
            # Each picker link carries the *current* URL with `device` set,
            # not just the path: a bare path would drop e.g. `?date_=...`
            # on /recordings, silently losing what the visitor was looking
            # at the moment they hit the ambiguity.
            devices = _enabled_devices_for_picker()
            return render(
                request,
                "no_device.html",
                {
                    "reason": "multiple",
                    "devices": devices,
                    "device_links": _device_picker_links(request, devices),
                },
                status_code=400,
            )
        archiver = fleet.get(device_id)
        device_row = repo.get_device(device_id)
        if archiver is None or device_row is None:
            return PlainTextResponse("unknown device", status_code=404)
        return archiver, device_row.timezone

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["redact"] = lambda text: redact_paths(text, settings.storage)
    templates.env.filters["human_bytes"] = human_bytes
    templates.env.filters["local_dt"] = local_dt
    asset_version = compute_asset_version(_STATIC_DIR)
    templates.env.globals["icon"] = lambda name, label=None, cls="": icon_tag(
        name, asset_version, label=label, cls=cls
    )

    activity_log = ActivityLog()
    if scheduler is not None:
        scheduler.add_listener(
            activity_log.apscheduler_listener,
            EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
        )

    limiter = LoginLimiter(
        max_attempts=web.login_max_attempts,
        window_secs=web.login_window_secs,
        lockout_secs=web.login_lockout_secs,
    )
    session_key = load_or_create_session_key(web.session_key_path)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        """Catch-all so an unexpected exception from any route (a `repo.get`
        that raises, a template error, anything) never reaches the client
        as exception text or a stack trace, which could carry a filesystem
        path. Exception detail goes to structlog only."""
        logger.error("web.unhandled_exception", path=request.url.path, error=str(exc), exc_info=exc)
        return PlainTextResponse("internal error", status_code=500)

    def _cookie_device_id(request: Request) -> int | None:
        """The last device explicitly picked (nav link, dropdown, or picker
        page), remembered so a page reached with no `?device=` at all -- a
        global-PK route, or a plain bookmark -- still lands on the right
        camera instead of the "which camera?" picker. Only ever a fallback:
        an explicit `?device=` in the URL always wins (see `render()`, which
        also keeps the cookie itself up to date). Ignores a stale value (a
        disabled/removed device) rather than trusting it, since this never
        goes through `_device_ctx`'s own validation."""
        raw = request.cookies.get(_DEVICE_COOKIE)
        if raw is None:
            return None
        try:
            device_id = int(raw)
        except ValueError:
            return None
        return device_id if device_id in fleet.ids() else None

    def _current_device_id(request: Request) -> int | None:
        """Whichever device the current page is scoped to: an explicit
        `?device=`, else the last one remembered via `_cookie_device_id`,
        else the sole enabled device. Never raises and never returns a
        `Response` -- used to build every `durl()` link and the navbar's
        device selector, neither of which can afford `_device_ctx`'s full
        resolution (which returns an error `Response` on failure)."""
        raw = request.query_params.get("device")
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                return None
        cookie_id = _cookie_device_id(request)
        if cookie_id is not None:
            return cookie_id
        ids = fleet.ids()
        return ids[0] if len(ids) == 1 else None

    def _nav_device_rows(request: Request, current_device_id: int | None) -> list[dict[str, Any]]:
        """The navbar device selector's rows: enabled devices only, each
        with a problem count and a link that switches to that device on the
        *current* page. Deliberately not `_devices_context` (that also
        backfills identity and refreshes the SD sample, both of which can
        shell out to reolink-cli) and deliberately no live gateway check (a
        subprocess call per device, per page load, isn't worth it for a
        rarely-opened dropdown)."""
        devices = _enabled_devices_for_picker()
        links = _device_picker_links(request, devices)
        rows = []
        for d in devices:
            totals = repo.totals(d.id)
            rows.append(
                {
                    "id": d.id,
                    "label": d.name or "Unnamed camera",
                    "problems": totals.failed_count + totals.quarantined_count,
                    "current": d.id == current_device_id,
                    "url": links[d.id],
                }
            )
        return rows

    @pass_context
    def durl(ctx: Context, url: str) -> str:
        """Appends the selected device to a device-scoped URL. A Jinja
        global, not a filter: every call site then reads as a small URL
        builder (`durl('/fragments/health')`), and being context-aware means
        no template has to thread the id through itself. Every device-scoped
        `href`/`hx-get`/`hx-post`/`hx-push-url` in the app goes through
        this -- without it, a multi-camera fragment 400s, since nothing
        carries `?device=` and it falls into `_device_ctx`'s "which
        camera?" branch."""
        device_id = ctx.get("current_device_id")
        if not device_id:
            return url
        return f"{url}{'&' if '?' in url else '?'}device={device_id}"

    templates.env.globals["durl"] = durl

    def render(request: Request, name: str, context: dict[str, Any], **kw: Any) -> HTMLResponse:
        # `request.state.session_jti` is always set by the middleware (to
        # None when unauthenticated), so `getattr(..., default)` would never
        # see a missing attribute to fall back on; `or` is required here.
        jti = getattr(request.state, "session_jti", None) or security.ANON_JTI
        current_device_id = _current_device_id(request)
        ctx = {
            "asset_version": asset_version,
            "csrf_token": security.sign_csrf(session_key, jti=jti),
            "nav_items": _NAV_ITEMS,
            "devices_nav_item": _DEVICES_NAV_ITEM,
            "reovault_version": __version__,
            "pinned_cli_version": settings.reolink_cli.pinned_version,
            "current_device_id": current_device_id,
            # Fragments poll far too often (the activity card every 5s, the
            # problem badge every 60s) to also pay for a devices list +
            # totals() per device on every response; only a full page needs
            # the navbar's device selector at all.
            "nav_devices": None
            if name.startswith("fragments/")
            else _nav_device_rows(request, current_device_id),
            **context,
        }
        response = templates.TemplateResponse(request, name, ctx, **kw)
        # An explicit `?device=` in this request is a real pick (a dropdown
        # click, a nav link, a picker page), so it's what `_cookie_device_id`
        # should hand back on the next page that carries no `?device=` at
        # all. Only written when the id actually resolved to something real
        # (never a bad/unknown id, which `current_device_id` already
        # reflects by being None), and only on an explicit pick -- a page
        # reached *via* the cookie fallback must not keep re-writing the
        # same value on every single response.
        if request.query_params.get("device") is not None and current_device_id is not None:
            response.set_cookie(
                _DEVICE_COOKIE,
                str(current_device_id),
                max_age=60 * 60 * 24 * 365,
                secure=web.cookie_secure,
                samesite=web.cookie_samesite,  # type: ignore[arg-type]
                path="/",
            )
        return response

    # -- security middleware: password gate, auth, CSRF, headers ----------

    @app.middleware("http")
    async def security_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        is_static = path.startswith("/static/")

        if not is_static and path != "/healthz":
            password_hash = load_password_hash(web)
            if password_hash is None:
                return PlainTextResponse(
                    "ReoVault has no web password configured yet. "
                    "Run `reovault web set-password` on the server.",
                    status_code=503,
                )

        is_public = is_static or path in _PUBLIC_PATHS

        session_payload = None
        cookie = request.cookies.get("rv_session")
        if cookie:
            session_payload = security.verify_session(session_key, cookie)

        if not is_public and session_payload is None:
            if request.headers.get("HX-Request") == "true":
                return JSONResponse(
                    {"error": "unauthorized"}, status_code=401, headers={"HX-Redirect": "/login"}
                )
            return RedirectResponse(f"/login?next={path}", status_code=303)

        request.state.session_jti = session_payload.jti if session_payload else None

        if request.method in ("POST", "PUT", "PATCH", "DELETE") and path != "/login":
            origin = request.headers.get("origin")
            if (
                web.check_origin
                and origin is not None
                and not _origin_matches_host(origin, request)
            ):
                return PlainTextResponse("forbidden: origin mismatch", status_code=403)
            token = request.headers.get("X-CSRF-Token", "")
            jti = session_payload.jti if session_payload else security.ANON_JTI
            if not security.verify_csrf(session_key, token, jti=jti):
                return PlainTextResponse(
                    "forbidden: missing or invalid CSRF token", status_code=403
                )

        response = await call_next(request)
        if not is_static:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; media-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
        return response

    # -- public: healthz, login/logout, PWA shell --------------------------

    @app.get("/healthz")
    def healthz(verbose: bool = False) -> JSONResponse:
        """Unauthenticated body stays status-only: behind a reverse-proxy
        hostname, a verbose body would leak gateway state and the coverage
        alarm to any caller. The Docker healthcheck only needs the status
        code; ?verbose=1 (meant for an authenticated caller, but not itself
        auth-gated here since the healthcheck can't authenticate) restores
        the full detail."""
        checks: dict[str, Any] = {}
        healthy = True
        try:
            repo.ping()
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001 - report, this endpoint must not raise
            checks["database"] = f"error: {exc}"
            healthy = False

        if not verbose:
            return JSONResponse(
                {"status": "ok" if healthy else "error"}, status_code=200 if healthy else 503
            )

        # One gateway is shared by the whole fleet (see fleet.py), so its
        # listening state is checked once, not per device; coverage alarms
        # are genuinely per device, so those are broken out below.
        for did in fleet.ids():
            arch = fleet.get(did)
            if arch is not None and isinstance(arch.provider, ReolinkCliProvider):
                checks["gateway"] = "listening" if arch.provider.gateway.is_listening() else "down"
                break

        devices_status: dict[str, Any] = {}
        for did in fleet.ids():
            try:
                status = health.coverage_margin(repository=repo, device_id=did)
                devices_status[str(did)] = {"coverage_alarm": status.alarm}
            except Exception as exc:  # noqa: BLE001
                devices_status[str(did)] = {"coverage_alarm": f"error: {exc}"}
        checks["devices"] = devices_status

        return JSONResponse(
            {"status": "ok" if healthy else "error", "checks": checks},
            status_code=200 if healthy else 503,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/") -> Response:
        if request.state.session_jti is not None:
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", {"next": next, "error": None})

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        body = await request.body()
        if len(body) > _MAX_LOGIN_FORM_BYTES:
            return PlainTextResponse("payload too large", status_code=413)
        form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        next_path = form.get("next") or "/"
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"

        # /login is exempt from the middleware's header-based CSRF check
        # (there's no session yet); it verifies its own hidden field here.
        if not security.verify_csrf(session_key, form.get("csrf_token", ""), jti=security.ANON_JTI):
            return PlainTextResponse("forbidden: missing or invalid CSRF token", status_code=403)

        client_key = request.client.host if request.client else "unknown"
        retry_after = limiter.retry_after(client_key)
        if retry_after is not None:
            minutes = int(retry_after // 60) + 1
            return render(
                request,
                "login.html",
                {"next": next_path, "error": f"Too many attempts. Try again in {minutes} minutes."},
                status_code=429,
                headers={"Retry-After": str(int(retry_after))},
            )

        password_hash = load_password_hash(web)
        password = form.get("password", "").encode()
        # Both checks must always run, never short-circuit: a wrong email
        # has to cost the same ~155ms Argon2id takes as a wrong password,
        # or the email field
        # becomes a free timing oracle for enumeration. That's why
        # email_ok/password_ok are each their own unconditional statement
        # rather than one inlined into an `if`/`and` expression, which is
        # what would let Python skip evaluating verify_password whenever
        # email_ok is already False. `&` on the combining line below is a
        # deliberate reminder of that -- `and` would work identically today
        # given the eager statements above it, but invites exactly the
        # "inline it" simplification that would reintroduce the timing
        # leak, so don't "clean" it into `and`.
        submitted_email = form.get("email", "")
        expected_email = web.email or ""
        email_ok = hmac.compare_digest(
            submitted_email.strip().casefold().encode(), expected_email.strip().casefold().encode()
        )
        password_ok = (
            security.verify_password(password, password_hash)
            if password_hash is not None
            else False
        )
        ok = email_ok & password_ok
        if not ok:
            limiter.record_failure(client_key)
            return render(
                request, "login.html", {"next": next_path, "error": "Wrong email or password."}
            )

        limiter.record_success(client_key)
        jti = security.new_jti()
        token = security.sign_session(
            session_key, jti=jti, max_age_secs=web.session_max_age_days * 86400
        )
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            "rv_session",
            token,
            httponly=True,
            secure=web.cookie_secure,
            samesite=web.cookie_samesite,  # type: ignore[arg-type]
            max_age=web.session_max_age_days * 86400,
            path="/",
        )
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = Response(status_code=200, headers={"HX-Redirect": "/login"})
        response.delete_cookie("rv_session", path="/")
        return response

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse(_manifest_body(asset_version), media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker(request: Request) -> Response:
        body = render(request, "sw.js.jinja", {}).body
        return Response(
            body,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/offline", response_class=HTMLResponse)
    def offline_page(request: Request) -> Response:
        return render(request, "offline.html", {})

    # -- authenticated pages -------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        device_id = archiver.device_id
        today = _local_today(tz)
        day_start, day_end = local_day_bounds(today, tz)
        today_rows = repo.recordings_in_window(
            device_id=device_id, from_utc=day_start, to_utc=day_end, limit=100000
        )
        today_bytes = sum(r.plaintext_size or 0 for r in today_rows if r.state == "archived")
        return render(
            request,
            "overview.html",
            {
                "active_nav": "health",
                "today": today.isoformat(),
                "today_count": len(today_rows),
                "today_bytes": today_bytes,
            },
        )

    @app.get("/fragments/health", response_class=HTMLResponse)
    def fragment_health(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        return render(request, "fragments/health.html", _health_context(archiver))

    @app.get("/fragments/growth", response_class=HTMLResponse)
    def fragment_growth(request: Request, days: int = 30) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        return render(request, "fragments/growth.html", _growth_context(archiver, tz, days))

    @app.get("/fragments/activity", response_class=HTMLResponse)
    def fragment_activity(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, _tz = ctx
        return render(request, "fragments/activity.html", _activity_context(archiver))

    @app.get("/fragments/problems/badge", response_class=HTMLResponse)
    def fragment_problem_badge(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, _tz = ctx
        totals = repo.totals(archiver.device_id)
        count = totals.failed_count + totals.quarantined_count
        return render(request, "fragments/problem_badge.html", {"count": count})

    # -- footage: date-first browse --------------------------------------

    @app.get("/recordings", response_class=HTMLResponse)
    def recordings_page(request: Request, date_: str | None = None, types: str = "") -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        _archiver, tz = ctx
        day = _parse_date(date_) or _local_today(tz)
        return render(
            request,
            "recordings.html",
            {
                "active_nav": "recordings",
                "date": day.isoformat(),
                "types": types,
                "month": f"{day.year:04d}-{day.month:02d}",
            },
        )

    @app.get("/fragments/calendar", response_class=HTMLResponse)
    def fragment_calendar(request: Request, month: str) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        return render(
            request, "fragments/calendar.html", _calendar_context(archiver.device_id, tz, month)
        )

    @app.get("/fragments/day", response_class=HTMLResponse)
    def fragment_day(request: Request, date_: str, types: str = "") -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        day = _parse_date(date_)
        if day is None:
            return PlainTextResponse("bad date", status_code=400)
        return render(
            request, "fragments/day.html", _day_context(archiver.device_id, tz, day, types)
        )

    @app.get("/fragments/day/hour", response_class=HTMLResponse)
    def fragment_day_hour(
        request: Request, date_: str, hour: int, types: str = "", after: str = ""
    ) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        day = _parse_date(date_)
        if day is None:
            return PlainTextResponse("bad date", status_code=400)
        return render(
            request,
            "fragments/hour_rows.html",
            _hour_rows_context(archiver.device_id, tz, day, hour, types, after),
        )

    # The four routes below are keyed by `recording_id` alone, a global
    # unambiguous primary key, and deliberately do NOT go through
    # `_device_ctx`: a recording's own row already says which device it
    # belongs to, and using the *ambient* selected device's timezone here
    # instead would render an actually-correct-looking but wrong local time
    # whenever the row belongs to a different device than the one currently
    # selected.

    def _tz_for_row(row: RecordingRow) -> str:
        device_row = repo.get_device(row.device_id)
        return device_row.timezone if device_row is not None else "UTC"

    @app.get("/fragments/recordings/{recording_id}/row", response_class=HTMLResponse)
    def fragment_recording_row(request: Request, recording_id: int) -> Response:
        row = repo.get(recording_id)
        if row is None:
            return PlainTextResponse("not found", status_code=404)
        return render(request, "fragments/rec_row.html", {"rec": _rec_view(row, _tz_for_row(row))})

    @app.get("/fragments/recordings/{recording_id}/player", response_class=HTMLResponse)
    def fragment_recording_player(request: Request, recording_id: int) -> Response:
        row = repo.get(recording_id)
        if row is None:
            return PlainTextResponse("not found", status_code=404)
        return render(
            request, "fragments/rec_player.html", {"rec": _rec_view(row, _tz_for_row(row))}
        )

    @app.post("/recordings/{recording_id}/retry", response_class=HTMLResponse)
    def retry_recording(request: Request, recording_id: int) -> Response:
        """Two different callers share this route and need two different
        response shapes: the Footage day view's expanded player collapses
        back to a plain row, matching its own Close button, while the
        Problems page needs to keep looking like a problem row -- the
        recording is still `failed`, only now eligible for the next run's
        pickup, not actually re-downloaded on the spot (see
        Repository.retry_recording). Returning `rec_row.html` to the
        Problems page leaves a half-Footage, half-Problems row behind, plus
        an orphaned error `<details>` beside it, since the swap only
        replaces the `<article>`, not that trailing sibling."""
        repo.retry_recording(recording_id)
        row = repo.get(recording_id)
        headers = {"HX-Trigger": "problems-changed"}
        if row is None:
            return PlainTextResponse("not found", status_code=404)
        if request.query_params.get("context") == "problem":
            return render(
                request,
                "fragments/problem_row.html",
                {"rec": _rec_view(row, _tz_for_row(row)), "queued": True},
                headers=headers,
            )
        return render(
            request,
            "fragments/rec_row.html",
            {"rec": _rec_view(row, _tz_for_row(row))},
            headers=headers,
        )

    @app.post("/recordings/{recording_id}/verify", response_class=HTMLResponse)
    def verify_recording(request: Request, recording_id: int) -> Response:
        row = repo.get(recording_id)
        if row is None or row.vault_path is None:
            return PlainTextResponse("not found", status_code=404)
        # ponytail: inline synchronous verify. Measured ~100ms at the real
        # ~20MB/clip average, so a queue only earns its keep if clip sizes
        # grow a lot. Uses the shared fleet vault, not a specific device's
        # archiver, for
        # the same reason streaming does (see stream_recording): this must
        # still work for a recording that belongs to a disabled device.
        ok = fleet.vault.verify(row.vault_path)
        headers = {"HX-Trigger": "problems-changed"} if not ok else {}
        return render(
            request,
            "fragments/verify_result.html",
            {"rec": _rec_view(row, _tz_for_row(row)), "ok": ok},
            headers=headers,
        )

    # -- range streaming / download --------------------------------------
    # Deliberately NOT behind _device_ctx: a stream URL is generated from a
    # recording id alone (see rec_player.html), never carries `?device=`,
    # and the id is already a global, unambiguous primary key across every
    # device sharing this Repository. Scoping this by device would just add
    # a way for a stale/missing `?device=` to 404 a stream that a plain
    # `repo.get(recording_id)` answers correctly on its own.

    @app.api_route("/recordings/{recording_id}/stream", methods=["GET", "HEAD"])
    def stream_recording(request: Request, recording_id: int, download: int = 0) -> Response:
        row = repo.get(recording_id)
        if row is None or row.state != "archived" or row.vault_path is None:
            return PlainTextResponse("not found", status_code=404)
        size = row.plaintext_size
        if size is None:
            try:
                size = fleet.vault.describe(row.vault_path).plaintext_size
            except Exception:  # noqa: BLE001
                return PlainTextResponse("not found", status_code=404)

        parsed = parse_range(request.headers.get("range"), size)
        base_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "video/mp4",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            # nginx honors this per-response, so a reverse proxy never
            # spools a Range request through a temp file.
            "X-Accel-Buffering": "no",
        }
        filename = safe_download_filename(row.remote_name)
        disposition = "attachment" if download else "inline"
        base_headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'

        if parsed == "unsatisfiable":
            return Response(
                status_code=416,
                headers={**base_headers, "Content-Range": f"bytes */{size}"},
            )

        byte_range: ByteRange = parsed if isinstance(parsed, ByteRange) else ByteRange(0, size - 1)
        status_code = 206 if isinstance(parsed, ByteRange) else 200
        length = byte_range.end - byte_range.start + 1
        headers = {**base_headers, "Content-Length": str(length)}
        if status_code == 206:
            headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{size}"

        if request.method == "HEAD":
            return Response(status_code=status_code, headers=headers)

        vault_path = row.vault_path
        chunk_size = web.stream_chunk_bytes
        try:
            gen = iter_plaintext(
                fleet.vault, vault_path, byte_range.start, byte_range.end, chunk_size
            )
            first_chunk = next(gen, b"")
        except FileNotFoundError as exc:
            # The vault file is gone (e.g. reconciliation raced with this
            # request). Same public answer as an unknown id: 404, path
            # logged server-side only.
            logger.error("web.stream_vault_file_missing", recording_id=recording_id, error=str(exc))
            return PlainTextResponse("not found", status_code=404)
        except (StreamTruncatedError, OSError, ValueError) as exc:
            logger.error("web.stream_precheck_failed", recording_id=recording_id, error=str(exc))
            return PlainTextResponse("internal error", status_code=500)

        def body() -> Iterator[bytes]:
            if first_chunk:
                yield first_chunk
            try:
                yield from gen
            except StreamTruncatedError as exc:
                logger.error("web.stream_truncated", recording_id=recording_id, offset=exc.offset)
                raise

        return StreamingResponse(body(), status_code=status_code, headers=headers)

    # -- runs --------------------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        rows = repo.recent_runs(device_id=archiver.device_id, limit=web.page_size)
        times = next_run_times(scheduler).get(archiver.device_id, {}) if scheduler else {}
        scheduled_runs = [
            {"label": label, "next_run": times.get(base)}
            for base, label in _SCHEDULE_JOB_LABELS.items()
        ]
        return render(
            request,
            "runs.html",
            {
                "active_nav": "runs",
                "runs": [_run_view(r) for r in rows],
                "has_more": len(rows) == web.page_size,
                "tz": tz,
                "scheduled_runs": scheduled_runs,
            },
        )

    @app.get("/fragments/runs", response_class=HTMLResponse)
    def fragment_runs(request: Request, before_id: int | None = None) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        rows = repo.recent_runs(
            device_id=archiver.device_id, limit=web.page_size, before_id=before_id
        )
        return render(
            request,
            "fragments/run_rows.html",
            {
                "runs": [_run_view(r) for r in rows],
                "has_more": len(rows) == web.page_size,
                "tz": tz,
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int) -> Response:
        # No _device_ctx: run_id is a global PK (archive_runs), not scoped
        # to the ambient selected device. Its own device's timezone is
        # looked up directly from `row.device_id` instead, so the
        # started/finished timestamps below still render in the right
        # timezone rather than falling back to UTC.
        row = repo.get_run(run_id)
        if row is None:
            return PlainTextResponse("not found", status_code=404)
        device_row = repo.get_device(row.device_id) if row.device_id is not None else None
        tz = device_row.timezone if device_row is not None else "UTC"
        return render(
            request, "run_detail.html", {"active_nav": "runs", "run": _run_view(row), "tz": tz}
        )

    # -- problems ------------------------------------------------------

    @app.get("/problems", response_class=HTMLResponse)
    def problems_page(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        rows = repo.problems(device_id=archiver.device_id, limit=web.page_size)
        return render(
            request,
            "problems.html",
            {
                "active_nav": "problems",
                "rows": [_rec_view(r, tz) for r in rows],
                "has_more": len(rows) == web.page_size,
            },
        )

    @app.get("/fragments/problems", response_class=HTMLResponse)
    def fragment_problems(request: Request, after: str = "") -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, tz = ctx
        cursor = _parse_cursor(after)
        rows = repo.problems(device_id=archiver.device_id, limit=web.page_size, after=cursor)
        return render(
            request,
            "fragments/problem_rows.html",
            {
                "rows": [_rec_view(r, tz) for r in rows],
                "has_more": len(rows) == web.page_size,
            },
        )

    # -- controls: run now / backfill / reconcile --------------------------

    @app.post("/actions/run", response_class=HTMLResponse)
    def action_run(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, _tz = ctx
        if scheduler is None:
            return render(
                request,
                "fragments/activity.html",
                {**_activity_context(archiver), "scheduler_down": True},
            )
        if repo.running_run(archiver.device_id) is not None:
            return render(request, "fragments/activity.html", _activity_context(archiver))
        now = datetime.now(UTC)
        # Same window as `archive_overlap_hours`'s default: a manual run
        # doesn't try to be smarter than the scheduled one about it.
        from_utc = now - timedelta(hours=48)
        with contextlib.suppress(ConflictingIdError):
            queue_manual_archive(scheduler, archiver, from_utc=from_utc, to_utc=now)
        return render(request, "fragments/activity.html", _activity_context(archiver))

    @app.post("/actions/stop", response_class=HTMLResponse)
    def action_stop(request: Request) -> Response:
        """Cooperative, not immediate: sets a flag the Archiver only checks
        between recordings (see `Archiver.request_cancel`), so a fetch or
        encrypt already in flight always finishes or fails on its own
        rather than being torn down mid-write. The run then closes itself
        out as `aborted`, same as any other early exit."""
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, _tz = ctx
        if repo.running_run(archiver.device_id) is not None:
            archiver.request_cancel()
        return render(request, "fragments/activity.html", _activity_context(archiver))

    @app.post("/actions/backfill", response_class=HTMLResponse)
    async def action_backfill(request: Request) -> Response:
        dctx = _device_ctx(request)
        if isinstance(dctx, Response):
            return dctx
        archiver, tz = dctx
        body = await request.body()
        form = dict(parse_qsl(body.decode("utf-8", "replace")))
        from_date = _parse_date(form.get("from", ""))
        to_date = _parse_date(form.get("to", ""))
        ctx = _activity_context(archiver)
        if scheduler is None:
            return render(request, "fragments/activity.html", {**ctx, "scheduler_down": True})
        if from_date is None or to_date is None or from_date > to_date:
            return render(
                request, "fragments/activity.html", {**ctx, "backfill_error": "Invalid date range."}
            )
        if (to_date - from_date).days > _MAX_BACKFILL_DAYS:
            return render(
                request,
                "fragments/activity.html",
                {**ctx, "backfill_error": f"Range too wide; max {_MAX_BACKFILL_DAYS} days."},
            )
        from_utc, _ = local_day_bounds(from_date, tz)
        _, to_utc = local_day_bounds(min(to_date, _local_today(tz)), tz)
        to_utc = min(to_utc, datetime.now(UTC))
        if repo.running_run(archiver.device_id) is not None:
            return render(request, "fragments/activity.html", ctx)
        with contextlib.suppress(ConflictingIdError):
            queue_manual_backfill(scheduler, archiver, from_utc=from_utc, to_utc=to_utc)
        return render(request, "fragments/activity.html", _activity_context(archiver))

    @app.post("/actions/reconcile", response_class=HTMLResponse)
    def action_reconcile(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, _tz = ctx
        if scheduler is None:
            return render(
                request,
                "fragments/activity.html",
                {**_activity_context(archiver), "scheduler_down": True},
            )
        with contextlib.suppress(ConflictingIdError):
            queue_manual_reconcile(scheduler, archiver)
        return render(request, "fragments/activity.html", _activity_context(archiver))

    # -- schedule: when the standing jobs run, and whether they're on at
    # all --------------------------------------------------------------

    _SCHEDULE_JOB_LABELS = {
        "scheduled_archive": "Download new clips",
        "deep_backfill": "Weekly deep catch-up",
        "reconcile": "Check the vault against the database",
        "integrity_scan": "Spot-check stored clips for corruption",
    }

    def _schedule_context(
        archiver: Archiver, *, error: str | None = None, saved: bool = False
    ) -> dict[str, Any]:
        device_id = archiver.device_id
        schedule = load_effective_schedule(settings, repo, device_id=device_id)
        env_pinned = is_schedule_env_pinned()
        backfill_dow, backfill_time = _cron_weekly(schedule.backfill_cron)
        return {
            "active_nav": "schedule",
            "schedule": schedule,
            "env_pinned": env_pinned,
            "archive_time": _cron_daily_time(schedule.archive_cron),
            "backfill_dow": backfill_dow,
            "backfill_time": backfill_time,
            "weekday_names": list(enumerate(_WEEKDAY_NAMES)),
            "job_labels": _SCHEDULE_JOB_LABELS,
            "error": error,
            "saved": saved,
        }

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule_page(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, _tz = ctx
        return render(request, "schedule.html", _schedule_context(archiver))

    @app.post("/schedule", response_class=HTMLResponse)
    async def schedule_save(request: Request) -> Response:
        ctx = _device_ctx(request)
        if isinstance(ctx, Response):
            return ctx
        archiver, _tz = ctx
        if is_schedule_env_pinned():
            return render(
                request,
                "fragments/schedule_body.html",
                _schedule_context(archiver, error="The schedule is set by environment variables."),
            )
        body = await request.body()
        form = dict(parse_qsl(body.decode("utf-8", "replace")))
        try:
            new_schedule = ScheduleConfig(
                archive_enabled="archive_enabled" in form,
                archive_cron=_build_daily_cron(form.get("archive_time", "05:00")),
                archive_overlap_hours=int(form.get("archive_overlap_hours", 48)),
                backfill_enabled="backfill_enabled" in form,
                backfill_cron=_build_weekly_cron(
                    int(form.get("backfill_dow", 6)), form.get("backfill_time", "04:00")
                ),
                backfill_days=int(form.get("backfill_days", 30)),
                reconcile_enabled="reconcile_enabled" in form,
                reconcile_interval_hours=int(form.get("reconcile_interval_hours", 6)),
                integrity_scan_enabled="integrity_scan_enabled" in form,
                integrity_scan_sample_pct=float(form.get("integrity_scan_sample_pct", 5.0)),
                timezone=form.get("timezone") or None,
            )
        except (ValueError, PydanticValidationError) as exc:
            return render(
                request, "fragments/schedule_body.html", _schedule_context(archiver, error=str(exc))
            )

        save_schedule(repo, new_schedule, device_id=archiver.device_id)
        if scheduler is not None:
            device_row = repo.get_device(archiver.device_id)
            if device_row is not None:
                reschedule_device(
                    scheduler, archiver, repo, new_schedule, device_timezone=device_row.timezone
                )
        return render(
            request, "fragments/schedule_body.html", _schedule_context(archiver, saved=True)
        )

    # -- devices: see cameras, add one on the LAN --------------------------

    def _backfill_device_identity(d: DeviceRow) -> tuple[str | None, str | None, str | None]:
        """Fills `host`/`model`/`name` from reolink-cli when the TOML-seeding
        path (alias/channel/timezone only, see cli.py) left them unset, then
        caches the result via `upsert_device` so this only ever runs once per
        device. `host` comes from `registry.list_devices()`, a local call
        that needs no gateway and works even on a sleeping battery camera;
        `model`/`name` need `device_info`, which does need the gateway, so a
        camera that's merely asleep or a gateway that's down degrades
        silently back to the placeholder text rather than blocking the page
        load."""
        host, model, name = d.host, d.model, d.name
        if registry is None or (host is not None and model is not None and name is not None):
            return host, model, name
        if host is None:
            try:
                for rd in registry.list_devices():
                    if rd.alias == d.alias and rd.host:
                        host = rd.host
                        break
            except ProviderError:
                pass
        if model is None or name is None:
            try:
                info = registry.device_info(alias=d.alias)
            except ProviderError:
                info = None
            if info is not None:
                model, name = info.model, info.name
        if (host, model, name) != (d.host, d.model, d.name):
            repo.upsert_device(
                alias=d.alias,
                channel=d.channel,
                timezone=d.timezone,
                model=model,
                host=host,
                name=name,
            )
        return host, model, name

    def _refresh_storage_sample(d: DeviceRow, archiver: Archiver | None) -> StorageSampleRow | None:
        """A cheap, on-demand SD-card read for the device card: just
        `storage status`, never `health.record_storage_sample`'s 3650-day
        VOD scan (that one's for the coverage alarm and runs from the
        scheduler/CLI instead). Skipped when a sample less than an hour old
        already exists, and whenever the gateway is down, both to keep this
        page load fast."""
        from reovault.db.repository import parse_iso_utc

        sample = repo.latest_storage_sample(d.id)
        if archiver is None or not isinstance(archiver.provider, ReolinkCliProvider):
            return sample
        if not archiver.provider.gateway.is_listening():
            return sample
        if sample is not None:
            age = datetime.now(UTC) - parse_iso_utc(sample.sampled_at)
            if age < timedelta(hours=1):
                return sample
        try:
            storage = archiver.provider.storage_status()
        except ProviderError:
            return sample
        prior_oldest = (
            parse_iso_utc(sample.oldest_recording_utc)
            if sample is not None and sample.oldest_recording_utc
            else None
        )
        repo.record_storage_sample(
            device_id=d.id,
            total_gb=storage.total_gb,
            remain_gb=storage.remain_gb,
            formatted=storage.formatted,
            mounted=storage.mounted,
            oldest_recording_utc=prior_oldest,
        )
        return repo.latest_storage_sample(d.id)

    def _sd_bar_context(
        sample: StorageSampleRow | None, on_card: OnCardBytesRow
    ) -> dict[str, Any] | None:
        """Archived/used/free segments for the device card's SD usage bar.
        `remote_size` is decimal bytes, but the camera's own
        `totalGB`/`remainGB` are binary GiB (a
        256GB/1e9 card reports 238.7 "GB"), so both convert through 2**30 to
        compare on the same scale. Deliberately two segments, not three: an
        explicit "not yet archived" bucket needs the card's actual retention
        boundary to mean anything precise, which isn't reliably available
        (see `_devices_context`'s `since`), and the user can already read
        "not yet archived" straight off archived vs. used -- a third,
        similarly-colored segment for it added confusion, not information."""
        if sample is None or not sample.total_gb:
            return None
        total_gb = sample.total_gb
        used_gb = max(total_gb - (sample.remain_gb or 0), 0.0)
        archived_gb = min(on_card.archived_bytes / 2**30, used_gb)
        other_gb = max(used_gb - archived_gb, 0.0)
        return {
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": max(total_gb - used_gb, 0.0),
            "archived_gb": archived_gb,
            "other_gb": other_gb,
            "archived_pct": 100 * archived_gb / total_gb,
            "other_pct": 100 * other_gb / total_gb,
        }

    def _devices_context(
        *,
        discover_results: Any = None,
        discover_error: str | None = None,
        add_error: str | None = None,
    ) -> dict[str, Any]:
        from reovault.db.repository import parse_iso_utc

        rows = []
        for d in repo.list_devices():
            host, model, name = _backfill_device_identity(d)
            totals = repo.totals(d.id)
            archiver = fleet.get(d.id)
            gateway_down = None
            if archiver is not None and isinstance(archiver.provider, ReolinkCliProvider):
                gateway_down = not archiver.provider.gateway.is_listening()
            sample = _refresh_storage_sample(d, archiver)
            # Not `sample.oldest_recording_utc`: that field needs a full
            # 3650-day `vod search` (see health.record_storage_sample),
            # which can time out even though the card holds only a few days
            # of footage -- it comes back empty far more often than it
            # succeeds, so gating the whole "not yet archived" bucket on it
            # left that bucket permanently at 0. `totals.first_start_utc` is
            # the earliest
            # recording this device has ever actually discovered, already
            # computed by the `totals()` call above with no extra query,
            # and never depends on the camera responding to anything.
            since = parse_iso_utc(totals.first_start_utc) if totals.first_start_utc else None
            on_card = repo.on_card_bytes(d.id, since)
            last_run = repo.last_finished_run(d.id)
            rows.append(
                {
                    "id": d.id,
                    "alias": d.alias,
                    "host": host,
                    "model": model,
                    "name": name,
                    "timezone": d.timezone,
                    "enabled": d.enabled,
                    "archived_count": totals.archived_count,
                    "archived_bytes": totals.archived_plaintext_bytes,
                    "sample": sample,
                    "sd": _sd_bar_context(sample, on_card),
                    "last_run": last_run,
                    "gateway_down": gateway_down,
                }
            )
        return {
            "active_nav": "devices",
            "devices": rows,
            "registry_available": registry is not None,
            "discover_results": discover_results,
            "discover_error": discover_error,
            "add_error": add_error,
        }

    @app.get("/devices", response_class=HTMLResponse)
    def devices_page(request: Request) -> Response:
        return render(request, "devices.html", _devices_context())

    @app.post("/devices/discover", response_class=HTMLResponse)
    def devices_discover(request: Request) -> Response:
        if registry is None:
            return render(
                request,
                "fragments/discover_results.html",
                {"discover_error": "Camera discovery isn't available on this server."},
            )
        try:
            results = registry.discover()
        except ProviderError as exc:
            return render(request, "fragments/discover_results.html", {"discover_error": str(exc)})
        return render(request, "fragments/discover_results.html", {"discover_results": results})

    @app.post("/devices", response_class=HTMLResponse)
    async def devices_add(request: Request) -> Response:
        if registry is None:
            return render(
                request,
                "fragments/devices_body.html",
                _devices_context(add_error="Adding a camera isn't available on this server."),
            )
        body = await request.body()
        form = dict(parse_qsl(body.decode("utf-8", "replace")))
        try:
            req = AddDeviceRequest(
                alias=form.get("alias", ""),
                host=form.get("host", ""),
                user=form.get("user", ""),
                password=SecretStr(form.get("password", "")),
                channel=int(form["channel"]) if form.get("channel") else None,
                timezone=form.get("timezone", ""),
                name=form.get("name") or None,
            )
        except (PydanticValidationError, ValueError):
            return render(
                request,
                "fragments/devices_body.html",
                _devices_context(
                    add_error="Please fill in alias, host, user, password and timezone."
                ),
            )

        try:
            ZoneInfo(req.timezone)
        except Exception:  # noqa: BLE001 - any zoneinfo failure is the same user-facing error
            return render(
                request,
                "fragments/devices_body.html",
                _devices_context(add_error=f"Unknown timezone: {req.timezone!r}"),
            )

        # Alias collision across both namespaces: ReoVault's own devices
        # table and reolink-cli's registry are two different places an
        # alias could already exist.
        existing = {d.alias for d in repo.list_devices()} | {
            d.alias for d in registry.list_devices()
        }
        if req.alias in existing:
            return render(
                request,
                "fragments/devices_body.html",
                _devices_context(add_error=f"{req.alias!r} is already registered."),
            )

        try:
            registry.add_device(
                alias=req.alias,
                host=req.host,
                user=req.user,
                password=req.password,
                channel=req.channel,
            )
        except ProviderError as exc:
            return render(
                request,
                "fragments/devices_body.html",
                _devices_context(add_error=f"Could not add the camera: {exc}"),
            )

        device_id = repo.upsert_device(
            alias=req.alias,
            channel=req.channel or 0,
            timezone=req.timezone,
            host=req.host,
            user=req.user,
            name=req.name,
        )
        device_row = repo.get_device(device_id)
        if device_row is not None:
            archiver = fleet.add(device_row)
            if scheduler is not None:
                schedule = load_effective_schedule(settings, repo, device_id=device_id)
                add_device_jobs(scheduler, archiver, schedule, device_timezone=device_row.timezone)
        return render(request, "fragments/devices_body.html", _devices_context())

    @app.post("/devices/{device_id}/toggle", response_class=HTMLResponse)
    def devices_toggle(request: Request, device_id: int) -> Response:
        device_row = repo.get_device(device_id)
        if device_row is None:
            return PlainTextResponse("not found", status_code=404)
        new_enabled = not device_row.enabled
        repo.set_device_enabled(device_id, new_enabled)
        if new_enabled:
            updated = repo.get_device(device_id)
            if updated is not None:
                archiver = fleet.add(updated)
                if scheduler is not None:
                    schedule = load_effective_schedule(settings, repo, device_id=device_id)
                    add_device_jobs(scheduler, archiver, schedule, device_timezone=updated.timezone)
        else:
            fleet.remove(device_id)
            if scheduler is not None:
                remove_device_jobs(scheduler, device_id)
        return render(request, "fragments/devices_body.html", _devices_context())

    # -- context builders (closures: need repo/settings; device_id/tz/
    # archiver are explicit params, resolved per-request via _device_ctx,
    # not closure constants) -----------------------------------------------

    def _rec_view(row: RecordingRow, tz: str) -> RecView:
        from reovault.db.repository import parse_iso_utc

        local_dt = parse_iso_utc(row.start_utc).astimezone(ZoneInfo(tz))
        return RecView(
            id=row.id,
            remote_name=row.remote_name,
            start_utc=row.start_utc,
            local_time=local_dt.strftime("%H:%M"),
            local_datetime=local_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
            duration_s=row.duration_s,
            rec_type=row.rec_type,
            type_list=[t for t in (row.rec_type or "").split(",") if t],
            state=row.state,
            plaintext_size=row.plaintext_size,
            ciphertext_size=row.ciphertext_size,
            plaintext_sha256=row.plaintext_sha256,
            archived_at=row.archived_at,
            last_error=row.last_error,
            last_error_class=row.last_error_class,
            last_error_class_text=_ERROR_CLASS_TEXT.get(row.last_error_class or ""),
            last_error_detail=row.last_error_detail,
            remote_size=row.remote_size,
            attempts=row.attempts,
            next_attempt_at=row.next_attempt_at,
            next_attempt_local=(
                parse_iso_utc(row.next_attempt_at).astimezone(ZoneInfo(tz)).strftime("%H:%M")
                if row.next_attempt_at
                else None
            ),
        )

    def _health_context(archiver: Archiver) -> dict[str, Any]:
        from reovault.db.repository import parse_iso_utc

        device_id = archiver.device_id
        totals = repo.totals(device_id)
        coverage = health.coverage_margin(repository=repo, device_id=device_id)
        sample = repo.latest_storage_sample(device_id)
        since = parse_iso_utc(totals.first_start_utc) if totals.first_start_utc else None
        on_card = repo.on_card_bytes(device_id, since)
        running = repo.running_run(device_id)
        last_run = repo.last_finished_run(device_id)
        times = next_run_times(scheduler).get(device_id, {}) if scheduler is not None else {}
        next_archive = times.get("scheduled_archive")

        gateway_down = False
        if isinstance(archiver.provider, ReolinkCliProvider):
            gateway_down = not archiver.provider.gateway.is_listening()

        problems_count = totals.failed_count + totals.quarantined_count

        return {
            "verdict": _verdict_text(last_run, running),
            "verdict_sub": _verdict_sub_text(last_run, next_archive),
            "state_class": _verdict_state_class(last_run, running),
            "coverage": coverage,
            "coverage_pct": None
            if coverage.margin_fraction is None
            else round(coverage.margin_fraction * 100),
            "sample": sample,
            "sd": _sd_bar_context(sample, on_card),
            "gateway_down": gateway_down,
            "problems_count": problems_count,
            "banner": _banner_context(sample, coverage, gateway_down, problems_count),
        }

    def _growth_context(archiver: Archiver, tz: str, days: int) -> dict[str, Any]:
        device_id = archiver.device_id
        totals = repo.totals(device_id)
        now = datetime.now(UTC)
        from_utc = now - timedelta(days=days)
        series = repo.runs_per_day(device_id=device_id, from_utc=from_utc, to_utc=now, tz=tz)
        by_day = {r.day: r.bytes_archived for r in series}
        day_list = [(from_utc + timedelta(days=i)).date().isoformat() for i in range(days)]
        values = [by_day.get(d, 0) for d in day_list]
        peak = max(values) if values and max(values) > 0 else 1
        # Precomputed x/y/width/height (percent-of-viewBox) per bar, not a
        # raw 0-1 ratio for a `style="--v:...` custom property: see the
        # .meter-svg comment in app.css for why (style-src CSP). A day with
        # 0 bytes still gets a 5%-height sliver, replacing the old
        # `min-height: 2px` floor.
        n = len(values)
        slot = 100 / n if n else 0
        bar_width = round(slot * 0.82, 3)
        bars = []
        for i, v in enumerate(values):
            height = max((v / peak) * 100, 5)
            bars.append(
                {
                    "x": round(i * slot, 3),
                    "width": bar_width,
                    "y": round(100 - height, 3),
                    "height": round(height, 3),
                }
            )
        avg_per_day = sum(values) / days if days else 0
        free_gb = None
        vault_free_str = None
        try:
            import shutil

            usage = shutil.disk_usage(settings.storage.vault_dir)
            free_gb = usage.free / (1024**3)
            days_headroom = int(free_gb * (1024**3) / avg_per_day) if avg_per_day > 0 else None
            vault_free_str = f"{free_gb:.1f} GB free"
        except OSError:
            days_headroom = None

        return {
            "archived_count": totals.archived_count,
            "archived_bytes": totals.archived_plaintext_bytes,
            "bars": bars,
            "avg_per_day": avg_per_day,
            "yearly_projection": avg_per_day * 365,
            "vault_free_str": vault_free_str,
            "days_headroom": days_headroom,
        }

    def _activity_context(archiver: Archiver) -> dict[str, Any]:
        device_id = archiver.device_id
        running = repo.running_run(device_id)
        # The activity log's listener is attached once to the whole
        # scheduler (every device's jobs), so events are filtered here to
        # just this device's -- otherwise camera A's Health page would show
        # camera B's scheduler activity mixed in.
        events = [e for e in activity_log.recent() if device_of_job_id(e.job_id) == device_id]
        poll_secs = 5 if running is not None else 30
        device_row = repo.get_device(device_id)
        tz = device_row.timezone if device_row is not None else "UTC"
        return {"running": running, "events": events[:5], "poll_secs": poll_secs, "tz": tz}

    def _banner_context(
        sample: Any, coverage: Any, gateway_down: bool, problems_count: int
    ) -> dict[str, Any] | None:
        if sample is not None and sample.mounted is False:
            return {"level": "bad", "text": "The SD card is not mounted.", "href": None}
        if coverage.alarm:
            pct = round((coverage.margin_fraction or 0) * 100)
            return {
                "level": "warn",
                "text": f"Falling behind: only {pct}% of the card's retention window is left.",
                "href": None,
            }
        if gateway_down:
            return {"level": "warn", "text": "The camera gateway is unreachable.", "href": None}
        if problems_count > 0:
            plural = "s" if problems_count != 1 else ""
            return {
                "level": "bad",
                "text": f"{problems_count} recording{plural} need attention.",
                "href": "/problems",
            }
        return None

    def _calendar_context(device_id: int, tz: str, month: str) -> dict[str, Any]:
        year, mon = (int(x) for x in month.split("-"))
        month_start = date(year, mon, 1)
        next_month = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
        from_utc, _ = local_day_bounds(month_start, tz)
        _, to_utc = local_day_bounds(next_month - timedelta(days=1), tz)
        buckets = {
            b.day: b
            for b in repo.day_buckets(device_id=device_id, from_utc=from_utc, to_utc=to_utc, tz=tz)
        }
        max_total = max((b.total for b in buckets.values()), default=0) or 1

        weeks: list[list[dict[str, Any] | None]] = []
        first_weekday = month_start.weekday()  # Monday=0
        week: list[dict[str, Any] | None] = [None] * first_weekday
        d = month_start
        today = _local_today(tz)
        while d < next_month:
            key = d.isoformat()
            bucket = buckets.get(key)
            density = 0
            if bucket:
                density = max(1, round((bucket.total / max_total) * 4))
            week.append(
                {
                    "iso": key,
                    "day": d.day,
                    "count": bucket.total if bucket else 0,
                    "density": density,
                    "problems": bucket.problems if bucket else 0,
                    "is_today": d == today,
                    "in_future": d > today,
                }
            )
            if len(week) == 7:
                weeks.append(week)
                week = []
            d += timedelta(days=1)
        if week:
            week.extend([None] * (7 - len(week)))
            weeks.append(week)

        prev_month = date(year, mon - 1, 1) if mon > 1 else date(year - 1, 12, 1)
        return {
            "month_label": month_start.strftime("%B %Y"),
            "month": month,
            "prev_month": f"{prev_month.year:04d}-{prev_month.month:02d}",
            "next_month": f"{next_month.year:04d}-{next_month.month:02d}",
            "weeks": weeks,
        }

    def _day_context(device_id: int, tz: str, day: date, types: str) -> dict[str, Any]:
        from_utc, to_utc = local_day_bounds(day, tz)
        state_filter = ["failed", "quarantined"] if types == "__problems__" else None
        rec_type_filter = types if types and types != "__problems__" else None
        hour_buckets = {
            b.hour: b
            for b in repo.hour_buckets(device_id=device_id, from_utc=from_utc, to_utc=to_utc, tz=tz)
        }
        # Type-filtered counts recompute the per-hour totals so the summary
        # lines stay honest after a chip toggle: a naive filter would leave
        # stale counts on screen.
        if rec_type_filter or state_filter:
            rows = repo.recordings_in_window(
                device_id=device_id,
                from_utc=from_utc,
                to_utc=to_utc,
                states=state_filter,
                rec_type=rec_type_filter,
                limit=100000,
            )
            hour_buckets = _bucket_rows_by_local_hour(rows, tz)

        available_types = repo.distinct_rec_types(
            device_id=device_id, from_utc=from_utc, to_utc=to_utc
        )
        total = sum(b.total for b in hour_buckets.values())
        total_bytes = sum(b.bytes for b in hour_buckets.values())
        # Only hours with at least one clip are rendered: a per-hour "quiet"
        # row for every silent hour buries the real clips under a wall of
        # noise on a typical day. `hidden_hours` feeds the one summary line
        # the template shows instead of each individual quiet hour.
        hours = []
        hidden_hours = 0
        first_open_set = False
        for h in range(24):
            b = hour_buckets.get(h)
            if not b or b.total == 0:
                hidden_hours += 1
                continue
            is_first_open = False
            if not first_open_set:
                is_first_open = True
                first_open_set = True
            hours.append(
                {
                    "hour": h,
                    "label": f"{h:02d}:00",
                    "total": b.total,
                    "bytes": b.bytes,
                    "open": is_first_open,
                }
            )

        prev_day = day - timedelta(days=1)
        next_day = day + timedelta(days=1)
        today = _local_today(tz)
        return {
            "date": day.isoformat(),
            "date_label": day.strftime("%a %d %b"),
            "prev_date": prev_day.isoformat(),
            "next_date": next_day.isoformat(),
            "next_disabled": next_day > today,
            "total": total,
            "total_bytes": total_bytes,
            "hours": hours,
            "hidden_hours": hidden_hours,
            "available_types": available_types,
            "selected_types": [t for t in types.split(",") if t]
            if types and types != "__problems__"
            else [],
            "types_raw": types,
            "is_future": day > today,
            "is_empty_day": total == 0,
        }

    def _hour_rows_context(
        device_id: int, tz: str, day: date, hour: int, types: str, after: str
    ) -> dict[str, Any]:
        from_utc, day_end = local_day_bounds(day, tz)
        # Fetch the whole day and filter to this local hour in Python,
        # rather than recomputing a per-hour UTC window (which would
        # duplicate the DST segmentation already inside
        # Repository.hour_buckets). One day is a small, bounded fetch.
        state_filter = ["failed", "quarantined"] if types == "__problems__" else None
        rec_type_filter = types if types and types != "__problems__" else None
        rows = repo.recordings_in_window(
            device_id=device_id,
            from_utc=from_utc,
            to_utc=day_end,
            states=state_filter,
            rec_type=rec_type_filter,
            limit=100000,
        )
        zone = ZoneInfo(tz)
        matching = [r for r in rows if _to_local(r.start_utc, zone).hour == hour]
        matching.sort(key=lambda r: r.start_utc)
        cursor = _parse_cursor(after)
        if cursor:
            matching = [r for r in matching if (r.start_utc, r.id) > cursor]
        page = matching[:200]
        return {
            "date": day.isoformat(),
            "hour": hour,
            "types": types,
            "rows": [_rec_view(r, tz) for r in page],
            "has_more": len(matching) > 200,
            "next_after": f"{page[-1].start_utc},{page[-1].id}"
            if page and len(matching) > 200
            else "",
        }

    return app


# -- module-level helpers (no closure state needed) ------------------------


def _origin_matches_host(origin: str, request: Request) -> bool:
    parsed = urlparse(origin)
    host_header = request.headers.get("host", "")
    return parsed.netloc == host_header


def _local_today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def _to_local(iso_utc: str, zone: ZoneInfo) -> datetime:
    from reovault.db.repository import parse_iso_utc

    return parse_iso_utc(iso_utc).astimezone(zone)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# -- schedule cron <-> simple-mode form fields: the Schedule tab's simple
# mode is one <select> plus one <input type="time">, and the server
# assembles/parses the 5-field cron string, so the page needs no
# JavaScript. Only "daily at HH:MM" and
# "weekly on <day> at HH:MM" shapes are supported -- exactly the two this
# app actually schedules (archive, backfill) -- not general cron.

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Plain-English sentences for the Problems tab: one entry per `ErrorClass`,
# since "protocol" or "exit code -2" means nothing to someone who isn't
# reading reolink-cli's source.
_ERROR_CLASS_TEXT = {
    "input": "ReoVault sent reolink-cli a request it rejected.",
    "network": "Couldn't reach the camera over the network.",
    "auth": "The camera rejected the stored credentials.",
    "device": "The camera reported a problem on its end.",
    "protocol": "The camera's response didn't match what ReoVault expected.",
    "local": "Something on the ReoVault server itself failed (disk, permissions, or the gateway).",
}


def _cron_daily_time(cron: str) -> str:
    """'M H * * *' -> 'HH:MM'. Falls back to '00:00' for anything that
    doesn't parse (defensive only: every cron here was built by
    `_build_daily_cron` or came from a validated config default)."""
    parts = cron.split()
    if len(parts) != 5:
        return "00:00"
    minute, hour = parts[0], parts[1]
    try:
        return f"{int(hour):02d}:{int(minute):02d}"
    except ValueError:
        return "00:00"


def _build_daily_cron(time_str: str) -> str:
    hour, _, minute = time_str.partition(":")
    return f"{int(minute)} {int(hour)} * * *"


def _cron_weekly(cron: str) -> tuple[int, str]:
    """'M H * * D' -> (weekday 0=Mon..6=Sun, 'HH:MM'). Cron's own day-of-week
    is 0=Sun..6=Sat; converted here to Python's Monday-first convention,
    which is what the `<select>` options and `_WEEKDAY_NAMES` use."""
    parts = cron.split()
    if len(parts) != 5:
        return 6, "00:00"
    minute, hour, cron_dow = parts[0], parts[1], parts[4]
    try:
        py_dow = (int(cron_dow) - 1) % 7
        return py_dow, f"{int(hour):02d}:{int(minute):02d}"
    except ValueError:
        return 6, "00:00"


def _build_weekly_cron(py_weekday: int, time_str: str) -> str:
    hour, _, minute = time_str.partition(":")
    cron_dow = (py_weekday + 1) % 7
    return f"{int(minute)} {int(hour)} * * {cron_dow}"


def _parse_cursor(s: str) -> tuple[str, int] | None:
    if not s or "," not in s:
        return None
    ts, _, id_str = s.rpartition(",")
    try:
        return (ts, int(id_str))
    except ValueError:
        return None


def _bucket_rows_by_local_hour(rows: list[RecordingRow], tz: str) -> dict[int, Any]:
    from reovault.db.repository import HourBucketRow, parse_iso_utc

    zone = ZoneInfo(tz)
    buckets: dict[int, dict[str, int]] = {}
    for r in rows:
        h = parse_iso_utc(r.start_utc).astimezone(zone).hour
        b = buckets.setdefault(h, {"total": 0, "archived": 0, "problems": 0, "bytes": 0})
        b["total"] += 1
        if r.state == "archived":
            b["archived"] += 1
            b["bytes"] += r.plaintext_size or 0
        if r.state in ("failed", "quarantined"):
            b["problems"] += 1
    return {
        h: HourBucketRow(
            hour=h,
            total=v["total"],
            archived=v["archived"],
            problems=v["problems"],
            bytes=v["bytes"],
        )
        for h, v in buckets.items()
    }


@dataclass(frozen=True, slots=True)
class RecView:
    id: int
    remote_name: str
    start_utc: str
    local_time: str  # "HH:MM" in the device's timezone, for display
    local_datetime: str  # full local timestamp, for title/detail
    duration_s: float | None
    rec_type: str | None
    type_list: list[str]
    state: str
    plaintext_size: int | None
    ciphertext_size: int | None
    plaintext_sha256: str | None
    archived_at: str | None
    last_error: str | None
    last_error_class: str | None
    last_error_class_text: str | None
    last_error_detail: str | None
    remote_size: int | None
    attempts: int
    next_attempt_at: str | None
    next_attempt_local: str | None


@dataclass(frozen=True, slots=True)
class RunView:
    id: int
    trigger: str
    started_at: str
    finished_at: str | None
    outcome: str | None
    discovered: int
    downloaded: int
    skipped_dup: int
    failed: int
    bytes_archived: int
    error: str | None


def _run_view(row: RunRow) -> RunView:
    return RunView(
        id=row.id,
        trigger=row.trigger,
        started_at=row.started_at,
        finished_at=row.finished_at,
        outcome=row.outcome,
        discovered=row.discovered,
        downloaded=row.downloaded,
        skipped_dup=row.skipped_dup,
        failed=row.failed,
        bytes_archived=row.bytes_archived,
        error=row.error,
    )


def _verdict_text(last_run: RunRow | None, running: RunRow | None) -> str:
    if running is not None:
        return f"Run in progress ({running.trigger})."
    if last_run is None:
        return "No run yet."
    if last_run.outcome == "success":
        bytes_str = human_bytes(last_run.bytes_archived)
        return f"Last run archived {last_run.downloaded} clips, {bytes_str}."
    if last_run.outcome == "partial":
        return f"Last run archived {last_run.downloaded} clips, {last_run.failed} failed."
    if last_run.outcome == "aborted":
        return "Last run failed to start."
    return f"Last run failed after {last_run.downloaded} clips."


def _verdict_state_class(last_run: RunRow | None, running: RunRow | None) -> str:
    if running is not None:
        return "ok"
    if last_run is None:
        return "idle"
    return {"success": "ok", "partial": "warn", "failed": "bad", "aborted": "bad"}.get(
        last_run.outcome or "", "idle"
    )


def _verdict_sub_text(last_run: RunRow | None, next_run: datetime | None) -> str:
    from reovault.db.repository import parse_iso_utc

    parts = []
    if last_run is not None and last_run.finished_at:
        finished = parse_iso_utc(last_run.finished_at)
        parts.append(f"Finished {_relative(finished)}.")
    if next_run is not None:
        parts.append(f"Next run {_relative(next_run)}.")
    return " ".join(parts) if parts else "No run history yet."


def _relative(dt: datetime) -> str:
    now = datetime.now(UTC)
    delta = dt - now
    secs = delta.total_seconds()
    if abs(secs) < 60:
        return "just now" if secs <= 0 else "in under a minute"
    hours = int(abs(secs) // 3600)
    mins = int((abs(secs) % 3600) // 60)
    if secs < 0:
        return f"{hours}h ago" if hours else f"{mins}m ago"
    return f"in {hours}h" if hours else f"in {mins}m"


def _manifest_body(asset_version: str) -> dict[str, Any]:
    icon_dir = "/static/icons"
    v = f"?v={asset_version}"
    return {
        "id": "/",
        "name": "ReoVault",
        "short_name": "ReoVault",
        "description": "Local-first, encrypted archive of your camera recordings.",
        "start_url": "/?utm_source=pwa",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "background_color": "#000814",
        "theme_color": "#000814",
        "lang": "en",
        "dir": "ltr",
        "categories": ["utilities", "security"],
        "icons": [
            {
                "src": f"{icon_dir}/icon-192.png{v}",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": f"{icon_dir}/icon-512.png{v}",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": f"{icon_dir}/icon-maskable-512.png{v}",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
            {
                "src": f"{icon_dir}/icon.svg{v}",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            },
        ],
        "shortcuts": [
            {
                "name": "Today's footage",
                "url": "/recordings",
                "icons": [{"src": f"{icon_dir}/icon-192.png{v}", "sizes": "192x192"}],
            },
            {
                "name": "Problems",
                "url": "/problems",
                "icons": [{"src": f"{icon_dir}/icon-192.png{v}", "sizes": "192x192"}],
            },
        ],
    }
