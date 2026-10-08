"""Marker extraction and citation recording.

Design doc, "Citation recording" section: after the answer completes,
markers are extracted from the prose by pattern match. For each marker
found, a `message_citations` row is written using the slot map to resolve
marker to chunk id, and the chunk's `document_id`. Markers referring to
slots that were not supplied are discarded rather than stored, so a model
that invents `[9]` when eight passages were given produces no citation
rather than a broken one (Requirements 8.1, 8.2, 8.8).

Chunk id -> document id lookup: `app.prompting.PromptAssembly.slot_map` is
deliberately just `dict[int, str]` (slot number to `chunk_id`) — that
module's docstring is explicit that it stays focused on prompt text and
does not persist or enrich that mapping. Rather than widening the slot
map's shape (which would make `prompting.py` carry citation-recording
concerns it doesn't need), this module takes the `chunks: list[RetrievedChunk]`
that produced the slot map and builds its own `chunk_id -> document_id`
lookup internally. The caller already has both the slot map and the
original chunk list in memory for the request (per the design doc, "the
handler holds an in-memory map ... for the duration of the request"), so
passing the chunk list alongside the slot map costs nothing extra and
keeps each module's responsibility singular.
"""

from __future__ import annotations

import re

from psycopg import Connection

from app.retrieval import RetrievedChunk

_MARKER_PATTERN = re.compile(r"\[(\d+)\]")


def extract_markers(answer_text: str) -> list[int]:
    """Find every bracketed integer marker in the prose, in order.

    Pattern match only, per the design doc — no attempt is made to
    interpret surrounding text. Duplicates are preserved here (e.g. the
    same marker cited twice yields two entries); callers that need one row
    per distinct marker dedupe separately.
    """
    return [int(match.group(1)) for match in _MARKER_PATTERN.finditer(answer_text)]


def record_citations(
    conn: Connection,
    *,
    message_id: str,
    answer_text: str,
    slot_map: dict[int, str],
    chunks: list[RetrievedChunk],
) -> None:
    """Extract markers from `answer_text` and write `message_citations` rows.

    Each marker is resolved through `slot_map` (passage number -> chunk
    id). A marker with no entry in `slot_map` is discarded silently — not
    an error, just no citation for that number, per the design doc's
    "markers referring to slots that were not supplied are discarded
    rather than stored."

    `chunks` provides the `document_id` for each valid chunk id, via an
    internal lookup built here (see module docstring).

    A marker repeated multiple times in the prose produces exactly one
    row: markers are deduplicated before inserting, so this is safe
    against the `unique (message_id, marker)` constraint on
    `message_citations` even when the model cites `[1]` more than once.
    """
    document_id_by_chunk_id = {chunk.chunk_id: chunk.document_id for chunk in chunks}

    markers = extract_markers(answer_text)
    distinct_valid_markers = sorted({marker for marker in markers if marker in slot_map})

    for marker in distinct_valid_markers:
        chunk_id = slot_map[marker]
        document_id = document_id_by_chunk_id.get(chunk_id)
        conn.execute(
            "insert into message_citations (message_id, marker, chunk_id, document_id) "
            "values (%s, %s, %s, %s)",
            (message_id, marker, chunk_id, document_id),
        )


__all__ = ["extract_markers", "record_citations"]
