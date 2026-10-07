"""Session creation and verification.

A session is a real credential, unlike the `X-User-Id` placeholder the rest
of the application still trusts. The raw token is a bearer credential: only
its hash is stored, exactly as a password's hash is stored rather than the
password itself, so a database read alone never hands out a working
session.

Not a JWT: sessions are created rarely (once per login, not once per
request), so the extra database lookup a server-side table costs is
negligible, and in exchange a session can be revoked by deleting one row,
with no key-rotation event affecting every other issued token.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from psycopg import Connection

from app.config import get_settings


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(conn: Connection, user_id: str) -> str:
    """Create a session for `user_id`. Returns the raw token — the only
    time it is ever available in cleartext; only its hash is stored.
    """
    settings = get_settings()
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.session_expiry_seconds)
    conn.execute(
        "insert into sessions (user_id, token_hash, expires_at) values (%s, %s, %s)",
        (user_id, _hash_token(raw_token), expires_at),
    )
    return raw_token


def verify_session(conn: Connection, raw_token: str) -> str | None:
    """Resolve a raw session token to a user id, or None if the token is
    unknown, malformed, or expired.
    """
    row = conn.execute(
        "select user_id from sessions where token_hash = %s and expires_at > now()",
        (_hash_token(raw_token),),
    ).fetchone()
    return str(row["user_id"]) if row else None


__all__ = ["create_session", "verify_session"]
