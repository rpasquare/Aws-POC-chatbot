"""Prompt assembly for grounded answer generation.

Pure function, no DB or network access, so it can be unit tested in
isolation and called from the eventual answer handler (task 6.4) just by
passing it the chunks already returned by `app.retrieval.retrieve`.

Design doc, "Prompt assembly" section, lays out the fixed shape:

    Answer using ONLY the passages below. Cite each fact with [n] matching
    the passage number. If the passages do not contain the answer, say the
    documents do not cover it. Do not estimate.

    [1] {filename}, page {page_from}
        {heading_path}
        {chunk_text}

    [2] ...

    Question: {question}

Passages are numbered 1-indexed in the order the caller's chunk list is
given — that order is retrieval's fused-score order (best first), and this
module does not re-sort or otherwise interpret it.

Citations must be trustworthy (design doc, top-level constraints): the
model's only output obligation is the bracketed integers, and "[n]" is
resolved back to a chunk id via the slot map, never by parsing a filename
or page number out of generated text. This module builds that slot map —
`dict[int, str]` from passage number to `chunk_id` — but does not persist
it anywhere. The design doc is explicit that "the handler holds an
in-memory map from slot number to chunk id for the duration of the
request"; this function returns that map for the caller to hold, satisfying
Requirement 8.8. Extracting markers from the model's completed answer and
writing `message_citations` rows from this map is task 6.6, out of scope
here.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.retrieval import RetrievedChunk

_INSTRUCTIONS = (
    "Answer using ONLY the passages below. Cite each fact with [n] matching\n"
    "the passage number. If the passages do not contain the answer, say the\n"
    "documents do not cover it. Do not estimate. Do not state a page number "
    "in your own words -- the [n] marker alone identifies the source; the "
    "application resolves it to the correct page independently."
)


@dataclass(frozen=True)
class PromptAssembly:
    """The assembled prompt string plus the slot map that resolves it.

    `slot_map` maps 1-based passage number to `chunk_id`, mirroring the
    numbering actually printed in `prompt`. The caller (a future answer
    handler) holds this dict in memory for the duration of the request and
    uses it in task 6.6 to resolve markers found in the model's answer back
    to a chunk id, never from the generated text itself.
    """

    prompt: str
    slot_map: dict[int, str]


def _format_passage(number: int, chunk: RetrievedChunk) -> str:
    """Render one numbered passage block.

    Template, per the design doc, literally uses `page {page_from}`: a
    chunk spanning multiple pages (`page_from != page_to`) is still cited
    by its starting page only, not a range. The heading line is omitted
    entirely when `heading_path` is absent, rather than printing a
    placeholder like "None".
    """
    lines = [f"[{number}] {chunk.filename}, page {chunk.page_from}"]
    if chunk.heading_path:
        lines.append(f"    {chunk.heading_path}")
    lines.append(f"    {chunk.text}")
    return "\n".join(lines)


def assemble_prompt(chunks: list[RetrievedChunk], question: str) -> PromptAssembly:
    """Build the answer-model prompt and its slot map from retrieved chunks.

    `chunks` is expected in retrieval's fused-score order (best first);
    passages are numbered 1-indexed in that order, unchanged. An empty
    chunk list still produces a valid prompt — the instruction block and
    the question, with no passages in between — rather than erroring,
    since the refusal path (task 6.2) decides whether to call the model at
    all; this function does not make that decision.
    """
    passages: list[str] = []
    slot_map: dict[int, str] = {}

    for index, chunk in enumerate(chunks):
        number = index + 1
        passages.append(_format_passage(number, chunk))
        slot_map[number] = chunk.chunk_id

    sections = [_INSTRUCTIONS]
    sections.extend(passages)
    sections.append(f"Question: {question}")

    prompt = "\n\n".join(sections)
    return PromptAssembly(prompt=prompt, slot_map=slot_map)


__all__ = ["PromptAssembly", "assemble_prompt"]
