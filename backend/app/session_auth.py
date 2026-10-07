"""FastAPI dependency for session-based identity.

A parallel identity path to the existing `X-User-Id` placeholder, used only
by endpoints that need a real, verified caller — currently none of the
existing endpoints, only the project-invites feature's own. Migrating the
rest of the application to this is explicitly out of scope for that
feature (see its requirements document, Requirement 5.4).
"""

from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException
from typing import Annotated

from app import db
from app.sessions import verify_session

SESSION_COOKIE_NAME = "session_token"


def current_session_user(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> str:
    """Resolve the session cookie to a user id, or reject with 401.

    The cookie is HttpOnly, Secure, SameSite=Lax when set by the login/accept
    endpoints — this dependency only reads and verifies it, it does not set
    cookies itself.
    """
    if session_token is None:
        raise HTTPException(status_code=401, detail="not signed in")
    with db.connection() as conn:
        user_id = verify_session(conn, session_token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="session is invalid or expired")
    return user_id


CurrentSessionUser = Depends(current_session_user)


def resolve_caller_id(
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> str:
    """Requirement 7.1/7.7: a real session, when present, takes precedence
    over the `X-User-Id` placeholder; absent a session, behaviour is
    unchanged from before this feature existed.

    Unlike `current_session_user`, a missing or invalid session is not an
    error here — it just means fall back to the placeholder, since that is
    this helper's whole purpose: being a superset of today's behaviour, not
    a replacement for it.
    """
    if session_token is not None:
        with db.connection() as conn:
            user_id = verify_session(conn, session_token)
        if user_id is not None:
            return user_id
    return x_user_id


ResolvedCallerId = Depends(resolve_caller_id)

__all__ = [
    "SESSION_COOKIE_NAME",
    "current_session_user",
    "CurrentSessionUser",
    "resolve_caller_id",
    "ResolvedCallerId",
]
