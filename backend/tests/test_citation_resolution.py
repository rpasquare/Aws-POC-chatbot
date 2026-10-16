"""Tests for citation resolution and region-to-percentage mapping.

Validates: Requirements 8.4, 8.5, 8.6, 8.9, 11.1

No real Postgres connection, matching the rest of this suite's convention
(see `test_conversations.py`'s `_FakeConnection`): a minimal stand-in
backing `project_members`, `chat_sessions`, `chat_messages`,
`message_citations`, `chunks`, `document_pages` and `chunk_regions` with
plain Python lists/dicts.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.authz import NotAMemberError
from app.citation_resolution import (
    MessageNotFoundError,
    resolve_citation,
    to_percentage_offsets,
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
    def __init__(
        self,
        *,
        members: dict[tuple[str, str], str],
        sessions: list[dict] | None = None,
        messages: list[dict] | None = None,
        citations: list[dict] | None = None,
        chunks: list[dict] | None = None,
        pages: list[dict] | None = None,
        regions: list[dict] | None = None,
    ) -> None:
        self.members = dict(members)
        self.sessions = sessions or []
        self.messages = messages or []
        self.citations = citations or []
        self.chunks = chunks or []
        self.pages = pages or []
        self.regions = regions or []

    def execute(self, query: str, params: tuple = ()):
        normalized = " ".join(query.split())

        if normalized.startswith("select is_super_admin from users where id"):
            return _FakeResult({"is_super_admin": False})

        if normalized.startswith("select role from project_members where project_id"):
            project_id, user_id = params
            role = self.members.get((project_id, user_id))
            return _FakeResult({"role": role} if role else None)

        if normalized.startswith(
            "select cs.project_id from chat_messages cm join chat_sessions cs on cs.id = cm.session_id where cm.id"
        ):
            (message_id,) = params
            for m in self.messages:
                if m["id"] == message_id:
                    for s in self.sessions:
                        if s["id"] == m["session_id"]:
                            return _FakeResult({"project_id": s["project_id"]})
            return _FakeResult(None)

        if normalized.startswith("select chunk_id, document_id from message_citations where message_id"):
            message_id, marker = params
            for c in self.citations:
                if c["message_id"] == message_id and c["marker"] == marker:
                    return _FakeResult({"chunk_id": c["chunk_id"], "document_id": c["document_id"]})
            return _FakeResult(None)

        if normalized.startswith("select c.id, c.document_id, c.page_from, d.filename"):
            (chunk_id,) = params
            for c in self.chunks:
                if c["id"] == chunk_id:
                    return _FakeResult(dict(c))
            return _FakeResult(None)

        if normalized.startswith("select width, height, image_key from document_pages where document_id"):
            document_id, page_no = params
            for p in self.pages:
                if p["document_id"] == document_id and p["page_no"] == page_no:
                    return _FakeResult(dict(p))
            return _FakeResult(None)

        if normalized.startswith("select x0, y0, x1, y1 from chunk_regions where chunk_id"):
            chunk_id, page_no = params
            rows = [dict(r) for r in self.regions if r["chunk_id"] == chunk_id and r["page_no"] == page_no]
            return _FakeResult(rows=rows)

        raise AssertionError(f"unexpected query in fake connection: {query!r}")


_PROJECT = "project-1"
_MESSAGE = "message-1"
_CHUNK = "chunk-1"
_DOCUMENT = "doc-1"


def _base_conn(**overrides) -> _FakeConnection:
    defaults = dict(
        members={(_PROJECT, "viewer-user"): "viewer", (_PROJECT, "admin-user"): "admin"},
        sessions=[{"id": "conversation-1", "project_id": _PROJECT}],
        messages=[{"id": _MESSAGE, "session_id": "conversation-1"}],
        citations=[{"message_id": _MESSAGE, "marker": 1, "chunk_id": _CHUNK, "document_id": _DOCUMENT}],
        chunks=[{"id": _CHUNK, "document_id": _DOCUMENT, "page_from": 2, "filename": "report.pdf"}],
        pages=[{"width": 612.0, "height": 792.0, "image_key": "proj/doc/page-2.webp"}],
        regions=[{"chunk_id": _CHUNK, "page_no": 2, "x0": 61.2, "y0": 79.2, "x1": 550.8, "y1": 158.4}],
    )
    defaults.update(overrides)
    # pages lookup needs document_id/page_no, add them from chunk defaults unless overridden
    for p in defaults["pages"]:
        p.setdefault("document_id", _DOCUMENT)
        p.setdefault("page_no", 2)
    return _FakeConnection(**defaults)


# --- resolve_citation: happy path and permission gating ---------------------


def test_viewer_can_resolve_a_citation():
    """Requirement 11.1 (resolved): a viewer may open a cited page image,
    the same as admin and editor.
    """
    conn = _base_conn()
    with patch("app.citation_resolution.s3.presigned_get", return_value="https://signed.example/page-2.webp"):
        result = resolve_citation(conn, user_id="viewer-user", message_id=_MESSAGE, marker=1)

    assert result.available is True
    assert result.document_id == _DOCUMENT
    assert result.filename == "report.pdf"
    assert result.page_no == 2
    assert result.image_url == "https://signed.example/page-2.webp"
    assert len(result.regions) == 1


def test_non_member_raises_not_a_member_error():
    conn = _base_conn()
    with pytest.raises(NotAMemberError):
        resolve_citation(conn, user_id="stranger", message_id=_MESSAGE, marker=1)


def test_nonexistent_message_raises_message_not_found_error():
    conn = _base_conn()
    with pytest.raises(MessageNotFoundError):
        resolve_citation(conn, user_id="viewer-user", message_id="does-not-exist", marker=1)


# --- resolve_citation: graceful degradation (Requirement 8.9) ---------------


def test_null_chunk_id_resolves_as_unavailable_not_an_error():
    """A citation whose document was purged has chunk_id = NULL on the
    message_citations row. This must resolve as unavailable, not raise.
    """
    conn = _base_conn(citations=[{"message_id": _MESSAGE, "marker": 1, "chunk_id": None, "document_id": None}])
    result = resolve_citation(conn, user_id="viewer-user", message_id=_MESSAGE, marker=1)
    assert result.available is False


def test_missing_chunk_row_resolves_as_unavailable():
    conn = _base_conn(chunks=[])
    result = resolve_citation(conn, user_id="viewer-user", message_id=_MESSAGE, marker=1)
    assert result.available is False


def test_missing_page_row_resolves_as_unavailable():
    conn = _base_conn(pages=[])
    result = resolve_citation(conn, user_id="viewer-user", message_id=_MESSAGE, marker=1)
    assert result.available is False


def test_unknown_marker_resolves_as_unavailable():
    conn = _base_conn()
    result = resolve_citation(conn, user_id="viewer-user", message_id=_MESSAGE, marker=99)
    assert result.available is False


# --- to_percentage_offsets (Requirement 8.5) --------------------------------


def test_percentage_offsets_scale_invariant_example():
    """A fixed, concrete example: a box exactly covering the left half of
    a page maps to 0-50% on the x-axis, independent of the page's own
    point dimensions.
    """
    offsets = to_percentage_offsets(x0=0, y0=0, x1=306, y1=792, page_width=612.0, page_height=792.0)
    assert offsets.x0_pct == pytest.approx(0.0)
    assert offsets.x1_pct == pytest.approx(50.0)
    assert offsets.y0_pct == pytest.approx(0.0)
    assert offsets.y1_pct == pytest.approx(100.0)
