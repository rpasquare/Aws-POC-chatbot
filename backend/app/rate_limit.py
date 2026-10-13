"""A minimal in-memory rate limiter, keyed by an arbitrary identity string.

Used by `app.auth.sign_in` to cap repeated failed attempts for one email
(Requirements 1.4, 15.4). Deliberately simple: a fixed window per key,
counting failures only, reset on success.

**Limitation**: this state lives in process memory. It is correct for a
single API process (true today -- see `backend/run_api.sh` and the compose
setup, there is no Redis or other shared cache layer) but will NOT work
correctly if the API is ever scaled to multiple processes or instances,
since each process would track its own counters and an attacker could get
a full allotment of attempts per process. If/when the API is scaled
horizontally, this needs to move to a shared store (e.g. a Postgres table
with upsert-and-count, since Postgres is already the one shared store this
application uses) before it provides any real protection.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Window:
    count: int = 0
    window_started_at: float = field(default_factory=time.monotonic)


class RateLimiter:
    """Fixed-window limiter: at most `max_attempts` failures per `key`
    within `window_seconds`, after which further checks are rejected until
    the window rolls over. A successful attempt clears the key's window
    immediately rather than waiting for it to expire.
    """

    def __init__(self, *, max_attempts: int, window_seconds: float) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        """True if `key` has already exhausted its attempts for the
        current window. Does not itself count as an attempt -- call this
        before doing any credential work, and call `record_failure` only
        after a genuine failed attempt.
        """
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                return False
            if self._window_expired(window):
                del self._windows[key]
                return False
            return window.count >= self._max_attempts

    def record_failure(self, key: str) -> None:
        """Count one failed attempt for `key`."""
        with self._lock:
            window = self._windows.get(key)
            if window is None or self._window_expired(window):
                window = _Window()
                self._windows[key] = window
            window.count += 1

    def reset(self, key: str) -> None:
        """Clear `key`'s window entirely, e.g. after a successful sign-in."""
        with self._lock:
            self._windows.pop(key, None)

    def _window_expired(self, window: _Window) -> bool:
        return time.monotonic() - window.window_started_at >= self._window_seconds


__all__ = ["RateLimiter"]
