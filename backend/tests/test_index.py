"""Tests for the index stage job handler.

Needs a real Postgres with pgvector (the `chunks` table has a `vector(512)`
column and a generated `tsvector` column that cannot be exercised against a
mock). No real-DB fixture exists yet elsewhere in this test suite, so this
module creates its own minimal one: skip the whole module if
`DATABASE_URL` is not reachable, otherwise apply migrations once and wrap
each test in a transaction-per-test via `app.db`.

S3 and `app.embeddings.embed_texts` are mocked — no real Bedrock or S3
calls. `app.jobs.Job` is constructed directly rather than claimed from the
queue, since only the handler itself is under test here.

Validates: Requirements 6.3, 6.12, 11.1, 11.3
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest

from app import db
from app.config import get_settings
from app.embeddings import EmbeddingResult
from app.index import run_index_job
from app.jobs import Job

settings = get_settings()


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
    """Apply migrations once per test run against the configured database."""
    from app import migrate

    migrate.up()


@pytest.fixture
def conn():
    db.open_pool()
    with db.connection() as c:
        yield c
        # Tests commit via the handler's own connection usage; clean up
        # explicitly rather than relying on rollback since `db.connection()`
        # commits on clean exit.
    db.close_pool()


def _make_project_and_document(conn) -> tuple[str, str]:
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
    document_id = str(uuid.uuid4())
    conn.execute(
        "insert into documents (id, project_id, filename, status, s3_key_original, uploaded_by) "
        "values (%s, %s, 'test.pdf', 'indexing', 'projects/x/documents/x/original.pdf', %s)",
        (document_id, project_id, user_id),
    )
    return project_id, document_id


def _parsed_blocks() -> list[dict]:
    return [
        {
            "page_no": 1,
            "text": "Reported NAV for the interest as at 30 September 2025 is $42.6m.",
            "bbox": [72.0, 340.5, 468.0, 366.2],
            "kind": "paragraph",
            "heading_path": "Capital Account > Net Asset Value",
        },
        {
            "page_no": 1,
            "text": "Called capital stands at $10.0m.",
            "bbox": [72.0, 370.0, 468.0, 390.0],
            "kind": "paragraph",
            "heading_path": "Capital Account > Net Asset Value",
        },
    ]


def _fake_embed_texts(monkeypatch, dimensions: int = 512, embed_model: str = "fake-model-v1"):
    def _fake(texts: list[str]) -> list[EmbeddingResult]:
        return [EmbeddingResult(vector=tuple([0.01 * (i + 1)] * dimensions), embed_model=embed_model) for i in range(len(texts))]

    monkeypatch.setattr("app.index.embed_texts", _fake)
    return embed_model


def _fake_get_bytes(monkeypatch, blocks: list[dict]):
    payload = json.dumps({"pages": [{"page_no": 1, "width": 612.0, "height": 792.0}], "blocks": blocks}).encode("utf-8")
    monkeypatch.setattr("app.index.s3.get_bytes", lambda key: payload)


# --- unit tests --------------------------------------------------------------


def test_run_index_job_inserts_chunks_regions_and_advances_status(conn, monkeypatch):
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    embed_model = _fake_embed_texts(monkeypatch)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    chunks = conn.execute(
        "select id, ordinal, heading_path, embed_model, page_from, page_to "
        "from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    assert len(chunks) == 1  # both blocks share a heading path, under target tokens
    assert chunks[0]["embed_model"] == embed_model
    assert chunks[0]["page_from"] == 1
    assert chunks[0]["page_to"] == 1

    regions = conn.execute(
        "select cr.page_no, cr.x0, cr.y0, cr.x1, cr.y1 "
        "from chunk_regions cr join chunks c on c.id = cr.chunk_id "
        "where c.document_id = %s",
        (document_id,),
    ).fetchall()
    assert len(regions) == 2  # one region per contributing block

    status = conn.execute("select status from documents where id = %s", (document_id,)).fetchone()
    assert status["status"] == "ready"


def test_run_index_job_twice_is_idempotent(conn, monkeypatch):
    """P15: running index twice yields the identical chunk set, not doubled."""
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    _fake_embed_texts(monkeypatch)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    first_chunks = conn.execute(
        "select ordinal, text, heading_path, page_from, page_to, token_count "
        "from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    first_regions_count = conn.execute(
        "select count(*) as n from chunk_regions cr join chunks c on c.id = cr.chunk_id "
        "where c.document_id = %s",
        (document_id,),
    ).fetchone()["n"]

    # Document must be back in 'indexing' for a second index run to be legal
    # per the state machine (ready -> indexing is a declared transition).
    from app.documents import apply_transition

    apply_transition(conn, document_id, "indexing")
    run_index_job(conn, job)

    second_chunks = conn.execute(
        "select ordinal, text, heading_path, page_from, page_to, token_count "
        "from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    second_regions_count = conn.execute(
        "select count(*) as n from chunk_regions cr join chunks c on c.id = cr.chunk_id "
        "where c.document_id = %s",
        (document_id,),
    ).fetchone()["n"]

    assert len(second_chunks) == len(first_chunks)
    assert [dict(c) for c in second_chunks] == [dict(c) for c in first_chunks]
    assert second_regions_count == first_regions_count

    status = conn.execute("select status from documents where id = %s", (document_id,)).fetchone()
    assert status["status"] == "ready"


def test_embed_model_recorded_matches_embeddings_output(conn, monkeypatch):
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    embed_model = _fake_embed_texts(monkeypatch, embed_model="amazon.titan-embed-text-v2:0")

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    rows = conn.execute("select embed_model from chunks where document_id = %s", (document_id,)).fetchall()
    assert len(rows) >= 1
    assert all(row["embed_model"] == embed_model for row in rows)
