"""Double-submit CSRF protection for cookie-based sessions.

Requirement 15.6: `SameSite=Lax` on the session cookie is a partial
mitigation (it blocks simple cross-site GETs and most cross-site form
posts) but not a complete one -- top-level GET navigations still attach
the cookie, and historically not every browser has applied `Lax`
consistently to every method. A state-changing request that relies on the
session cookie needs an independent check that an attacker's page, running
on another origin, cannot satisfy.

Double-submit works like this: whenever the server sets the session
cookie (sign-in, invite acceptance), it also sets a second cookie holding a
random token. That second cookie is deliberately *not* `HttpOnly`, so the
frontend's own JavaScript can read it and echo it back as a custom header
on every state-changing request. A cross-origin attacker page can make the
browser attach cookies automatically, but it cannot read this cookie's
value (the browser's same-origin policy blocks that), so it cannot produce
a matching header. The server then just has to check the header equals the
cookie.

This module is wired in as ASGI middleware (`CSRFMiddleware`) rather than a
per-route dependency, so every current and future state-changing endpoint
under `/api/` is covered without remembering to add a dependency to each
one. Two (method, path) pairs are deliberately exempt because they
*establish* a session rather than rely on an existing one: `POST
/api/auth/session` (sign-in) and `POST /api/invites/accept` (invite
acceptance). There is no prior session to protect at that point, and the
asymmetry is intentional (see Requirement 15.6's note in the design doc on
why sign-in itself is out of scope for CSRF). Note `DELETE
/api/auth/session` (sign-out) shares the same path as sign-in but a
different method, and sign-out *does* rely on an existing session cookie,
so the exemption is keyed on (method, path), not path alone.

The check only applies when a session cookie is actually present. A
request using only the `X-User-Id` placeholder (no session cookie) has no
cookie-based session for an attacker to ride, so there is nothing for CSRF
to protect there -- requiring the header in that case would just break the
placeholder flow for no security benefit.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings
from app.session_auth import SESSION_COOKIE_NAME

CSRF_HEADER_NAME = "X-CSRF-Token"

# Methods that mutate state. GET/HEAD/OPTIONS never need a CSRF check --
# they shouldn't mutate anything, and that's exactly what SameSite=Lax
# already fails to fully prevent for navigations, so this check is scoped
# to the methods that actually matter.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Endpoints that set the session cookie for the first time. They have no
# prior session to forge -- the attacker gains nothing by triggering one of
# these on a victim's behalf other than making the victim sign into a
# session the attacker doesn't control, which is a different and
# lower-severity concern than classic CSRF, and out of scope here.
#
# Keyed by (method, path): `/api/auth/session` is also the sign-*out*
# endpoint under DELETE, which *does* rely on an existing session cookie
# and must remain protected, so the exemption cannot be path-only.
EXEMPT_METHOD_PATHS = frozenset(
    {
        ("POST", "/api/auth/session"),
        ("POST", "/api/invites/accept"),
    }
)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    """Set the CSRF cookie alongside the session cookie. Deliberately
    *not* HttpOnly -- the frontend must be able to read this value with
    JavaScript to echo it back as a header; its secrecy from the attacker
    comes from same-origin policy, not from being hidden from the
    legitimate frontend.
    """
    settings = get_settings()
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_expiry_seconds,
    )


class CSRFMiddleware(BaseHTTPMiddleware):
    """Rejects state-changing requests that rely on the session cookie but
    carry no matching CSRF header, with a 403.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in _SAFE_METHODS or (request.method, request.url.path) in EXEMPT_METHOD_PATHS:
            return await call_next(request)

        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        if session_token is None:
            # No cookie-based session is in play -- e.g. a pure
            # X-User-Id placeholder request. Nothing for CSRF to protect.
            return await call_next(request)

        settings = get_settings()
        cookie_value = request.cookies.get(settings.csrf_cookie_name)
        header_value = request.headers.get(CSRF_HEADER_NAME)
        if not cookie_value or not header_value or not secrets.compare_digest(cookie_value, header_value):
            return JSONResponse(status_code=403, content={"detail": "missing or invalid CSRF token"})

        return await call_next(request)


__all__ = [
    "CSRF_HEADER_NAME",
    "EXEMPT_METHOD_PATHS",
    "CSRFMiddleware",
    "generate_csrf_token",
    "set_csrf_cookie",
]
