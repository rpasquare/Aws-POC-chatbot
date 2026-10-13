"""Accepting an invite.

`POST /api/invites/accept` — Requirements 2.1, 2.2, 2.3, 2.4, 3.1, 3.3, 4.4
(project-invites spec).

Deliberately separate from `app.invites`: sending and managing invites
requires an authenticated admin; accepting one does not require
authentication at all, since the token itself is the credential being
presented. Keeping the two in separate modules keeps that trust boundary
visible rather than implicit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from psycopg import Connection

from app.passwords import hash_password
from app.sessions import create_session


class InviteNotValidError(Exception):
    """The token does not correspond to an acceptable invite: unknown,
    cancelled, or already accepted. Requirement 4.4: these are deliberately
    not distinguished in the raised error, so the response to the caller
    does not leak which case occurred.
    """


class PasswordRequiredError(Exception):
    """A first-time recipient must set a password to accept."""


@dataclass(frozen=True)
class AcceptanceResult:
    session_token: str
    project_id: str
    first_time: bool


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def accept_invite(conn: Connection, *, raw_token: str, password: str | None) -> AcceptanceResult:
    """Accept an invite by its raw token.

    Requirement 2.1/2.2 (first-time): if the invited email has no
    password set yet, `password` is required and is hashed and stored.
    Requirement 3.1/3.3 (returning): if the email already has a password,
    any supplied password is ignored — opening the valid link is itself
    the credential for this path.

    Requirement 2.4/3.3: the invite is marked accepted in the same
    transaction as the membership is created and the session issued, so a
    token can never be used a second time to gain anything further.
    """
    invite = conn.execute(
        "select id, project_id, email, role, status, invited_by from invites where token_hash = %s",
        (_hash_token(raw_token),),
    ).fetchone()
    if invite is None or invite["status"] != "pending":
        raise InviteNotValidError("this invite link is no longer valid")

    user = conn.execute(
        "select id, password_hash from users where email = %s",
        (invite["email"],),
    ).fetchone()

    first_time = user is None or user["password_hash"] is None

    if first_time:
        if not password:
            raise PasswordRequiredError("a password is required to accept this invite")
        hashed = hash_password(password)
        if user is None:
            user_row = conn.execute(
                "insert into users (email, external_id, password_hash) "
                "values (%s, %s, %s) returning id",
                (invite["email"], invite["email"], hashed),
            ).fetchone()
            user_id = str(user_row["id"])
        else:
            user_id = str(user["id"])
            conn.execute("update users set password_hash = %s where id = %s", (hashed, user_id))
    else:
        # Returning recipient: the link itself is the credential. Any
        # supplied password is ignored rather than checked (Requirement 3.1).
        user_id = str(user["id"])

    conn.execute(
        "insert into project_members (project_id, user_id, role, added_by) "
        "values (%s, %s, %s, %s) "
        "on conflict (project_id, user_id) do nothing",
        (invite["project_id"], user_id, invite["role"], invite["invited_by"]),
    )

    conn.execute(
        "update invites set status = 'accepted', accepted_at = now() where id = %s",
        (invite["id"],),
    )

    session_token = create_session(conn, user_id)
    return AcceptanceResult(
        session_token=session_token,
        project_id=str(invite["project_id"]),
        first_time=first_time,
    )


__all__ = ["InviteNotValidError", "PasswordRequiredError", "AcceptanceResult", "accept_invite"]
