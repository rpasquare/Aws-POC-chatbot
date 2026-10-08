"""Tests for hybrid retrieval (vector + full-text, fused by RRF).

`fuse_rankings` is pure Python with no I/O, so it is tested in isolation
with hand-computed expected scores, no database required.

`retrieve` needs a real Postgres with pgvector (cosine distance over
`vector(512)` and a generated `tsvector` column cannot be exercised
against a mock), so it follows the same skip-if-unreachable, migrate-once,
transaction-per-test convention as `test_index.py`.

Validates: Requirements 7.1, 7.6
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from app import db
from app.config import get_settings
from app.retrieval import RetrievedChunk, fuse_rankings, retrieve

settings = get_settings()


# --- fuse_rankings: pure function, no DB -----------------------------------


def test_fuse_rankings_single_method_matches_hand_computed_k60():
    # Rank 1 and rank 2 in one method only, k = 60.
    scores = fuse_rankings([["a", "b"]], k=60)

    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)


def test_fuse_rankings_chunk_in_both_methods_gets_sum_of_both_terms():
    # "a" is rank 1 in method 1 and rank 2 in method 2.
    scores = fuse_rankings([["a", "b"], ["c", "a"]], k=60)

    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62)
    assert scores["c"] == pytest.approx(1 / 61)


def test_fuse_rankings_chunk_in_only_one_method_gets_only_that_term():
    # "z" never appears in method 1's ranking at all.
    scores = fuse_rankings([["a"], ["z", "a"]], k=60)

    assert scores["z"] == pytest.approx(1 / 61)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert "z" in scores
    assert len(scores) == 2


def test_fuse_rankings_empty_rankings_produce_no_scores():
    assert fuse_rankings([], k=60) == {}
    assert fuse_rankings([[], []], k=60) == {}


def test_fuse_rankings_three_methods_sums_all_contributing_terms():
    scores = fuse_rankings([["a", "b"], ["b", "a"], ["a"]], k=60)

    # a: rank 1 (method 1) + rank 2 (method 2) + rank 1 (method 3)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62 + 1 / 61)
    # b: rank 2 (method 1) + rank 1 (method 2)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)


# --- retrieve: needs a real Postgres with pgvector -------------------------


def _db_reachable() -> bool:
    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(),
    reason="requires a real Postgres with pgvector (DATABASE_URL); not reachable in this environment",
)


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema():
    from app import migrate

    migrate.up()


@pytest.fixture
def conn():
    db.open_pool()
    with db.connection() as c:
        yield c
    db.close_pool()


def _make_project(conn) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    conn.execute(
        "insert into users (id, email, external_id) values (%s, %s, %s)",
        (user_id, f"{user_id}@example.com", user_id),
    )
    project_id = str(uuid.uuid4())
    conn.execute(
        "insert into projects (id, name, created_by) values (%s, 'Test Project', %s)",
        (project_id, user_id),
    )
    return project_id, user_id


def _make_document(conn, project_id: str, user_id: str, *, filename: str, status: str = "ready", deleted_at=None) -> str:
    document_id = str(uuid.uuid4())
    conn.execute(
        "insert into documents (id, project_id, filename, status, s3_key_original, uploaded_by, deleted_at) "
        "values (%s, %s, %s, %s, 'projects/x/documents/x/original.pdf', %s, %s)",
        (document_id, project_id, filename, status, user_id, deleted_at),
    )
    return document_id


def _vector(seed: float, dimensions: int | None = None) -> tuple[float, ...]:
    dimensions = dimensions if dimensions is not None else settings.embed_dimensions
    return tuple([seed] * dimensions)


def _vector_literal(vector: tuple[float, ...]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _insert_chunk(
    conn,
    *,
    document_id: str,
    project_id: str,
    ordinal: int,
    text: str,
    embedding: tuple[float, ...],
    heading_path: str | None = None,
    page_from: int = 1,
    page_to: int = 1,
) -> str:
    row = conn.execute(
        "insert into chunks "
        "(document_id, project_id, ordinal, text, heading_path, page_from, page_to, "
        " token_count, embedding, embed_model) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s) "
        "returning id",
        (
            document_id,
            project_id,
            ordinal,
            text,
            heading_path,
            page_from,
            page_to,
            len(text.split()),
            _vector_literal(embedding),
            settings.embed_model,
        ),
    ).fetchone()
    return str(row["id"])


def _fake_embed_query(monkeypatch, vector: tuple[float, ...]):
    from app.embeddings import EmbeddingResult

    monkeypatch.setattr(
        "app.retrieval.embed_text",
        lambda text: EmbeddingResult(vector=vector, embed_model=settings.embed_model),
    )


def test_retrieve_returns_chunk_with_expected_fields(conn, monkeypatch):
    project_id, user_id = _make_project(conn)
    document_id = _make_document(conn, project_id, user_id, filename="report.pdf")
    query_vector = _vector(0.1)
    _insert_chunk(
        conn,
        document_id=document_id,
        project_id=project_id,
        ordinal=0,
        text="Net asset value is $42.6m as at 30 September 2025.",
        embedding=query_vector,
        heading_path="Capital Account > Net Asset Value",
        page_from=3,
        page_to=3,
    )
    _fake_embed_query(monkeypatch, query_vector)

    results = retrieve(conn, project_id=project_id, query_text="net asset value")

    assert len(results) == 1
    chunk = results[0]
    assert isinstance(chunk, RetrievedChunk)
    assert chunk.document_id == document_id
    assert chunk.filename == "report.pdf"
    assert chunk.page_from == 3
    assert chunk.page_to == 3
    assert chunk.heading_path == "Capital Account > Net Asset Value"
    assert chunk.score > 0


def test_retrieve_excludes_non_ready_and_deleted_documents(conn, monkeypatch):
    project_id, user_id = _make_project(conn)
    ready_doc = _make_document(conn, project_id, user_id, filename="ready.pdf", status="ready")
    indexing_doc = _make_document(conn, project_id, user_id, filename="indexing.pdf", status="indexing")
    deleted_doc = _make_document(
        conn, project_id, user_id, filename="deleted.pdf", status="ready", deleted_at="now()"
    )
    # deleted_at above needs to be an actual timestamp, not the literal string.
    conn.execute("update documents set deleted_at = now() where id = %s", (deleted_doc,))

    query_vector = _vector(0.2)
    _insert_chunk(conn, document_id=ready_doc, project_id=project_id, ordinal=0, text="alpha bravo charlie", embedding=query_vector)
    _insert_chunk(conn, document_id=indexing_doc, project_id=project_id, ordinal=0, text="alpha bravo charlie", embedding=query_vector)
    _insert_chunk(conn, document_id=deleted_doc, project_id=project_id, ordinal=0, text="alpha bravo charlie", embedding=query_vector)
    _fake_embed_query(monkeypatch, query_vector)

    results = retrieve(conn, project_id=project_id, query_text="alpha bravo charlie")

    assert [r.document_id for r in results] == [ready_doc]


def test_retrieve_excludes_other_projects(conn, monkeypatch):
    project_id, user_id = _make_project(conn)
    other_project_id, other_user_id = _make_project(conn)
    own_doc = _make_document(conn, project_id, user_id, filename="own.pdf")
    other_doc = _make_document(conn, other_project_id, other_user_id, filename="other.pdf")

    query_vector = _vector(0.3)
    _insert_chunk(conn, document_id=own_doc, project_id=project_id, ordinal=0, text="unique identifier zx901", embedding=query_vector)
    _insert_chunk(
        conn, document_id=other_doc, project_id=other_project_id, ordinal=0, text="unique identifier zx901", embedding=query_vector
    )
    _fake_embed_query(monkeypatch, query_vector)

    results = retrieve(conn, project_id=project_id, query_text="unique identifier zx901")

    assert [r.document_id for r in results] == [own_doc]


def test_retrieve_finds_literal_token_via_full_text_even_with_distant_embedding(conn, monkeypatch):
    """A rare literal token (e.g. a section reference) should be retrievable
    through the full-text side even when the query embedding is nowhere near
    the chunk's embedding — Requirement 7.6."""
    project_id, user_id = _make_project(conn)
    document_id = _make_document(conn, project_id, user_id, filename="contract.pdf")
    # Chunk's own embedding is unrelated to the query's embedding.
    _insert_chunk(
        conn,
        document_id=document_id,
        project_id=project_id,
        ordinal=0,
        text="See clause ZX-77-QQ for the indemnification carve-out.",
        embedding=_vector(0.9),
    )
    # Query embeds far away in vector space, so only full text can find it.
    _fake_embed_query(monkeypatch, _vector(-0.9))

    results = retrieve(conn, project_id=project_id, query_text="ZX-77-QQ")

    assert any(r.document_id == document_id for r in results)


def test_retrieve_respects_top_k_override(conn, monkeypatch):
    project_id, user_id = _make_project(conn)
    document_id = _make_document(conn, project_id, user_id, filename="many-chunks.pdf")
    query_vector = _vector(0.4)
    for i in range(5):
        _insert_chunk(
            conn,
            document_id=document_id,
            project_id=project_id,
            ordinal=i,
            text=f"shared keyword occurrence number {i}",
            embedding=query_vector,
        )
    _fake_embed_query(monkeypatch, query_vector)

    results = retrieve(conn, project_id=project_id, query_text="shared keyword occurrence", top_k=2)

    assert len(results) == 2
