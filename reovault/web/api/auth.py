"""Session bootstrap, login, logout.

`GET /session` is public: the dashboard calls it first to learn whether
it's logged in and to get the CSRF token it must send as `X-CSRF-Token` on
every mutating request (login included, bound to the anonymous jti).
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from reovault import __version__, updates
from reovault.config_store import env_locked, set_fields, table_at
from reovault.logging import get_logger
from reovault.web import security
from reovault.web.api.config import write
from reovault.web.auth import load_password_hash, write_password_file
from reovault.web.deps import State

router = APIRouter(tags=["auth"])
logger = get_logger(__name__)

SESSION_COOKIE = "rv_session"
MIN_PASSWORD = 10
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class SessionOut(BaseModel):
    authenticated: bool
    csrf_token: str
    version: str
    pinned_cli_version: str
    # No login exists yet: the dashboard shows the setup screen. `setup_email`
    # is a login email already set in the config or environment (then the form
    # doesn't ask for one).
    setup_needed: bool = False
    setup_email: str | None = None
    # A newer release than this one, when the daily check found one.
    update_available: str | None = None


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class SetupIn(BaseModel):
    code: str = Field(max_length=64)
    email: str | None = Field(default=None, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD, max_length=1024)


class PasswordIn(BaseModel):
    current: str = Field(max_length=1024)
    password: str | None = Field(default=None, min_length=MIN_PASSWORD, max_length=1024)
    email: str | None = Field(default=None, max_length=320)


def new_setup_code() -> str:
    """8 characters, unambiguous (no 0/O, 1/I), shown as XXXX-XXXX."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _norm(code: str) -> bytes:
    return code.replace("-", "").replace(" ", "").upper().encode()


def _session_out(st: State, jti: str | None) -> SessionOut:
    # An authenticated token lives as long as the session it's bound to; the
    # anonymous one (only good for the login and setup POSTs) stays short.
    web = st.settings.web
    max_age = web.session_max_age_days * 86400 if jti else 600
    setup = load_password_hash(web) is None
    update = None
    if jti is not None and web.update_check:
        update = updates.newer(__version__, st.db.get_setting("latest_release"))
    return SessionOut(
        authenticated=jti is not None,
        csrf_token=security.sign_csrf(
            st.session_key, jti=jti or security.ANON_JTI, max_age_secs=max_age
        ),
        version=__version__,
        pinned_cli_version=st.settings.tools.reolink_cli_version,
        setup_needed=setup,
        setup_email=(web.email or None) if setup else None,
        update_available=update,
    )


def _throttle(st: State, request: Request) -> str:
    """The caller's rate-limit key; raises 429 while locked out. Setup codes,
    logins and password changes all count against the same buckets."""
    key = request.client.host if request.client else "unknown"
    retry_after = st.limiter.retry_after(key)
    if retry_after is not None:
        minutes = int(retry_after // 60) + 1
        raise HTTPException(
            429,
            f"Too many attempts. Try again in {minutes} minutes.",
            headers={"Retry-After": str(int(retry_after))},
        )
    return key


def confirm_password(st: State, request: Request, password: str | None) -> None:
    """Destructive changes (tightening retention, disconnecting or switching the
    primary destination) ask for the dashboard password again, so a stolen
    session can't wipe backups. Counts against the same lockout as logins."""
    client_key = _throttle(st, request)
    current = load_password_hash(st.settings.web)
    if not password or current is None or not security.verify_password(password.encode(), current):
        st.limiter.record_failure(client_key)
        raise HTTPException(401, "Enter your dashboard password to confirm this change.")
    st.limiter.record_success(client_key)


def _start_session(response: Response, st: State) -> SessionOut:
    web = st.settings.web
    jti = security.new_jti()
    max_age = web.session_max_age_days * 86400
    token = security.sign_session(
        st.session_key, jti=jti, max_age_secs=max_age, epoch=st.session_epoch()
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=web.cookie_secure,
        samesite=web.cookie_samesite,  # type: ignore[arg-type]
        max_age=max_age,
        path="/",
    )
    return _session_out(st, jti)


@router.get("/session")
def get_session(request: Request, st: State) -> SessionOut:
    return _session_out(st, request.state.session_jti)


@router.post("/setup", responses={403: {}, 409: {}, 422: {}, 429: {}})
def setup(body: SetupIn, request: Request, response: Response, st: State) -> SessionOut:
    """First run: the one-time code from the server log creates the login."""
    web = st.settings.web
    client_key = _throttle(st, request)
    with st.setup_lock:  # two browsers: the first to finish wins, the other gets 409
        if load_password_hash(web) is not None or st.setup_code is None:
            raise HTTPException(409, "ReoVault is already set up.")
        if not hmac.compare_digest(_norm(body.code), _norm(st.setup_code)):
            st.limiter.record_failure(client_key)
            raise HTTPException(403, "That setup code is wrong. It is in the server log.")
        email = (web.email or body.email or "").strip()
        if "@" not in email:
            raise HTTPException(422, "Enter an email address: it is only the login name.")
        # The password file is the commit point, so it goes last: a failure
        # before it leaves ReoVault in setup, and setup can simply be retried.
        if not web.email:
            write(st, lambda doc: set_fields(table_at(doc, "web"), {"email": email}))
        write_password_file(web.password_file, security.hash_password(body.password.encode()))
        st.setup_code = None
    st.limiter.record_success(client_key)
    logger.info("setup.done")
    return _start_session(response, st)


@router.post("/login", responses={401: {}, 429: {}})
def login(body: LoginIn, request: Request, response: Response, st: State) -> SessionOut:
    web = st.settings.web
    client_key = _throttle(st, request)
    password_hash = load_password_hash(web)
    # Both checks must always run, never short-circuit: a wrong email has
    # to cost the same ~155ms Argon2id takes as a wrong password, or the
    # email field becomes a free timing oracle for enumeration. `&` below is
    # a deliberate reminder of that; don't "clean" it into `and`.
    email_ok = hmac.compare_digest(
        body.email.strip().casefold().encode(), (web.email or "").strip().casefold().encode()
    )
    password_ok = (
        security.verify_password(body.password.encode(), password_hash)
        if password_hash is not None
        else False
    )
    if not (email_ok & password_ok):
        st.limiter.record_failure(client_key)
        raise HTTPException(401, "Wrong email or password.")

    st.limiter.record_success(client_key)
    return _start_session(response, st)


@router.post("/password", responses={401: {}, 409: {}, 422: {}, 429: {}})
def change_password(
    body: PasswordIn, request: Request, response: Response, st: State
) -> SessionOut:
    """Change the login password and/or email. Needs the current password even
    though the caller is signed in: a stolen session mustn't be able to take
    the account over. Signs every other session out."""
    web = st.settings.web
    client_key = _throttle(st, request)
    current = load_password_hash(web)
    if current is None or not security.verify_password(body.current.encode(), current):
        st.limiter.record_failure(client_key)
        raise HTTPException(401, "Wrong current password.")
    if body.password is None and not (body.email or "").strip():
        raise HTTPException(422, "Nothing to change.")
    if body.password is not None and web.password_hash:
        raise HTTPException(409, "The password is set by REOVAULT_WEB__PASSWORD_HASH.")
    if (body.email or "").strip() and env_locked("web", "email"):
        raise HTTPException(409, "The email is set by REOVAULT_WEB__EMAIL.")
    email = (body.email or "").strip()
    if body.email is not None and "@" not in email:
        raise HTTPException(422, "Enter an email address: it is only the login name.")
    if email:
        write(st, lambda doc: set_fields(table_at(doc, "web"), {"email": email}))
    if body.password is not None:
        write_password_file(web.password_file, security.hash_password(body.password.encode()))
    st.limiter.record_success(client_key)
    st.db.set_setting("session_epoch", str(st.session_epoch() + 1))
    return _start_session(response, st)


@router.post("/logout")
def logout(response: Response, st: State) -> SessionOut:
    """Revokes every session server-side by bumping the epoch, not just this
    browser's cookie: a copied cookie stops working too."""
    st.db.set_setting("session_epoch", str(st.session_epoch() + 1))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return _session_out(st, None)
