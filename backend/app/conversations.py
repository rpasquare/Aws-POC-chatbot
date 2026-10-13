"""Conversation and message data access, role-gated.

Named `conversations` rather than `sessions` deliberately — "session" is
already taken by `app.sessions` (real auth sessions). The table backing
this module is still `chat_sessions` per `migrations/0005_conversations.sql`;
that SQL name is historical and intentionally not renamed (design doc,
"API surface" revision note). "Conversation" is the word used at the
application/API layer only.

Three operations, matching Requirement 9:

  - `create_conversation`: admin or editor only (Requirement 9.1, 9.2).
  - `list_conversations`: any project member, ordered by `last_active`
    descending (Requirement 9.3, 9.4).
  - `list_messages`: any member of the conversation's project, with each
    message's citations attached (Requirement 9.5).

Role checks reuse `app.authz.require_role`/`NotAMemberError`, the same
helper every other role-gated module (`app.invites`, `app.uploads`) uses.
`NotAMemberError` is intentionally overloaded here the same way it already
is elsewhere: it covers both "not a member at all" and "a member, but not
one of the allowed roles" — the HTTP layer (`app/main.py`) is what turns
the first into a 404 and the second into a 403, per the design doc's
error-handling table ("Project not a member of" -> 404; "Role
insufficient" -> 403). This module itself does not need to distinguish
the two cases to do its job correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import Connection

from app.authz import NotAMemberError, require_role

__all__ = [
    "ConversationNotFoundError",
    "MessageNotFoundError",
    "ModelLog",
    "create_conversation",
    "get_message_model_log",
    "list_conversations",
    "list_messages",
]

_DEFAULT_TITLE = "New conversation"


class MessageNotFoundError(Exception):
    """Raised when a message id does not exist, or exists but has no
    model log row (e.g. a refusal message, Requirement 16.20) --
    `app/main.py` maps both to 404, the same "respond as though it does
    not exist" treatment `ConversationNotFoundError` gets.
    """


class ConversationNotFoundError(Exception):
    """Raised when the conversation id does not exist at all.

    Distinct from `NotAMemberError`: this means the row itself is missing,
    not that the caller lacks a role on its project. `app/main.py` maps
    both to 404 (Requirement 2.2's "respond as though it does not exist"
    pattern applies to a non-member hitting a real conversation id just as
    much as to a non-existent one), but keeping the error types separate
    here means that mapping is a deliberate choice at the HTTP boundary,
    not something baked into this module.
    """


@dataclass(frozen=True)
class Conversation:
    id: str
    project_id: str
    title: str
    created_by: str
    created_at: object
    last_active: object


@dataclass(frozen=True)
class Message:
    id: str
    session_id: str
    role: str
    text: str
    author_id: str | None
    grounded: bool
    created_at: object
    citations: list[dict]
    has_model_log: bool = False


def create_conversation(
    conn: Connection,
    *,
    user_id: str,
    project_id: str,
    title: str | None = None,
) -> Conversation:
    """Create a new, independent conversation in a project.

    Requirement 9.1/9.2: admin or editor only. A viewer's attempt raises
    `NotAMemberError` and creates no row — the role check happens before
    any insert.

    `title` defaults to a fixed placeholder. Deriving a title from the
    first question is task 10.4, explicitly later; this function never
    attempts it.
    """
    require_role(conn, user_id, project_id, "admin", "editor")

    row = conn.execute(
        "insert into chat_sessions (project_id, title, created_by) "
        "values (%s, %s, %s) "
        "returning id, project_id, title, created_by, created_at, last_active",
        (project_id, title or _DEFAULT_TITLE, user_id),
    ).fetchone()
    return Conversation(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        title=row["title"],
        created_by=str(row["created_by"]),
        created_at=row["created_at"],
        last_active=row["last_active"],
    )


def list_conversations(conn: Connection, *, user_id: str, project_id: str) -> list[Conversation]:
    """List a project's conversations, most recently active first.

    Requirement 9.3/9.4: any member (admin, editor or viewer) may read.
    """
    require_role(conn, user_id, project_id, "admin", "editor", "viewer")

    rows = conn.execute(
        "select id, project_id, title, created_by, created_at, last_active "
        "from chat_sessions "
        "where project_id = %s "
        "order by last_active desc",
        (project_id,),
    ).fetchall()
    return [
        Conversation(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            title=row["title"],
            created_by=str(row["created_by"]),
            created_at=row["created_at"],
            last_active=row["last_active"],
        )
        for row in rows
    ]


def _resolve_conversation_project(conn: Connection, *, conversation_id: str) -> str:
    """Look up a conversation's `project_id`, raising if it doesn't exist."""
    row = conn.execute(
        "select project_id from chat_sessions where id = %s",
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
    return str(row["project_id"])


@dataclass(frozen=True)
class ModelLog:
    message_id: str
    request_text: str
    response_text: str


def get_message_model_log(conn: Connection, *, user_id: str, message_id: str) -> ModelLog:
    """Fetch one message's audit log (Requirement 16.18).

    Resolves the message's conversation and project, then requires the
    caller hold any role (admin, editor or viewer) on that project --
    same gating as reading the conversation itself (Requirement 9.3/9.5).
    Raises `MessageNotFoundError` if the message does not exist or has no
    log row (a refusal message never has one, Requirement 16.20);
    `NotAMemberError` if the caller is not a member of its project.
    `app/main.py` maps both to 404.
    """
    row = conn.execute(
        "select cs.project_id "
        "from chat_messages cm "
        "join chat_sessions cs on cs.id = cm.session_id "
        "where cm.id = %s",
        (message_id,),
    ).fetchone()
    if row is None:
        raise MessageNotFoundError(f"message {message_id!r} does not exist")
    project_id = str(row["project_id"])

    require_role(conn, user_id, project_id, "admin", "editor", "viewer")

    log_row = conn.execute(
        "select message_id, request_text, response_text from message_model_logs where message_id = %s",
        (message_id,),
    ).fetchone()
    if log_row is None:
        raise MessageNotFoundError(f"message {message_id!r} has no model log")

    return ModelLog(
        message_id=str(log_row["message_id"]),
        request_text=log_row["request_text"],
        response_text=log_row["response_text"],
    )


def list_messages(conn: Connection, *, user_id: str, conversation_id: str) -> list[Message]:
    """List a conversation's messages, in order, with citations attached.

    Resolves the conversation's project first, then requires the caller
    hold any role (admin, editor or viewer) on that project — Requirement
    9.3/9.5. Raises `ConversationNotFoundError` if the conversation id
    does not exist, or `NotAMemberError` if the caller is not a member of
    its project; `app/main.py` maps both to 404.
    """
    project_id = _resolve_conversation_project(conn, conversation_id=conversation_id)
    require_role(conn, user_id, project_id, "admin", "editor", "viewer")

    message_rows = conn.execute(
        "select id, session_id, role, text, author_id, grounded, created_at "
        "from chat_messages "
        "where session_id = %s "
        "order by created_at",
        (conversation_id,),
    ).fetchall()

    message_ids = [str(row["id"]) for row in message_rows]
    citations_by_message: dict[str, list[dict]] = {message_id: [] for message_id in message_ids}
    has_model_log: set[str] = set()
    if message_ids:
        # Left-joined to chunks/documents so an unavailable citation
        # (chunk_id null, Requirement 8.9) still returns a row -- filename
        # and page_from simply come back null for it, matching
        # `available: false`.
        citation_rows = conn.execute(
            "select mc.message_id, mc.marker, mc.chunk_id, mc.document_id, "
            "       c.page_from, d.filename "
            "from message_citations mc "
            "left join chunks c on c.id = mc.chunk_id "
            "left join documents d on d.id = mc.document_id "
            "where mc.message_id = any(%s) "
            "order by mc.marker",
            (message_ids,),
        ).fetchall()
        for row in citation_rows:
            citations_by_message[str(row["message_id"])].append(
                {
                    "marker": row["marker"],
                    "chunk_id": str(row["chunk_id"]) if row["chunk_id"] is not None else None,
                    "document_id": str(row["document_id"]) if row["document_id"] is not None else None,
                    "available": row["chunk_id"] is not None,
                    "filename": row["filename"],
                    "page_from": row["page_from"],
                }
            )
        # has_model_log lets the client decide whether to render the eye
        # icon without a separate round trip per message (Requirement
        # 16.18). Refusal messages never have a row here (Requirement
        # 16.20), so this set is naturally empty for them.
        log_rows = conn.execute(
            "select message_id from message_model_logs where message_id = any(%s)",
            (message_ids,),
        ).fetchall()
        has_model_log = {str(row["message_id"]) for row in log_rows}

    return [
        Message(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            role=row["role"],
            text=row["text"],
            author_id=str(row["author_id"]) if row["author_id"] is not None else None,
            grounded=row["grounded"],
            created_at=row["created_at"],
            citations=citations_by_message[str(row["id"])],
            has_model_log=str(row["id"]) in has_model_log,
        )
        for row in message_rows
    ]
