"""Tests for sign-in and sign-out.

Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6

No real Postgres connection here, matching the rest of this test suite's
convention of exercising logic directly rather than through a live
database. `_FakeConnection` is a minimal stand-in that supports exactly the
`execute(...).fetchone()` and `execute(...)` shapes `app.auth` uses.
"""

from __future__ import annotations

import hashlib

import pytest

import app.auth as auth_module
from app.auth import (
    InvalidCredentialsError,
    INVALID_CREDENTIALS_MESSAGE,
    RATE_LIMITED_MESSAGE,
    TooManyAttemptsError,
    sign_in,
    sign_out,
)
from app.passwords import hash_password


class _FakeResult:
    def __init__(self, row: dict | None) -> None:
        self._row = row

    def fetchone(self) -> dict | None:
        return self._row


class _FakeConnection:
    """Backs `users` and `sessions` with plain Python dicts/lists, enough
    for `app.auth.sign_in`/`sign_out` to run against.
    """

    def __init__(self, users: list[dict]) -> None:
        self.users = users
        self.sessions: list[dict] = []

    def execute(self, query: str, params: tuple = ()) -> _FakeResult:
        normalized = " ".join(query.split())
        if normalized.startswith("select id, password_hash from users where email"):
            (email,) = params
            for user in self.users:
                if user["email"] == email:
                    return _FakeResult({"id": user["id"], "password_hash": user["password_hash"]})
            return _FakeResult(None)
        if normalized.startswith("insert into sessions"):
            user_id, token_hash, expires_at = params
            self.sessions.append({"user_id": user_id, "token_hash": token_hash, "expires_at": expires_at})
            return _FakeResult(None)
        if normalized.startswith("delete from sessions where token_hash"):
            (token_hash,) = params
            self.sessions = [s for s in self.sessions if s["token_hash"] != token_hash]
            return _FakeResult(None)
        raise AssertionError(f"unexpected query in fake connection: {query!r}")


@pytest.fixture
def registered_user() -> dict:
    return {"id": "user-1", "email": "alice@example.com", "password_hash": hash_password("correct-password")}


# --- unit tests -------------------------------------------------------------


def test_sign_in_with_correct_password_succeeds(registered_user):
    conn = _FakeConnection([registered_user])
    result = sign_in(conn, email="alice@example.com", password="correct-password")
    assert result.user_id == "user-1"
    assert result.session_token
    assert len(conn.sessions) == 1


def test_sign_in_creates_a_real_session_row(registered_user):
    conn = _FakeConnection([registered_user])
    result = sign_in(conn, email="alice@example.com", password="correct-password")
    stored_hash = hashlib.sha256(result.session_token.encode("utf-8")).hexdigest()
    assert conn.sessions[0]["token_hash"] == stored_hash
    assert conn.sessions[0]["user_id"] == "user-1"


def test_sign_in_with_wrong_password_is_rejected(registered_user):
    conn = _FakeConnection([registered_user])
    with pytest.raises(InvalidCredentialsError) as excinfo:
        sign_in(conn, email="alice@example.com", password="wrong-password")
    assert str(excinfo.value) == INVALID_CREDENTIALS_MESSAGE
    assert conn.sessions == []


def test_sign_in_with_unknown_email_is_rejected(registered_user):
    conn = _FakeConnection([registered_user])
    with pytest.raises(InvalidCredentialsError) as excinfo:
        sign_in(conn, email="nobody@example.com", password="whatever")
    assert str(excinfo.value) == INVALID_CREDENTIALS_MESSAGE
    assert conn.sessions == []


def test_unknown_email_and_wrong_password_raise_the_same_message(registered_user):
    conn = _FakeConnection([registered_user])
    messages = set()
    for email, password in [("nobody@example.com", "whatever"), ("alice@example.com", "wrong-password")]:
        with pytest.raises(InvalidCredentialsError) as excinfo:
            sign_in(conn, email=email, password=password)
        messages.add(str(excinfo.value))
    assert messages == {INVALID_CREDENTIALS_MESSAGE}


def test_sign_in_rejects_user_with_no_password_set():
    # An invited user who never accepted/set a password yet: Requirement
    # 1.3's uniform message still applies, not a different error.
    conn = _FakeConnection([{"id": "user-2", "email": "pending@example.com", "password_hash": None}])
    with pytest.raises(InvalidCredentialsError) as excinfo:
        sign_in(conn, email="pending@example.com", password="anything")
    assert str(excinfo.value) == INVALID_CREDENTIALS_MESSAGE


def test_sign_out_deletes_the_session_row(registered_user):
    conn = _FakeConnection([registered_user])
    result = sign_in(conn, email="alice@example.com", password="correct-password")
    assert len(conn.sessions) == 1

    sign_out(conn, raw_token=result.session_token)

    assert conn.sessions == []


def test_sign_out_with_unknown_token_does_not_raise(registered_user):
    conn = _FakeConnection([registered_user])
    sign_out(conn, raw_token="not-a-real-token")  # no error, nothing to delete
    assert conn.sessions == []


# --- rate limiting (Requirements 1.4, 15.4) ---------------------------------


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    """Each test gets its own limiter with a small, test-friendly
    threshold, so tests don't depend on the real configured default
    (5 / 15 minutes) and don't leak state between tests via the module
    level singleton.
    """
    from app.rate_limit import RateLimiter

    limiter = RateLimiter(max_attempts=3, window_seconds=900)
    monkeypatch.setattr(auth_module, "_sign_in_limiter", limiter)
    return limiter


def test_sign_in_is_rate_limited_after_threshold_failures(registered_user):
    conn = _FakeConnection([registered_user])
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            sign_in(conn, email="alice@example.com", password="wrong-password")

    with pytest.raises(TooManyAttemptsError) as excinfo:
        sign_in(conn, email="alice@example.com", password="wrong-password")
    assert str(excinfo.value) == RATE_LIMITED_MESSAGE
    assert conn.sessions == []


def test_rate_limit_triggers_the_same_way_for_a_nonexistent_email(registered_user):
    conn = _FakeConnection([registered_user])
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            sign_in(conn, email="nobody@example.com", password="whatever")

    with pytest.raises(TooManyAttemptsError):
        sign_in(conn, email="nobody@example.com", password="whatever")


def test_rate_limit_is_independent_per_email(registered_user):
    conn = _FakeConnection(
        [registered_user, {"id": "user-2", "email": "bob@example.com", "password_hash": hash_password("bob-password")}]
    )
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            sign_in(conn, email="alice@example.com", password="wrong-password")

    # alice is now blocked, but bob's own limiter window is untouched.
    with pytest.raises(TooManyAttemptsError):
        sign_in(conn, email="alice@example.com", password="wrong-password")

    result = sign_in(conn, email="bob@example.com", password="bob-password")
    assert result.user_id == "user-2"


def test_successful_sign_in_resets_the_rate_limit(registered_user):
    conn = _FakeConnection([registered_user])
    for _ in range(2):
        with pytest.raises(InvalidCredentialsError):
            sign_in(conn, email="alice@example.com", password="wrong-password")

    # One failure below the threshold of 3, then a success.
    result = sign_in(conn, email="alice@example.com", password="correct-password")
    assert result.user_id == "user-1"

    # The window was reset by the success, so three more failures are
    # needed again before the limiter trips.
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            sign_in(conn, email="alice@example.com", password="wrong-password")
    with pytest.raises(TooManyAttemptsError):
        sign_in(conn, email="alice@example.com", password="wrong-password")


def test_rate_limit_check_happens_before_the_dummy_hash_verify(registered_user, monkeypatch):
    # Once blocked, sign_in must not call verify_password at all -- no
    # need to pay the Argon2 cost when already rejecting for rate-limit
    # reasons.
    conn = _FakeConnection([registered_user])
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            sign_in(conn, email="alice@example.com", password="wrong-password")

    calls = []
    monkeypatch.setattr(
        auth_module, "verify_password", lambda password, hash_: calls.append((password, hash_)) or False
    )
    with pytest.raises(TooManyAttemptsError):
        sign_in(conn, email="alice@example.com", password="whatever")
    assert calls == []
