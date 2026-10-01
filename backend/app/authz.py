"""Minimal role lookup.

Sprint 4 (task 9.1) replaces this with the full (role, action) capability
matrix behind a shared dependency. Until real authentication exists, callers
pass a user id explicitly; the lookup itself is real, not stubbed, so upload
authorisation genuinely checks membership. Requirement 5.1.
"""

from __future__ import annotations

from psycopg import Connection


class NotAMemberError(Exception):
    """Raised when the caller holds no role on the target project."""


def role_on_project(conn: Connection, user_id: str, project_id: str) -> str | None:
    """The caller's role on a project, or None if they are not a member.

    A super admin who holds no explicit membership row is treated as
    'admin' on every project. Requirement 4.2.
    """
    row = conn.execute(
        "select is_super_admin from users where id = %s", (user_id,)
    ).fetchone()
    if row is None:
        return None
    if row["is_super_admin"]:
        return "admin"

    member = conn.execute(
        "select role from project_members where project_id = %s and user_id = %s",
        (project_id, user_id),
    ).fetchone()
    return member["role"] if member else None


def require_role(conn: Connection, user_id: str, project_id: str, *allowed: str) -> str:
    """Return the caller's role if it is one of `allowed`, else raise."""
    role = role_on_project(conn, user_id, project_id)
    if role is None or role not in allowed:
        raise NotAMemberError(f"user {user_id!r} lacks required role on project {project_id!r}")
    return role
