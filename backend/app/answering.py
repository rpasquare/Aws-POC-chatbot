"""The relevance threshold and refusal path.

Design doc, "Refusal path" section: if the best fused score falls below
`RELEVANCE_THRESHOLD`, the system returns the refusal response, records the
message with `grounded = false`, writes no citation rows, and does not call
the answer model at all (Requirements 7.3, 7.4).

`should_refuse` is a small, pure decision function: given the chunks already
returned by `app.retrieval.retrieve` (sorted descending by fused score, see
that module), it answers only "is there enough to ground an answer in" —
nothing else. It does not call the answer model, does not touch the
database, and does not know about chat messages. That separation is what
lets task 6.4's eventual answer-calling code depend on this function's
result without depending on anything else here: the caller is expected to
call `should_refuse` first and only proceed to the answer model when it
returns `False`.

`record_refusal_message` is the one piece of "recording the message with
grounded = false" that has an obvious home already: inserting the single
`chat_messages` row for a refusal is a one-statement operation, not an
endpoint. It does not write anything to `message_citations` — refusals
structurally never do, because nothing here ever calls the citation
recording code path (task 6.6). Building the full
`POST /api/conversations/{id}/messages` handler — reading the question,
calling retrieval, branching on `should_refuse`, and otherwise calling the
answer model and streaming its output — remains task 6.10/6.12.
"""

from __future__ import annotations

from psycopg import Connection

from app.config import get_settings
from app.retrieval import RetrievedChunk

REFUSAL_TEXT = (
    "The project's documents do not cover this question, so I can't give you "
    "a grounded answer. Try rephrasing, or ask about something covered in the "
    "uploaded documents."
)
"""Requirement 7.3's exact wording: state that the project's documents do
not cover the question, and do not generate a speculative answer."""


def should_refuse(chunks: list[RetrievedChunk], *, threshold: float | None = None) -> bool:
    """Decide whether retrieval found enough to ground an answer in.

    Refuses when `chunks` is empty — there is nothing to ground an answer
    in — or when the best (first, since `retrieve` sorts descending by
    fused score) score is strictly below `threshold`. A score exactly equal
    to the threshold does NOT refuse: the design doc says the score "falls
    below" the threshold, so the boundary itself still counts as relevant.

    `threshold` defaults to `settings.relevance_threshold`; a caller may
    override it (e.g. in tests) without needing to patch configuration.
    """
    if not chunks:
        return True

    resolved_threshold = threshold if threshold is not None else get_settings().relevance_threshold
    best_score = chunks[0].score
    return best_score < resolved_threshold


def record_refusal_message(conn: Connection, *, session_id: str) -> str:
    """Insert the refusal's `chat_messages` row and return its id.

    `role='assistant'`, `grounded=false`, `author_id=null` (assistant
    messages are never authored by a user), and the fixed `REFUSAL_TEXT`.
    No `message_citations` rows are written here or anywhere else in this
    module — Requirement 7.4 holds structurally because the citation
    recording code path (task 6.6) is simply never invoked on this path.
    """
    row = conn.execute(
        "insert into chat_messages (session_id, role, text, author_id, grounded) "
        "values (%s, 'assistant', %s, null, false) "
        "returning id",
        (session_id, REFUSAL_TEXT),
    ).fetchone()
    return str(row["id"])


__all__ = ["REFUSAL_TEXT", "should_refuse", "record_refusal_message"]
