"""Hybrid retrieval: vector similarity fused with full-text search.

Requirement 7.6 exists because financial documents are full of literal
identifiers such as section references, and pure vector search misses
them. Two independent candidate pools are queried — nearest neighbours by
cosine distance over `chunks.embedding`, and a `tsvector` match over
`chunks.text_search` — then combined by reciprocal rank fusion (RRF),
which needs no score normalisation between the two methods:

    score(chunk) = sum over methods of 1 / (k + rank_in_method)

`k` is `settings.rrf_k` (60). A chunk that appears in only one method's
candidate pool still gets a score, from that one term alone — it is not
penalised with an infinite or missing rank from the method that did not
surface it.

Security note: row-level security (RLS) is the eventual structural
guarantee (design doc, Sprint 3 task 7.5), but RLS does not exist yet.
Until it does, the explicit filters applied here — `project_id = %s`,
`documents.deleted_at is null`, `documents.status = 'ready'` — are the
actual (not merely illustrative) security boundary, satisfying
Requirements 7.1 and 6.3/10.2. They must not be weakened or dropped on
the assumption that RLS will cover it.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import Connection

from app.config import get_settings
from app.embeddings import embed_text

# Size of each method's own candidate pool, before fusion. Deliberately
# larger than `retrieval_top_k` so that a chunk ranked, say, 30th by one
# method but highly ranked by the other still has a chance to be pulled
# into the fused top N. Not currently exposed via `app.config.Settings`;
# flagged in task notes as a value that may want to become configurable
# once retrieval is calibrated against real questions.
_CANDIDATE_POOL_SIZE = 50


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk surfaced by hybrid retrieval, with its fused score."""

    chunk_id: str
    document_id: str
    score: float
    filename: str
    page_from: int
    page_to: int
    heading_path: str | None
    text: str


def _vector_literal(vector: tuple[float, ...]) -> str:
    """Render an embedding as a pgvector input literal, e.g. '[0.1,0.2]'.

    Mirrors `app.index._vector_literal`: no pgvector Python adapter is
    registered in `app/db.py`, so the vector travels as a bracketed string
    cast with `::vector` in the SQL.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _vector_candidates(conn: Connection, *, project_id: str, query_vector: tuple[float, ...], limit: int) -> list[str]:
    """Chunk ids ordered by ascending cosine distance (closer = smaller).

    Filtered to the project, to non-deleted documents and to `ready`
    documents — see module docstring on why this filter is load-bearing
    right now, not merely illustrative.
    """
    rows = conn.execute(
        "select c.id "
        "from chunks c "
        "join documents d on d.id = c.document_id "
        "where c.project_id = %s "
        "  and d.deleted_at is null "
        "  and d.status = 'ready' "
        "order by c.embedding <=> %s::vector "
        "limit %s",
        (project_id, _vector_literal(query_vector), limit),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _text_candidates(conn: Connection, *, project_id: str, query_text: str, limit: int) -> list[str]:
    """Chunk ids ordered by full-text relevance (`plainto_tsquery`).

    `plainto_tsquery` over `to_tsquery`: it tokenises arbitrary free-text
    questions without requiring the caller to know tsquery operator syntax,
    which is the safer choice for user-typed questions.
    """
    rows = conn.execute(
        "select c.id "
        "from chunks c "
        "join documents d on d.id = c.document_id "
        "where c.project_id = %s "
        "  and d.deleted_at is null "
        "  and d.status = 'ready' "
        "  and c.text_search @@ plainto_tsquery('english', %s) "
        "order by ts_rank(c.text_search, plainto_tsquery('english', %s)) desc "
        "limit %s",
        (project_id, query_text, query_text, limit),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def fuse_rankings(rankings: list[list[str]], *, k: int) -> dict[str, float]:
    """Reciprocal rank fusion over any number of ranked id lists.

    Pure function, no I/O: each element of `rankings` is one method's
    candidate ids in rank order (best first, 0-indexed internally, rank
    numbers used in the formula are 1-based). A chunk id missing from a
    given list contributes nothing to that list's term — it is treated as
    "this method did not rank it" rather than as an infinite or error
    rank, so a chunk found by only one method still receives a score.

    Returns a mapping of id to fused score, unsorted.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for index, chunk_id in enumerate(ranking):
            rank = index + 1
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


def _load_chunk_details(conn: Connection, chunk_ids: list[str]) -> dict[str, dict]:
    """Fetch document/page/heading/text details for a set of chunk ids.

    Takes no project filter of its own: `chunk_ids` always originates from
    `_vector_candidates`/`_text_candidates`, which are already filtered.
    """
    if not chunk_ids:
        return {}
    rows = conn.execute(
        "select c.id, c.document_id, c.page_from, c.page_to, c.heading_path, c.text, "
        "       d.filename "
        "from chunks c "
        "join documents d on d.id = c.document_id "
        "where c.id = any(%s)",
        (chunk_ids,),
    ).fetchall()
    return {str(row["id"]): row for row in rows}


def retrieve(
    conn: Connection,
    *,
    project_id: str,
    query_text: str,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Hybrid retrieval over one project's `ready`, non-deleted chunks.

    Embeds `query_text` with the same Titan client used at index time
    (Requirement 7.7), runs the vector and full-text candidate queries
    independently, fuses them with RRF, and returns the top `top_k` chunks
    (default `settings.retrieval_top_k`) ordered by descending fused score.
    """
    settings = get_settings()
    resolved_top_k = top_k if top_k is not None else settings.retrieval_top_k

    embedding_result = embed_text(query_text)

    vector_ranking = _vector_candidates(
        conn, project_id=project_id, query_vector=embedding_result.vector, limit=_CANDIDATE_POOL_SIZE
    )
    text_ranking = _text_candidates(
        conn, project_id=project_id, query_text=query_text, limit=_CANDIDATE_POOL_SIZE
    )

    fused = fuse_rankings([vector_ranking, text_ranking], k=settings.rrf_k)
    ranked_ids = sorted(fused.keys(), key=lambda chunk_id: fused[chunk_id], reverse=True)
    top_ids = ranked_ids[:resolved_top_k]

    details = _load_chunk_details(conn, top_ids)

    results: list[RetrievedChunk] = []
    for chunk_id in top_ids:
        row = details.get(chunk_id)
        if row is None:
            # A candidate could in principle vanish between the candidate
            # query and this lookup (concurrent delete). Skip rather than
            # error: the remaining ranked chunks are still a valid answer.
            continue
        results.append(
            RetrievedChunk(
                chunk_id=chunk_id,
                document_id=str(row["document_id"]),
                score=fused[chunk_id],
                filename=row["filename"],
                page_from=row["page_from"],
                page_to=row["page_to"],
                heading_path=row["heading_path"],
                text=row["text"],
            )
        )
    return results


__all__ = ["RetrievedChunk", "fuse_rankings", "retrieve"]
