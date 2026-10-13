"""Tests for conversation and message role-gating.

Validates: Requirements 9.1, 9.2, 9.4, 9.6

No real Postgres connection, matching the rest of this suite's convention
(see `test_auth.py`'s `_FakeConnection`): a minimal stand-in backing
`users`, `project_members`, `chat_sessions`, `chat_messages` and
`message_citations` with plain Python lists/dicts.
"""

from __future__ import annotations

import pytest

from app.authz import NotAMemberError
from app.conversations import (
    ConversationNotFoundError,
    create_conversation,
    list_conversations,
    list_messages,
)


class _FakeResult:
    def __init__(self, row=None, rows=None) -> None:
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeConnection:
    """Backs `users`, `project_members`, `chat_sessions`, `chat_messages`
    and `message_citations` with plain Python lists/dicts -- enough for
    `app.authz.role_on_project`/`require_role` and `app.conversations` to
    run against.
    """

    def __init__(self, *, members: dict[tuple[str, str], str], is_super_admin: set[str] | None = None) -> None:
        # members: {(project_id, user_id): role}
        self.members = dict(members)
        self.is_super_admin = is_super_admin or set()
        self.sessions: list[dict] = []
        self.messages: list[dict] = []
        self.citations: list[dict] = []
        self._next_session_id = 1
        self._next_message_id = 1

    def execute(self, query: str, params: tuple = ()):
        normalized = " ".join(query.split())

        if normalized.startswith("select is_super_admin from users where id"):
            (user_id,) = params
            # Any user id passed in tests is treated as a known user.
            return _FakeResult({"is_super_admin": user_id in self.is_super_admin})

        if normalized.startswith("select role from project_members where project_id"):
            project_id, user_id = params
            role = self.members.get((project_id, user_id))
            return _FakeResult({"role": role} if role else None)

        if normalized.startswith("insert into chat_sessions"):
            project_id, title, created_by = params
            session_id = f"conversation-{self._next_session_id}"
            self._next_session_id += 1
            row = {
                "id": session_id,
                "project_id": project_id,
                "title": title,
                "created_by": created_by,
                "created_at": "2024-01-01T00:00:00+00:00",
                "last_active": "2024-01-01T00:00:00+00:00",
            }
            self.sessions.append(row)
            return _FakeResult(dict(row))

        if normalized.startswith("select id, project_id, title, created_by, created_at, last_active from chat_sessions where project_id"):
            (project_id,) = params
            rows = [dict(s) for s in self.sessions if s["project_id"] == project_id]
            rows.sort(key=lambda r: r["last_active"], reverse=True)
            return _FakeResult(rows=rows)

        if normalized.startswith("select project_id from chat_sessions where id"):
            (conversation_id,) = params
            for s in self.sessions:
                if s["id"] == conversation_id:
                    return _FakeResult({"project_id": s["project_id"]})
            return _FakeResult(None)

        if normalized.startswith("select id, session_id, role, text, author_id, grounded, created_at from chat_messages"):
            (session_id,) = params
            rows = [dict(m) for m in self.messages if m["session_id"] == session_id]
            rows.sort(key=lambda r: r["created_at"])
            return _FakeResult(rows=rows)

        if normalized.startswith("select message_id, marker, chunk_id, document_id from message_citations"):
            (message_ids,) = params
            rows = [dict(c) for c in self.citations if c["message_id"] in message_ids]
            rows.sort(key=lambda r: r["marker"])
            return _FakeResult(rows=rows)

        raise AssertionError(f"unexpected query in fake connection: {query!r}")


# --- create_conversation -----------------------------------------------------


def test_admin_can_create_conversation():
    conn = _FakeConnection(members={("project-1", "admin-1"): "admin"})
    conversation = create_conversation(conn, user_id="admin-1", project_id="project-1")
    assert conversation.project_id == "project-1"
    assert conversation.created_by == "admin-1"
    assert conversation.title == "New conversation"
    assert len(conn.sessions) == 1


def test_editor_can_create_conversation():
    conn = _FakeConnection(members={("project-1", "editor-1"): "editor"})
    conversation = create_conversation(conn, user_id="editor-1", project_id="project-1")
    assert conversation.created_by == "editor-1"
    assert len(conn.sessions) == 1


def test_viewer_cannot_create_conversation_and_creates_no_row():
    conn = _FakeConnection(members={("project-1", "viewer-1"): "viewer"})
    with pytest.raises(NotAMemberError):
        create_conversation(conn, user_id="viewer-1", project_id="project-1")
    assert conn.sessions == []


def test_non_member_cannot_create_conversation_and_creates_no_row():
    conn = _FakeConnection(members={})
    with pytest.raises(NotAMemberError):
        create_conversation(conn, user_id="stranger-1", project_id="project-1")
    assert conn.sessions == []


def test_create_conversation_accepts_a_custom_title():
    conn = _FakeConnection(members={("project-1", "admin-1"): "admin"})
    conversation = create_conversation(conn, user_id="admin-1", project_id="project-1", title="Q3 diligence")
    assert conversation.title == "Q3 diligence"


# --- list_conversations -------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
def test_any_member_role_can_list_conversations(role):
    conn = _FakeConnection(
        members={("project-1", "admin-1"): "admin", ("project-1", "user-1"): role}
    )
    create_conversation(conn, user_id="admin-1", project_id="project-1")

    conversations = list_conversations(conn, user_id="user-1", project_id="project-1")
    assert len(conversations) == 1


def test_non_member_cannot_list_conversations():
    conn = _FakeConnection(members={})
    with pytest.raises(NotAMemberError):
        list_conversations(conn, user_id="stranger-1", project_id="project-1")


def test_list_conversations_orders_most_recently_active_first():
    conn = _FakeConnection(members={("project-1", "admin-1"): "admin"})
    first = create_conversation(conn, user_id="admin-1", project_id="project-1", title="first")
    second = create_conversation(conn, user_id="admin-1", project_id="project-1", title="second")
    # Bump the first conversation's last_active past the second's.
    for s in conn.sessions:
        if s["id"] == first.id:
            s["last_active"] = "2024-06-01T00:00:00+00:00"

    conversations = list_conversations(conn, user_id="admin-1", project_id="project-1")
    assert [c.id for c in conversations] == [first.id, second.id]


# --- list_messages -------------------------------------------------------------


def test_list_messages_requires_conversation_to_exist():
    conn = _FakeConnection(members={("project-1", "admin-1"): "admin"})
    with pytest.raises(ConversationNotFoundError):
        list_messages(conn, user_id="admin-1", conversation_id="does-not-exist")


def test_list_messages_denies_non_member_of_conversations_project():
    conn = _FakeConnection(members={("project-1", "admin-1"): "admin"})
    conversation = create_conversation(conn, user_id="admin-1", project_id="project-1")
    with pytest.raises(NotAMemberError):
        list_messages(conn, user_id="stranger-1", conversation_id=conversation.id)


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
def test_any_member_role_can_list_messages(role):
    conn = _FakeConnection(members={("project-1", "admin-1"): "admin", ("project-1", "user-1"): role})
    conversation = create_conversation(conn, user_id="admin-1", project_id="project-1")
    conn.messages.append(
        {
            "id": "message-1",
            "session_id": conversation.id,
            "role": "user",
            "text": "What is the NAV?",
            "author_id": "admin-1",
            "grounded": True,
            "created_at": "2024-01-01T00:00:01+00:00",
        }
    )

    messages = list_messages(conn, user_id="user-1", conversation_id=conversation.id)
    assert len(messages) == 1
    assert messages[0].text == "What is the NAV?"
    assert messages[0].citations == []


def test_list_messages_attaches_citations_to_the_right_message():
    conn = _FakeConnection(members={("project-1", "admin-1"): "admin"})
    conversation = create_conversation(conn, user_id="admin-1", project_id="project-1")
    conn.messages.extend(
        [
            {
                "id": "message-1",
                "session_id": conversation.id,
                "role": "user",
                "text": "What is the NAV?",
                "author_id": "admin-1",
                "grounded": True,
                "created_at": "2024-01-01T00:00:01+00:00",
            },
            {
                "id": "message-2",
                "session_id": conversation.id,
                "role": "assistant",
                "text": "The NAV is $42.6m [1].",
                "author_id": None,
                "grounded": True,
                "created_at": "2024-01-01T00:00:02+00:00",
            },
        ]
    )
    conn.citations.append(
        {"message_id": "message-2", "marker": 1, "chunk_id": "chunk-1", "document_id": "doc-1"}
    )

    messages = list_messages(conn, user_id="admin-1", conversation_id=conversation.id)
    assert len(messages) == 2
    assert messages[0].citations == []
    assert messages[1].citations == [
        {"marker": 1, "chunk_id": "chunk-1", "document_id": "doc-1", "available": True}
    ]


def test_list_messages_marks_citation_unavailable_when_chunk_is_null():
    conn = _FakeConnection(members={("project-1", "admin-1"): "admin"})
    conversation = create_conversation(conn, user_id="admin-1", project_id="project-1")
    conn.messages.append(
        {
            "id": "message-1",
            "session_id": conversation.id,
            "role": "assistant",
            "text": "The NAV is $42.6m [1].",
            "author_id": None,
            "grounded": True,
            "created_at": "2024-01-01T00:00:01+00:00",
        }
    )
    conn.citations.append(
        {"message_id": "message-1", "marker": 1, "chunk_id": None, "document_id": None}
    )

    messages = list_messages(conn, user_id="admin-1", conversation_id=conversation.id)
    assert messages[0].citations == [
        {"marker": 1, "chunk_id": None, "document_id": None, "available": False}
    ]
