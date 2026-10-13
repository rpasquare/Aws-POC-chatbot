"""Tests for the relevance threshold and refusal path.

Validates: Requirements 7.3, 7.4

No real Postgres connection for the message-insertion helper, matching the
rest of this suite's convention (see `test_auth.py`'s `_FakeConnection`):
a minimal stand-in backing `chat_messages` with a plain Python list.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from app.answering import REFUSAL_TEXT, record_refusal_message, should_refuse
from app.retrieval import RetrievedChunk


def _chunk(score: float, chunk_id: str = "chunk-1") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        score=score,
        filename="report.pdf",
        page_from=1,
        page_to=1,
        heading_path="Section One",
        text="The reported NAV is $42.6m.",
    )


class _FakeResult:
    def __init__(self, row: dict | None) -> None:
        self._row = row

    def fetchone(self) -> dict | None:
        return self._row


class _FakeConnection:
    """Backs `chat_messages` with a plain Python list."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._next_id = 1

    def execute(self, query: str, params: tuple = ()) -> _FakeResult:
        normalized = " ".join(query.split())
        if normalized.startswith("insert into chat_messages"):
            session_id, text = params
            message_id = f"message-{self._next_id}"
            self._next_id += 1
            self.messages.append(
                {
                    "id": message_id,
                    "session_id": session_id,
                    "role": "assistant",
                    "text": text,
                    "author_id": None,
                    "grounded": False,
                }
            )
            return _FakeResult({"id": message_id})
        raise AssertionError(f"unexpected query in fake connection: {query!r}")


# --- should_refuse: example tests -------------------------------------------


def test_empty_chunk_list_refuses():
    assert should_refuse([]) is True


def test_best_score_below_threshold_refuses():
    chunks = [_chunk(0.01), _chunk(0.005, "chunk-2")]
    assert should_refuse(chunks, threshold=0.02) is True


def test_best_score_above_threshold_does_not_refuse():
    chunks = [_chunk(0.05), _chunk(0.01, "chunk-2")]
    assert should_refuse(chunks, threshold=0.02) is False


def test_best_score_exactly_at_threshold_does_not_refuse():
    # Design doc says the score "falls below" the threshold to trigger
    # refusal, so equality is still relevant, not a refusal.
    chunks = [_chunk(0.02)]
    assert should_refuse(chunks, threshold=0.02) is False


def test_only_the_first_best_score_is_considered():
    # retrieve() returns results sorted descending by fused score, so the
    # first element is the best one; a low second-place score must not
    # trigger a refusal on its own.
    chunks = [_chunk(0.5), _chunk(0.0001, "chunk-2")]
    assert should_refuse(chunks, threshold=0.02) is False


def test_default_threshold_comes_from_settings(monkeypatch):
    import app.answering as answering_module

    class _FakeSettings:
        relevance_threshold = 0.3

    monkeypatch.setattr(answering_module, "get_settings", lambda: _FakeSettings())

    assert should_refuse([_chunk(0.2)]) is True
    assert should_refuse([_chunk(0.3)]) is False


# --- should_refuse: property test -------------------------------------------


@given(score=st.floats(min_value=-10, max_value=10, allow_nan=False), threshold=st.floats(min_value=-10, max_value=10, allow_nan=False))
def test_refusal_matches_strict_less_than_comparison(score: float, threshold: float) -> None:
    """For any score and any threshold, should_refuse of a single-chunk
    list equals (score < threshold) exactly — the boundary condition is a
    strict less-than, matching the design doc's "falls below" wording.
    """
    chunks = [_chunk(score)]
    assert should_refuse(chunks, threshold=threshold) == (score < threshold)


# --- record_refusal_message -------------------------------------------------


def test_record_refusal_message_writes_grounded_false_assistant_row():
    conn = _FakeConnection()
    message_id = record_refusal_message(conn, session_id="session-1")

    assert len(conn.messages) == 1
    row = conn.messages[0]
    assert row["id"] == message_id
    assert row["session_id"] == "session-1"
    assert row["role"] == "assistant"
    assert row["author_id"] is None
    assert row["grounded"] is False
    assert row["text"] == REFUSAL_TEXT


def test_record_refusal_message_text_states_documents_do_not_cover_question():
    assert "do not cover" in REFUSAL_TEXT
