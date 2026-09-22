"""In-process login rate limiter. No dependency: a sliding window of
failure timestamps per key, under a lock (routes are sync and dispatched to
a threadpool, so this is genuinely concurrent).

Two keys are checked on every attempt: the caller's own key (normally the
client IP) and a shared global key. The global bucket matters on its own:
if `forwarded_allow_ips` is misconfigured behind a reverse proxy, every
request collapses onto the proxy's container IP and the per-IP bucket
silently becomes a single shared bucket anyway. Making that bucket explicit
means it still limits correctly instead of quietly not limiting at all.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

_GLOBAL_KEY = "__global__"


class LoginLimiter:
    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_secs: float = 900.0,
        lockout_secs: float = 300.0,
        global_max_attempts: int | None = None,
        max_keys: int = 1024,
    ) -> None:
        self._max_attempts = max_attempts
        # A separate, looser threshold for the shared bucket: it exists for
        # the degraded case where a misconfigured `forwarded_allow_ips`
        # collapses every request onto one proxy IP, not to double-punish a
        # single real user's own retries. Clearing a key's own bucket on
        # success must be enough to let that user straight back in.
        self._global_max_attempts = (
            global_max_attempts if global_max_attempts is not None else max_attempts * 4
        )
        self._window_secs = window_secs
        self._lockout_secs = lockout_secs
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, list[float]] = OrderedDict()

    def retry_after(self, key: str) -> float | None:
        """Seconds until the next attempt is allowed, or `None` if it's
        allowed now. Checks both the caller's own bucket and the global
        one."""
        now = time.time()
        with self._lock:
            wait = self._retry_after_locked(key, now, self._max_attempts)
            if wait is not None:
                return wait
            return self._retry_after_locked(_GLOBAL_KEY, now, self._global_max_attempts)

    def _retry_after_locked(self, key: str, now: float, threshold: int) -> float | None:
        attempts = [t for t in self._failures.get(key, []) if now - t < self._window_secs]
        self._failures[key] = attempts
        if len(attempts) < threshold:
            return None
        # Escalates every additional block of `threshold` failures, capped
        # so a very old, very hammered key can't lock out forever.
        excess_blocks = (len(attempts) - threshold) // threshold
        lockout = min(self._lockout_secs * (2**excess_blocks), self._window_secs * 4)
        remaining = lockout - (now - attempts[-1])
        return remaining if remaining > 0 else None

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            for k in (key, _GLOBAL_KEY):
                self._failures.setdefault(k, []).append(now)
                self._failures.move_to_end(k)
            while len(self._failures) > self._max_keys:
                self._failures.popitem(last=False)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
