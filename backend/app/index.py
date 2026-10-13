"""The index stage job handler.

Reads the persisted parse output, builds chunks and embeddings from it, and
replaces the document's searchable content. The original PDF is never
opened here (Requirement 11.2's zero-cost rebuild depends on that), only
`parsed.json` written by the parse stage.

Delete-then-insert, not insert-then-delete and not a diff: existing
`chunks` rows for the document are removed first (cascading to
`chunk_regions` via FK), and the freshly built chunks are inserted after.
This ordering is what makes the stage idempotent — running it any number
of times on the same parse output yields the same chunk set, never a
doubled one. Requirements 6.12, 11.1, 11.3; design property P15.
"""

from __future__ import annotations

import json

from app import s3
from app.chunking import ChunkDraft, build_chunks
from app.config import get_settings
from app.documents import apply_transition
from app.embeddings import embed_texts


def _vector_literal(vector: tuple[float, ...]) -> str:
    """Render an embedding as a pgvector input literal, e.g. '[0.1,0.2]'.

    No pgvector Python adapter is registered in `app/db.py`, so the vector
    is passed as a bracketed string and cast with `::vector` in the SQL.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _insert_chunk(conn, *, document_id: str, project_id: str, draft: ChunkDraft, embedding, embed_model: str) -> str:
    row = conn.execute(
        "insert into chunks "
        "(document_id, project_id, ordinal, text, heading_path, page_from, page_to, "
        " token_count, embedding, embed_model) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s) "
        "returning id",
        (
            document_id,
            project_id,
            draft.ordinal,
            draft.text,
            draft.heading_path,
            draft.page_from,
            draft.page_to,
            draft.token_count,
            _vector_literal(embedding),
            embed_model,
        ),
    ).fetchone()
    return str(row["id"])


def _insert_regions(conn, *, chunk_id: str, draft: ChunkDraft) -> None:
    for region in draft.regions:
        x0, y0, x1, y1 = region.bbox
        conn.execute(
            "insert into chunk_regions (chunk_id, page_no, x0, y0, x1, y1) "
            "values (%s, %s, %s, %s, %s, %s)",
            (chunk_id, region.page_no, x0, y0, x1, y1),
        )


def run_index_job(conn, job) -> None:
    """Stage handler for `ingestion_jobs.stage = 'index'`.

    Requirement 6.3: content only becomes searchable once this handler
    advances the document to `ready`. Requirement 6.12/11.3 and property
    P15: delete-then-insert in one transaction makes a repeat run produce
    the identical chunk set rather than a duplicated one. Requirement 11.1:
    rebuilds read only the persisted `parsed.json`, never the original PDF.
    """
    row = conn.execute(
        "select project_id from documents where id = %s",
        (job.document_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no document with id {job.document_id!r}")
    project_id = str(row["project_id"])

    parsed = json.loads(s3.get_bytes(s3.parsed_key(project_id, job.document_id)))
    blocks = parsed["blocks"]

    settings = get_settings()
    chunk_drafts = build_chunks(
        blocks,
        target_tokens=settings.chunk_target_tokens,
        overlap_tokens=settings.chunk_overlap_tokens,
    )
    embedding_results = embed_texts([draft.text for draft in chunk_drafts])

    # Delete-then-insert, in this order, in the caller's transaction: see
    # module docstring and property P15.
    conn.execute("delete from chunks where document_id = %s", (job.document_id,))

    for draft, embedding_result in zip(chunk_drafts, embedding_results, strict=True):
        chunk_id = _insert_chunk(
            conn,
            document_id=job.document_id,
            project_id=project_id,
            draft=draft,
            embedding=embedding_result.vector,
            embed_model=embedding_result.embed_model,
        )
        _insert_regions(conn, chunk_id=chunk_id, draft=draft)

    apply_transition(conn, job.document_id, "ready")


__all__ = ["run_index_job"]
