"""Session/CSRF signing and Argon2id password hashing, all hand-rolled with
no new dependency."""

import os

from reovault.web.security import (
    ANON_JTI,
    hash_password,
    new_jti,
    sign_csrf,
    sign_session,
    verify_csrf,
    verify_password,
    verify_session,
)


def test_hash_and_verify_password_roundtrip():
    phc = hash_password(b"hunter2")
    assert phc.startswith("$argon2id$")
    assert verify_password(b"hunter2", phc) is True
    assert verify_password(b"wrong", phc) is False


def test_verify_password_rejects_malformed_phc():
    assert verify_password(b"anything", "not a phc string") is False


def test_verify_password_rejects_oversized_password():
    assert verify_password(b"x" * 2000, hash_password(b"y")) is False


def test_session_roundtrip():
    key = os.urandom(32)
    jti = new_jti()
    token = sign_session(key, jti=jti, max_age_secs=3600)
    payload = verify_session(key, token)
    assert payload is not None
    assert payload.jti == jti


def test_session_rejects_tampered_signature():
    key = os.urandom(32)
    token = sign_session(key, jti=new_jti(), max_age_secs=3600)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verify_session(key, tampered) is None


def test_session_rejects_expired():
    key = os.urandom(32)
    token = sign_session(key, jti=new_jti(), max_age_secs=10, now=1000)
    assert verify_session(key, token, now=1011) is None


def test_session_rejects_wrong_key():
    key1, key2 = os.urandom(32), os.urandom(32)
    token = sign_session(key1, jti=new_jti(), max_age_secs=3600)
    assert verify_session(key2, token) is None


def test_session_rejects_garbage():
    key = os.urandom(32)
    assert verify_session(key, "not.a.token") is None
    assert verify_session(key, "") is None
    assert verify_session(key, "1.only-two-parts") is None


def test_csrf_roundtrip():
    key = os.urandom(32)
    jti = new_jti()
    token = sign_csrf(key, jti=jti)
    assert verify_csrf(key, token, jti=jti) is True


def test_csrf_rejects_different_session():
    key = os.urandom(32)
    token = sign_csrf(key, jti=new_jti())
    assert verify_csrf(key, token, jti=new_jti()) is False


def test_csrf_rejects_expired():
    key = os.urandom(32)
    jti = new_jti()
    token = sign_csrf(key, jti=jti, max_age_secs=10, now=1000)
    assert verify_csrf(key, token, jti=jti, now=1011) is False


def test_csrf_anon_jti_for_login_form():
    key = os.urandom(32)
    token = sign_csrf(key, jti=ANON_JTI, max_age_secs=600)
    assert verify_csrf(key, token, jti=ANON_JTI) is True
