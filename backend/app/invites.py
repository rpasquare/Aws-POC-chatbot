"""Project invites: sending, listing, and cancelling.

`POST /api/projects/{id}/invites` — admin-only, Requirements 1.1, 1.2, 1.4,
1.5 (project-invites spec).
`GET /api/projects/{id}/invites` — admin-only, Requirement 4.2.
`POST /api/projects/{id}/invites/{invite_id}/cancel` — admin-only,
Requirement 4.3.

Accepting an invite lives in `app.invite_acceptance`, not here: sending and
managing invites requires an authenticated admin; accepting one does not
require authentication at all (the token itself is the credential), so the
two are kept in separate modules with different trust boundaries.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from psycopg import Connection

from app.authz import NotAMemberError, require_role
from app.config import get_settings
from app.email import send_invite_email


class InviteRejectedError(ValueError):
    """The invite could not be created as requested."""


@dataclass(frozen=True)
class InviteSlot:
    invite_id: str
    raw_token: str


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def send_invite(
    conn: Connection,
    *,
    user_id: str,
    project_id: str,
    email: str,
    role: str,
) -> InviteSlot:
    """Create a pending invite. Requirement 1.1: admin-only, enforced before
    anything else. Requirement 1.4: reject an email that already holds a
    role on the project. Requirement 1.5: reject a duplicate pending invite
    for the same (project, email) pair, enforced by the partial unique
    index at the database layer; this function checks it explicitly first
    so the rejection message is specific rather than a raw constraint error.
    """
    require_role(conn, user_id, project_id, "admin")

    if role not in ("editor", "viewer"):
        raise InviteRejectedError(f"role must be 'editor' or 'viewer', got {role!r}")

    existing_member = conn.execute(
        "select pm.role from project_members pm "
        "join users u on u.id = pm.user_id "
        "where pm.project_id = %s and u.email = %s",
        (project_id, email),
    ).fetchone()
    if existing_member is not None:
        raise InviteRejectedError(f"{email!r} is already a member of this project")

    existing_pending = conn.execute(
        "select id from invites where project_id = %s and email = %s and status = 'pending'",
        (project_id, email),
    ).fetchone()
    if existing_pending is not None:
        raise InviteRejectedError(f"an invite is already pending for {email!r} on this project")

    raw_token = secrets.token_urlsafe(32)
    row = conn.execute(
        "insert into invites (project_id, email, role, invited_by, status, token_hash) "
        "values (%s, %s, %s, %s, 'pending', %s) "
        "returning id",
        (project_id, email, role, user_id, _hash_token(raw_token)),
    ).fetchone()
    invite_id = str(row["id"])

    project = conn.execute("select name from projects where id = %s", (project_id,)).fetchone()
    inviter = conn.execute("select email from users where id = %s", (user_id,)).fetchone()
    settings = get_settings()
    accept_url = f"{settings.frontend_base_url}/accept-invite?token={raw_token}"
    try:
        send_invite_email(
            to=email,
            project_name=project["name"],
            inviter_email=inviter["email"],
            role=role,
            accept_url=accept_url,
        )
    except Exception as exc:  # noqa: BLE001 - any send failure means the invite does not exist
        # The invite row created above is rolled back along with this
        # exception, by the caller's transaction: email success is
        # required for the invite to exist at all, so an admin never sees
        # "invited successfully" when the recipient will never actually
        # get the email. Re-raised as a clear, specific error rather than
        # left as a raw SMTP/boto3 exception reaching the HTTP layer.
        raise InviteRejectedError(f"could not send the invite email to {email!r}: {exc}") from exc

    return InviteSlot(invite_id=invite_id, raw_token=raw_token)


def list_pending_invites(conn: Connection, *, user_id: str, project_id: str) -> list[dict]:
    """Requirement 4.2: pending invites only, not accepted or cancelled."""
    require_role(conn, user_id, project_id, "admin")
    rows = conn.execute(
        "select i.id, i.email, i.role, i.invited_by, i.created_at, u.email as invited_by_email "
        "from invites i "
        "join users u on u.id = i.invited_by "
        "where i.project_id = %s and i.status = 'pending' "
        "order by i.created_at desc",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def cancel_invite(conn: Connection, *, user_id: str, project_id: str, invite_id: str) -> None:
    """Requirement 4.3: cancelling a pending invite invalidates its link
    immediately. Cancelling an invite that is already cancelled or already
    accepted is a no-op, not an error — the end state the caller wants (this
    invite is not active) is already true.
    """
    require_role(conn, user_id, project_id, "admin")
    conn.execute(
        "update invites set status = 'cancelled', cancelled_at = now() "
        "where id = %s and project_id = %s and status = 'pending'",
        (invite_id, project_id),
    )


__all__ = [
    "InviteRejectedError",
    "InviteSlot",
    "send_invite",
    "list_pending_invites",
    "cancel_invite",
    "NotAMemberError",
]
