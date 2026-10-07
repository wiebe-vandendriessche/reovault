"""Session bootstrap, login, logout.

`GET /session` is public: the dashboard calls it first to learn whether
it's logged in and to get the CSRF token it must send as `X-CSRF-Token` on
every mutating request (login included, bound to the anonymous jti).
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from reovault import __version__
from reovault.web import security
from reovault.web.auth import load_password_hash
from reovault.web.deps import State

router = APIRouter(tags=["auth"])

SESSION_COOKIE = "rv_session"


class SessionOut(BaseModel):
    authenticated: bool
    csrf_token: str
    version: str
    pinned_cli_version: str


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


def _session_out(st: State, jti: str | None) -> SessionOut:
    # An authenticated token lives as long as the session it's bound to; the
    # anonymous one (only good for the login POST) stays short.
    max_age = st.settings.web.session_max_age_days * 86400 if jti else 600
    return SessionOut(
        authenticated=jti is not None,
        csrf_token=security.sign_csrf(
            st.session_key, jti=jti or security.ANON_JTI, max_age_secs=max_age
        ),
        version=__version__,
        pinned_cli_version=st.settings.reolink_cli.pinned_version,
    )


@router.get("/session")
def get_session(request: Request, st: State) -> SessionOut:
    return _session_out(st, request.state.session_jti)


@router.post("/login", responses={401: {}, 429: {}})
def login(body: LoginIn, request: Request, response: Response, st: State) -> SessionOut:
    web = st.settings.web
    client_key = request.client.host if request.client else "unknown"
    retry_after = st.limiter.retry_after(client_key)
    if retry_after is not None:
        minutes = int(retry_after // 60) + 1
        raise HTTPException(
            429,
            f"Too many attempts. Try again in {minutes} minutes.",
            headers={"Retry-After": str(int(retry_after))},
        )

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


@router.post("/logout")
def logout(response: Response, st: State) -> SessionOut:
    """Revokes every session server-side by bumping the epoch, not just this
    browser's cookie: a copied cookie stops working too."""
    st.repo.set_setting("session_epoch", str(st.session_epoch() + 1))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return _session_out(st, None)
