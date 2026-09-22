"""Session cookie signing, Argon2id password hashing, and CSRF tokens.

No new dependency for any of this:

- Argon2id comes from `cryptography`, already a dependency and already used
  by `crypto/keyring.py` for the master key passphrase. `derive_phc_encoded`
  / `verify_phc_encoded` produce and check standard `$argon2id$...` strings.
- The session cookie is a signed dict, hand-rolled with stdlib `hmac` +
  `base64` + `json`. Starlette's `SessionMiddleware` (which needs
  `itsdangerous`) is more machinery than "is this person logged in".

One HMAC key signs both session and CSRF tokens; a domain-separation label
per token type (`rv_session.v1` / `rv_csrf.v1`) keeps them from being
confused for one another.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

# Same parameters as crypto/keyring.py's master-key KDF: a good login cost
# without new tuning.
ARGON2_ITERATIONS = 3
ARGON2_MEMORY_COST_KIB = 65536
ARGON2_LANES = 4
ARGON2_KEY_LENGTH = 32

_MAX_PASSWORD_BYTES = 1024

_SESSION_LABEL = b"rv_session.v1"
_CSRF_LABEL = b"rv_csrf.v1"

ANON_JTI = "anon"  # binds the login form's own CSRF token; no session exists yet.


def hash_password(password: bytes) -> str:
    """Returns a `$argon2id$...` PHC string, safe to store in a plain file."""
    salt = os.urandom(16)
    kdf = Argon2id(
        salt=salt,
        length=ARGON2_KEY_LENGTH,
        iterations=ARGON2_ITERATIONS,
        lanes=ARGON2_LANES,
        memory_cost=ARGON2_MEMORY_COST_KIB,
    )
    return kdf.derive_phc_encoded(password)


def verify_password(password: bytes, phc_encoded: str) -> bool:
    if len(password) > _MAX_PASSWORD_BYTES:
        return False
    try:
        Argon2id.verify_phc_encoded(password, phc_encoded)
    except InvalidKey:
        return False
    except ValueError:
        # Malformed PHC string (e.g. a hand-edited password file).
        return False
    return True


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes | None:
    try:
        padding = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + padding)
    except (ValueError, TypeError):
        return None


def _mac(key: bytes, label: bytes, payload: bytes) -> bytes:
    return hmac.new(key, label + b"|" + payload, hashlib.sha256).digest()


def new_jti() -> str:
    return os.urandom(16).hex()


@dataclass(frozen=True, slots=True)
class SessionPayload:
    jti: str
    iat: int
    exp: int


def sign_session(key: bytes, *, jti: str, max_age_secs: int, now: int | None = None) -> str:
    now = now if now is not None else int(time.time())
    payload = {"jti": jti, "iat": now, "exp": now + max_age_secs}
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = _mac(key, _SESSION_LABEL, payload_b64.encode())
    return f"1.{payload_b64}.{_b64url_encode(sig)}"


def verify_session(key: bytes, token: str, *, now: int | None = None) -> SessionPayload | None:
    """Constant-time signature check first, then expiry. Any malformed or
    tampered input returns `None`, never raises: this runs on every request
    in middleware, and the caller's only decision is "authenticated or not"."""
    now = now if now is not None else int(time.time())
    parts = token.split(".", 2)
    if len(parts) != 3 or parts[0] != "1":
        return None
    _, payload_b64, sig_b64 = parts
    given_sig = _b64url_decode(sig_b64)
    if given_sig is None:
        return None
    expected_sig = _mac(key, _SESSION_LABEL, payload_b64.encode())
    if not hmac.compare_digest(expected_sig, given_sig):
        return None
    raw_payload = _b64url_decode(payload_b64)
    if raw_payload is None:
        return None
    try:
        payload = json.loads(raw_payload)
        jti = str(payload["jti"])
        iat = int(payload["iat"])
        exp = int(payload["exp"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if iat > now + 60:  # clock skew tolerance, not a validity window
        return None
    if exp < now:
        return None
    return SessionPayload(jti=jti, iat=iat, exp=exp)


def sign_csrf(key: bytes, *, jti: str, max_age_secs: int = 600, now: int | None = None) -> str:
    now = now if now is not None else int(time.time())
    nonce = _b64url_encode(os.urandom(16))
    payload = f"{jti}|{now + max_age_secs}|{nonce}"
    payload_b64 = _b64url_encode(payload.encode())
    sig = _mac(key, _CSRF_LABEL, payload_b64.encode())
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_csrf(key: bytes, token: str, *, jti: str, now: int | None = None) -> bool:
    """Binds the token to a specific session's `jti`, so a token minted for
    one session is rejected for another."""
    now = now if now is not None else int(time.time())
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    payload_b64, sig_b64 = parts
    given_sig = _b64url_decode(sig_b64)
    if given_sig is None:
        return False
    expected_sig = _mac(key, _CSRF_LABEL, payload_b64.encode())
    if not hmac.compare_digest(expected_sig, given_sig):
        return False
    raw_payload = _b64url_decode(payload_b64)
    if raw_payload is None:
        return False
    try:
        token_jti, exp_str, _nonce = raw_payload.decode("utf-8").split("|", 2)
        exp = int(exp_str)
    except (UnicodeDecodeError, ValueError):
        return False
    if not hmac.compare_digest(token_jti, jti):
        return False
    return exp >= now
