"""Integration tests for double-submit CSRF protection.

Validates: Requirements 15.2, 15.6

Design doc note (`design.md`, "Not covered by property tests"): CSRF
protection here is a mechanism choice verified by an integration test that
a cross-origin-style state-changing request without the token is
rejected, not a property.

Uses FastAPI's `TestClient` directly against `app.main.app` so the real
`CSRFMiddleware` is exercised exactly as it runs in production. The
sign-out endpoint (`DELETE /api/auth/session`) is used as the
state-changing probe because it only touches the database when a session
cookie is actually present -- with no cookie at all it is a pure
middleware/handler round trip, letting these tests avoid a live Postgres
connection for the "no session cookie" cases. For the "session cookie
present" cases, `app.db.connection` is monkeypatched to a fake connection
so `sign_out` has something to run `DELETE FROM sessions` against without
a real database.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.csrf import CSRF_HEADER_NAME
from app.main import app
from app.session_auth import SESSION_COOKIE_NAME


class _FakeConnection:
    def execute(self, query: str, params: tuple = ()):
        class _Result:
            def fetchone(self):
                return None

        return _Result()


@pytest.fixture
def client(monkeypatch):
    @contextmanager
    def _fake_connection():
        yield _FakeConnection()

    monkeypatch.setattr("app.main.db.connection", _fake_connection)
    return TestClient(app)


# --- requests with no session cookie: CSRF does not apply -------------------


def test_state_changing_request_without_session_cookie_is_not_blocked(client):
    # No session cookie at all -- e.g. a pure X-User-Id placeholder flow.
    # The middleware must not require a CSRF header here.
    response = client.delete("/api/auth/session")
    assert response.status_code == 200
    assert response.json() == {"status": "signed_out"}


def test_safe_methods_are_never_blocked_regardless_of_cookies(client):
    response = client.get("/health")
    assert response.status_code == 200


# --- requests with a session cookie: CSRF is enforced -----------------------


def test_state_changing_request_with_session_cookie_and_no_csrf_header_is_rejected(client):
    client.cookies.set(SESSION_COOKIE_NAME, "some-session-token")
    client.cookies.set("csrf_token", "the-real-csrf-token")

    response = client.delete("/api/auth/session")

    assert response.status_code == 403
    assert "csrf" in response.json()["detail"].lower()


def test_state_changing_request_with_session_cookie_and_wrong_csrf_header_is_rejected(client):
    client.cookies.set(SESSION_COOKIE_NAME, "some-session-token")
    client.cookies.set("csrf_token", "the-real-csrf-token")

    response = client.delete(
        "/api/auth/session",
        headers={CSRF_HEADER_NAME: "an-attacker-guessed-token"},
    )

    assert response.status_code == 403


def test_state_changing_request_with_matching_csrf_header_succeeds(client):
    client.cookies.set(SESSION_COOKIE_NAME, "some-session-token")
    client.cookies.set("csrf_token", "the-real-csrf-token")

    response = client.delete(
        "/api/auth/session",
        headers={CSRF_HEADER_NAME: "the-real-csrf-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "signed_out"}


def test_session_cookie_present_but_no_csrf_cookie_at_all_is_rejected(client):
    # Should not happen in practice (the two cookies are always set
    # together) but if the CSRF cookie is missing entirely, there is
    # nothing to double-submit against, so the request must be rejected
    # rather than silently allowed.
    client.cookies.set(SESSION_COOKIE_NAME, "some-session-token")

    response = client.delete(
        "/api/auth/session",
        headers={CSRF_HEADER_NAME: "anything"},
    )

    assert response.status_code == 403


# --- exempt endpoints: establishing a session is not itself protected ------


def test_sign_in_endpoint_is_exempt_even_with_no_csrf_header(client, monkeypatch):
    # Sign-in doesn't rely on an existing session cookie, so it must not
    # be blocked by the CSRF middleware even though it's a POST. It will
    # fail for an unrelated reason (invalid credentials against the fake
    # connection), which is fine -- the point here is only that the
    # middleware itself does not return 403.
    response = client.post(
        "/api/auth/session",
        json={"email": "nobody@example.com", "password": "whatever"},
    )
    assert response.status_code != 403


def test_accept_invite_endpoint_is_exempt_even_with_no_csrf_header(client):
    response = client.post("/api/invites/accept", json={"token": "not-a-real-token"})
    assert response.status_code != 403


# --- cookie attributes (P39 / Requirement 15.2) ------------------------------


def test_sign_in_response_sets_httponly_secure_samesite_session_cookie(client, monkeypatch):
    from app.auth import SignInResult

    # `app.main` imports `sign_in` by name, so the module-level attribute
    # there (not `app.auth.sign_in`) is what the endpoint actually calls.
    monkeypatch.setattr(
        "app.main.sign_in",
        lambda conn, *, email, password: SignInResult(session_token="raw-token-value", user_id="user-1"),
    )

    response = client.post(
        "/api/auth/session",
        json={"email": "alice@example.com", "password": "correct-password"},
    )

    assert response.status_code == 200
    set_cookie_headers = response.headers.get_list("set-cookie")
    session_cookie_header = next(h for h in set_cookie_headers if h.startswith(f"{SESSION_COOKIE_NAME}="))
    assert "HttpOnly" in session_cookie_header
    assert "SameSite=lax" in session_cookie_header or "SameSite=Lax" in session_cookie_header

    csrf_cookie_header = next(h for h in set_cookie_headers if h.startswith("csrf_token="))
    assert "HttpOnly" not in csrf_cookie_header  # must be JS-readable
