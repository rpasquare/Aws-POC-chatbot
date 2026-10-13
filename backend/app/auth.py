"""Sign-in and sign-out.

`POST /api/auth/session` and `DELETE /api/auth/session` — Requirement 1
(1.1, 1.2, 1.3, 1.5, 1.6) and Requirement 15.5 (uniform failure).

Deliberately separate from `app.session_auth` (which only *reads* an
existing session) and from `app.invite_acceptance` (which issues a session
as a side effect of a token, not a password). This module is the one place
a password is actually checked against a stored hash for an ordinary
sign-in.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from psycopg import Connection

from app.config import get_settings
from app.passwords import verify_password
from app.rate_limit import RateLimiter
from app.sessions import create_session

# Requirement 1.4/15.4: one limiter instance for the process, keyed by the
# submitted email string. See `app.rate_limit`'s docstring for the
# single-process limitation. Settings are read lazily inside `sign_in` (not
# at import time) so tests and startup order don't depend on
# `get_settings()` having run yet.
_sign_in_limiter: RateLimiter | None = None


def _get_sign_in_limiter() -> RateLimiter:
    global _sign_in_limiter
    if _sign_in_limiter is None:
        settings = get_settings()
        _sign_in_limiter = RateLimiter(
            max_attempts=settings.sign_in_rate_limit_max_attempts,
            window_seconds=settings.sign_in_rate_limit_window_seconds,
        )
    return _sign_in_limiter

# Requirement 1.3/15.5: one message, regardless of which half of the
# credential was wrong, so a failure never discloses whether the email is
# registered.
INVALID_CREDENTIALS_MESSAGE = "incorrect email or password"

# A real Argon2id hash of an arbitrary, fixed password that corresponds to
# no account. When the email lookup misses, we still run a full verify
# against this hash so the time spent is comparable to the real-account
# path (which always does exactly one verify) rather than returning
# immediately -- Requirement 1.3/15.5's "same amount of time" clause.
# Generated once; never changes.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "6YQJiAfXrE7l0eCqZ2qfkA$kqX2OqfhXz0q8m8sWJYQ9f3h0b1k0G1b2d1Y3b2d1Y0"
)


class InvalidCredentialsError(Exception):
    """Unknown email or wrong password. Deliberately a single error type
    carrying the single uniform message -- callers must not branch on
    which case occurred.
    """

    def __init__(self) -> None:
        super().__init__(INVALID_CREDENTIALS_MESSAGE)


RATE_LIMITED_MESSAGE = "too many sign-in attempts; try again later"


class TooManyAttemptsError(Exception):
    """Requirement 1.4/15.4: the submitted email has exceeded its failed
    attempt allotment for the current window. Raised before any
    credential check runs, independent of whether the email exists.
    """

    def __init__(self) -> None:
        super().__init__(RATE_LIMITED_MESSAGE)


@dataclass(frozen=True)
class SignInResult:
    session_token: str
    user_id: str


def sign_in(conn: Connection, *, email: str, password: str) -> SignInResult:
    """Verify an email/password pair and issue a session on success.

    Requirement 1.4/15.4: the submitted email is checked against the
    sign-in rate limiter before anything else -- before the user lookup
    and before any password verify -- keyed purely on the email string as
    submitted. This makes the limiter trigger identically for a real
    email under attack and for an attacker enumerating random addresses;
    neither the limiter check nor its 429 response depends on whether the
    account exists. Rejecting here is intentionally fast (no Argon2 verify
    is run), which is a visible, expected signal ("you are rate limited"),
    not a secret one -- it doesn't leak anything about account existence
    the way response time on the credential path must not (Requirement
    15.5, handled separately below).

    Requirement 1.2: on success, creates a server-side session row,
    resets the limiter for this email, and returns the raw token for the
    caller to set as a cookie.
    Requirement 1.3/15.5: on credential failure -- unknown email or wrong
    password -- raises the same `InvalidCredentialsError` either way, and
    performs a dummy password verify in the unknown-email case so both
    paths do one Argon2 verify and take comparable time. Each credential
    failure also counts against the limiter for next time.
    """
    limiter = _get_sign_in_limiter()
    if limiter.is_blocked(email):
        raise TooManyAttemptsError()

    user = conn.execute(
        "select id, password_hash from users where email = %s",
        (email,),
    ).fetchone()

    if user is None or user["password_hash"] is None:
        # Requirement 15.5: no account (or no password set yet, e.g. an
        # invite never accepted) -- still spend the time a real check
        # would, so response time does not reveal which case this is.
        verify_password(password, _DUMMY_HASH)
        limiter.record_failure(email)
        raise InvalidCredentialsError()

    if not verify_password(password, user["password_hash"]):
        limiter.record_failure(email)
        raise InvalidCredentialsError()

    user_id = str(user["id"])
    session_token = create_session(conn, user_id)
    limiter.reset(email)
    return SignInResult(session_token=session_token, user_id=user_id)


def sign_out(conn: Connection, *, raw_token: str) -> None:
    """Delete the session row for `raw_token`, if any.

    Requirement 1.5: signing out deletes the session server-side rather
    than only clearing the cookie, so a stolen cookie value cannot be
    replayed after sign-out. Deleting a token that doesn't exist (already
    expired, already signed out elsewhere) is not an error -- the caller
    is signed out either way.
    """
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    conn.execute("delete from sessions where token_hash = %s", (token_hash,))


__all__ = [
    "InvalidCredentialsError",
    "INVALID_CREDENTIALS_MESSAGE",
    "TooManyAttemptsError",
    "RATE_LIMITED_MESSAGE",
    "SignInResult",
    "sign_in",
    "sign_out",
]
