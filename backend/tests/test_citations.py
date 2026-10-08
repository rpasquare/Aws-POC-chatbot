"""Tests for marker extraction and citation recording.

Validates: Requirements 8.1, 8.2, 8.8

No real Postgres connection, matching the rest of this suite's convention
(see `test_auth.py`'s `_FakeConnection`): a minimal stand-in backing
`message_citations` with a plain Python list, enforcing the same
`unique (message_id, marker)` constraint the real migration declares so a
duplicate insert attempt is caught here too.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from app.citations import extract_markers, record_citations
from app.retrieval import RetrievedChunk


def _chunk(chunk_id: str, document_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        score=1.0,
        filename="report.pdf",
        page_from=1,
        page_to=1,
        heading_path="Section One",
        text="The reported NAV is $42.6m.",
    )


class _FakeConnection:
    """Backs `message_citations` with a plain Python list.

    Mirrors the real migration's `unique (message_id, marker)` constraint
    by raising if a duplicate (message_id, marker) pair is inserted, so a
    bug in deduplication would be caught by these tests rather than only
    at the database layer.
    """

    def __init__(self) -> None:
        self.citations: list[dict] = []

    def execute(self, query: str, params: tuple = ()):
        normalized = " ".join(query.split())
        if normalized.startswith("insert into message_citations"):
            message_id, marker, chunk_id, document_id = params
            key = (message_id, marker)
            if any((row["message_id"], row["marker"]) == key for row in self.citations):
                raise AssertionError(f"duplicate (message_id, marker) insert: {key!r}")
            self.citations.append(
                {
                    "message_id": message_id,
                    "marker": marker,
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                }
            )
            return None
        raise AssertionError(f"unexpected query in fake connection: {query!r}")


# --- extract_markers ---------------------------------------------------------


def test_extract_markers_from_realistic_prose():
    text = "The NAV is $42.6m [1] and called capital is $10m [2]."
    assert extract_markers(text) == [1, 2]


def test_extract_markers_with_no_markers_returns_empty_list():
    assert extract_markers("The documents do not cover this question.") == []


def test_extract_markers_handles_multi_digit_numbers():
    assert extract_markers("See [1] and [12] and [9].") == [1, 12, 9]


def test_extract_markers_preserves_duplicates_and_order():
    assert extract_markers("[1] then again [2] then [1] once more.") == [1, 2, 1]


# --- record_citations: unit tests -------------------------------------------


def test_single_valid_marker_produces_one_citation_row():
    conn = _FakeConnection()
    chunks = [_chunk("chunk-1", "doc-1")]
    record_citations(
        conn,
        message_id="message-1",
        answer_text="The NAV is $42.6m [1].",
        slot_map={1: "chunk-1"},
        chunks=chunks,
    )

    assert len(conn.citations) == 1
    row = conn.citations[0]
    assert row["message_id"] == "message-1"
    assert row["marker"] == 1
    assert row["chunk_id"] == "chunk-1"
    assert row["document_id"] == "doc-1"


def test_marker_not_in_slot_map_is_discarded_without_error():
    conn = _FakeConnection()
    chunks = [_chunk("chunk-1", "doc-1")]
    record_citations(
        conn,
        message_id="message-1",
        answer_text="Figures from [1] and a fabricated [9].",
        slot_map={1: "chunk-1"},  # only slot 1 was supplied
        chunks=chunks,
    )

    assert len(conn.citations) == 1
    assert conn.citations[0]["marker"] == 1


def test_multiple_distinct_valid_markers_each_produce_their_own_row():
    conn = _FakeConnection()
    chunks = [_chunk("chunk-1", "doc-1"), _chunk("chunk-2", "doc-2")]
    record_citations(
        conn,
        message_id="message-1",
        answer_text="NAV is $42.6m [1] and called capital is $10m [2].",
        slot_map={1: "chunk-1", 2: "chunk-2"},
        chunks=chunks,
    )

    assert len(conn.citations) == 2
    by_marker = {row["marker"]: row for row in conn.citations}
    assert by_marker[1]["chunk_id"] == "chunk-1"
    assert by_marker[1]["document_id"] == "doc-1"
    assert by_marker[2]["chunk_id"] == "chunk-2"
    assert by_marker[2]["document_id"] == "doc-2"


def test_repeated_marker_in_prose_produces_exactly_one_row():
    conn = _FakeConnection()
    chunks = [_chunk("chunk-1", "doc-1")]
    record_citations(
        conn,
        message_id="message-1",
        answer_text="NAV is $42.6m [1]. This is confirmed again [1] on the next line.",
        slot_map={1: "chunk-1"},
        chunks=chunks,
    )

    assert len(conn.citations) == 1
    assert conn.citations[0]["marker"] == 1


def test_answer_with_no_markers_produces_zero_rows():
    conn = _FakeConnection()
    record_citations(
        conn,
        message_id="message-1",
        answer_text="The documents do not cover this question.",
        slot_map={1: "chunk-1"},
        chunks=[_chunk("chunk-1", "doc-1")],
    )

    assert conn.citations == []


def test_all_markers_invalid_produces_zero_rows_without_error():
    conn = _FakeConnection()
    record_citations(
        conn,
        message_id="message-1",
        answer_text="Fabricated citations [5] and [6].",
        slot_map={1: "chunk-1"},
        chunks=[_chunk("chunk-1", "doc-1")],
    )

    assert conn.citations == []


# --- record_citations: property test ----------------------------------------


@given(
    valid_slots=st.lists(st.integers(min_value=1, max_value=20), min_size=0, max_size=8, unique=True),
    invalid_markers=st.lists(st.integers(min_value=21, max_value=40), min_size=0, max_size=8, unique=True),
)
def test_citation_rows_are_exactly_valid_markers_deduplicated(valid_slots, invalid_markers):
    """For any set of valid slot numbers embedded in generated prose plus
    any set of invalid marker numbers also embedded, the resulting
    citation rows are exactly the valid ones, deduplicated.

    Each valid slot and invalid marker is embedded twice in the prose to
    exercise deduplication alongside discarding.
    """
    slot_map = {slot: f"chunk-{slot}" for slot in valid_slots}
    chunks = [_chunk(f"chunk-{slot}", f"doc-{slot}") for slot in valid_slots]

    all_markers = list(valid_slots) + list(invalid_markers)
    prose_parts = [f"fact [{marker}]" for marker in all_markers] * 2  # each marker appears twice
    answer_text = " and ".join(prose_parts) + "."

    conn = _FakeConnection()
    record_citations(
        conn,
        message_id="message-1",
        answer_text=answer_text,
        slot_map=slot_map,
        chunks=chunks,
    )

    recorded_markers = {row["marker"] for row in conn.citations}
    assert recorded_markers == set(valid_slots)
    assert len(conn.citations) == len(set(valid_slots))
    for row in conn.citations:
        assert row["chunk_id"] == f"chunk-{row['marker']}"
        assert row["document_id"] == f"doc-{row['marker']}"
