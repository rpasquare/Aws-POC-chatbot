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

PII guardrail (Requirement 16.3, 16.8): the text written into each
numbered slot is always `chunk.text` as already stored — already masked
by the index stage (`app/index.py`), never re-masked here. What this
module adds on top (Requirement 16.12, 16.15) is the *placeholder
mapping* needed to restore real values into the model's answer later: for
every chunk whose `pii_masked` is true, its `original_text_enc` is
decrypted and passed through `check_guardrail` once, purely to recover
that chunk's `occurrences` (the real values Bedrock masked). That
guardrail call's `filtered_text` is discarded — the prompt never uses it,
since `chunk.text` is already the correct masked text to send. The
decrypted real text itself is held only for the duration of that one call
and never written into the prompt, logged, or returned to the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.guardrails import MaskedOccurrence, check_guardrail
from app.pii_crypto import decrypt_original_text
from app.retrieval import RetrievedChunk

_INSTRUCTIONS = (
    "Answer using ONLY the passages below. Cite each fact with [n] matching\n"
    "the passage number. If the passages do not contain the answer, say the\n"
    "documents do not cover it. Do not estimate. Do not state a page number "
    "in your own words -- the [n] marker alone identifies the source; the "
    "application resolves it to the correct page independently.\n\n"
    "Some passages contain tokens like {NAME}, {EMAIL}, {PHONE}, {ADDRESS} "
    "in place of personal information. Treat each such token as if it were "
    "the real value it stands for -- it IS the answer, not a sign that the "
    "information is missing. For example, if a passage reads 'contact is "
    "{NAME}', answer the question as 'The contact is {NAME} [n]', writing "
    "the token itself into your answer exactly as it appears, with no "
    "quotation marks or extra commentary about it being a placeholder. "
    "Only say the documents do not cover something if no passage addresses "
    "it at all -- a masked token is coverage, not an absence."
)


_PLACEHOLDER_PATTERN = re.compile(r"\{[A-Z_]+\}")


@dataclass(frozen=True)
class PlaceholderMapping:
    """Scoped to one answer request, held in memory only, never persisted
    (Requirement 16.12).

    `values_by_placeholder` maps a placeholder token (e.g. "{EMAIL}") to
    the list of real values it stood for, ordered by the chunk's position
    in the prompt (slot order), then by order within that chunk's own
    `occurrences`. This ordering is what resolves Requirement 16.15 when
    the same placeholder recurs across different chunks: a later
    substitution pass pairs the n-th occurrence of a placeholder in the
    model's response with the n-th entry in this list.
    """

    values_by_placeholder: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptAssembly:
    """The assembled prompt string plus the slot map that resolves it.

    `slot_map` maps 1-based passage number to `chunk_id`, mirroring the
    numbering actually printed in `prompt`. The caller (a future answer
    handler) holds this dict in memory for the duration of the request and
    uses it in task 6.6 to resolve markers found in the model's answer back
    to a chunk id, never from the generated text itself.

    `placeholder_mapping` is the Requirement 16.12 mapping used after the
    model responds to restore real values into the answer (task 8d.3).
    """

    prompt: str
    slot_map: dict[int, str]
    placeholder_mapping: PlaceholderMapping


def _format_passage(number: int, chunk: RetrievedChunk) -> str:
    """Render one numbered passage block.

    Template, per the design doc, literally uses `page {page_from}`: a
    chunk spanning multiple pages (`page_from != page_to`) is still cited
    by its starting page only, not a range. The heading line is omitted
    entirely when `heading_path` is absent, rather than printing a
    placeholder like "None".

    `chunk.text` is written into the slot exactly as stored -- already
    masked by the index stage, never re-masked here (see module
    docstring).
    """
    lines = [f"[{number}] {chunk.filename}, page {chunk.page_from}"]
    if chunk.heading_path:
        lines.append(f"    {chunk.heading_path}")
    lines.append(f"    {chunk.text}")
    return "\n".join(lines)


def _recover_occurrences(chunk: RetrievedChunk) -> tuple[MaskedOccurrence, ...]:
    """For a chunk with `pii_masked = True`, decrypt its original text and
    recover the real values the index-stage guardrail masked.

    Returns `()` when the chunk has no PII (nothing to recover) or when
    `original_text_enc` is unexpectedly absent despite `pii_masked` being
    true (defensive: treated as nothing recoverable rather than raising,
    so a request-time surprise here degrades to "cannot restore this
    chunk's values" rather than failing the whole answer).
    """
    if not chunk.pii_masked or chunk.original_text_enc is None:
        return ()
    original_text = decrypt_original_text(chunk.original_text_enc)
    return check_guardrail(original_text).occurrences


def _build_placeholder_mapping(chunks: list[RetrievedChunk]) -> PlaceholderMapping:
    """Collect every chunk's recovered occurrences, in prompt (slot)
    order, then in-chunk order, into one mapping (Requirement 16.12,
    16.15).
    """
    values_by_placeholder: dict[str, list[str]] = {}
    for chunk in chunks:
        for occurrence in _recover_occurrences(chunk):
            values_by_placeholder.setdefault(occurrence.placeholder, []).append(occurrence.original)
    return PlaceholderMapping(values_by_placeholder=values_by_placeholder)


def assemble_prompt(chunks: list[RetrievedChunk], question: str) -> PromptAssembly:
    """Build the answer-model prompt, its slot map, and its placeholder
    mapping from retrieved chunks.

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
    placeholder_mapping = _build_placeholder_mapping(chunks)
    return PromptAssembly(prompt=prompt, slot_map=slot_map, placeholder_mapping=placeholder_mapping)


def substitute_originals(response_text: str, mapping: PlaceholderMapping) -> str:
    """Replace each masked placeholder occurrence in `response_text` with
    the real value it stood for (Requirement 16.13).

    Walks `response_text` left to right; for each placeholder-shaped token
    found (e.g. "{EMAIL}"), consumes the next not-yet-used real value from
    `mapping.values_by_placeholder[placeholder]`, in order -- the same
    positional pairing `PlaceholderMapping` is built with (Requirement
    16.15). A placeholder occurrence with no remaining mapped value (more
    occurrences in the response than real values recorded, or a
    placeholder-shaped token the mapping never saw at all) is left
    unchanged in the output rather than guessed or raised as an error
    (Requirement 16.14).
    """
    next_index_by_placeholder: dict[str, int] = {}

    def _replace(match: re.Match[str]) -> str:
        placeholder = match.group(0)
        values = mapping.values_by_placeholder.get(placeholder)
        if not values:
            return placeholder
        index = next_index_by_placeholder.get(placeholder, 0)
        if index >= len(values):
            return placeholder
        next_index_by_placeholder[placeholder] = index + 1
        return values[index]

    return _PLACEHOLDER_PATTERN.sub(_replace, response_text)


__all__ = ["PlaceholderMapping", "PromptAssembly", "assemble_prompt", "substitute_originals"]
