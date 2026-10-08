"""Tests for chunk construction from parsed blocks.

Validates: Requirements 6.6, 8.2
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from app.chunking import ChunkDraft, build_chunks


def _block(
    text: str,
    *,
    page_no: int = 1,
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 100.0, 10.0),
    kind: str = "paragraph",
    heading_path: str | None = None,
) -> dict:
    return {
        "page_no": page_no,
        "text": text,
        "bbox": bbox,
        "kind": kind,
        "heading_path": heading_path,
    }


# --- unit tests --------------------------------------------------------------


def test_empty_blocks_yield_no_chunks():
    assert build_chunks([], target_tokens=350, overlap_tokens=60) == []


def test_simple_multi_block_document_forms_one_chunk_under_target():
    blocks = [
        _block("Reported NAV is $42.6m.", heading_path="Capital Account"),
        _block("Called capital stands at $10.0m.", heading_path="Capital Account"),
    ]
    chunks = build_chunks(blocks, target_tokens=350, overlap_tokens=60)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.ordinal == 1
    assert chunk.heading_path == "Capital Account"
    assert chunk.text.startswith("Capital Account\n\n")
    assert "Reported NAV is $42.6m." in chunk.text
    assert "Called capital stands at $10.0m." in chunk.text
    assert len(chunk.regions) == 2
    assert chunk.page_from == 1
    assert chunk.page_to == 1


def test_oversized_single_block_becomes_its_own_chunk():
    huge_text = "word " * 2000  # far more than 350 tokens at ~4 chars/token
    blocks = [
        _block("intro", heading_path="H"),
        _block(huge_text, heading_path="H"),
        _block("outro", heading_path="H"),
    ]
    chunks = build_chunks(blocks, target_tokens=350, overlap_tokens=60)

    # The oversized block must appear whole in exactly one chunk's text,
    # never truncated, even though it blows through the token target.
    matches = [c for c in chunks if huge_text in c.text]
    assert len(matches) == 1
    assert matches[0].token_count > 350


def test_heading_change_forces_new_chunk_below_target():
    blocks = [
        _block("short a", heading_path="Section One"),
        _block("short b", heading_path="Section Two"),
    ]
    chunks = build_chunks(blocks, target_tokens=350, overlap_tokens=60)

    assert len(chunks) == 2
    assert chunks[0].heading_path == "Section One"
    assert chunks[1].heading_path == "Section Two"
    assert chunks[0].ordinal == 1
    assert chunks[1].ordinal == 2


def test_overlap_carries_trailing_content_into_next_chunk():
    # Build enough same-heading blocks to force a target-exceeded split,
    # each ~12 tokens (48 chars / 4).
    filler = "x" * 44  # ~11 tokens
    blocks = [_block(f"block{i} {filler}", heading_path="H") for i in range(10)]
    chunks = build_chunks(blocks, target_tokens=40, overlap_tokens=15)

    assert len(chunks) >= 2
    # The last block's text that ended up in chunk N should also appear at
    # the start of chunk N+1's body (carried as overlap), for a boundary
    # that was NOT a heading change.
    first, second = chunks[0], chunks[1]
    last_block_of_first = None
    for i in range(10):
        candidate = f"block{i} {filler}"
        if candidate in first.text:
            last_block_of_first = candidate
    assert last_block_of_first is not None
    assert last_block_of_first in second.text


def test_overlap_does_not_cross_heading_change():
    blocks = [
        _block("alpha content here", heading_path="Section One"),
        _block("beta content here", heading_path="Section Two"),
    ]
    chunks = build_chunks(blocks, target_tokens=350, overlap_tokens=60)

    assert len(chunks) == 2
    # Nothing from Section One's body should leak into Section Two's chunk.
    assert "alpha content here" not in chunks[1].text


def test_chunk_regions_has_one_row_per_contributing_block():
    blocks = [
        _block("a", page_no=1, bbox=(0, 0, 10, 10), heading_path="H"),
        _block("b", page_no=1, bbox=(0, 10, 10, 20), heading_path="H"),
        _block("c", page_no=2, bbox=(0, 0, 10, 10), heading_path="H"),
    ]
    chunks = build_chunks(blocks, target_tokens=350, overlap_tokens=0)

    assert len(chunks) == 1
    regions = chunks[0].regions
    assert len(regions) == 3
    assert [(r.page_no, r.bbox) for r in regions] == [
        (1, (0, 0, 10, 10)),
        (1, (0, 10, 10, 20)),
        (2, (0, 0, 10, 10)),
    ]
    assert chunks[0].page_from == 1
    assert chunks[0].page_to == 2


def test_zero_overlap_carries_nothing():
    filler = "x" * 44
    blocks = [_block(f"block{i} {filler}", heading_path="H") for i in range(6)]
    chunks = build_chunks(blocks, target_tokens=20, overlap_tokens=0)

    assert len(chunks) >= 2
    # With zero overlap budget, no block should be duplicated into a second
    # chunk: total contributing region count across all chunks equals the
    # number of input blocks.
    total_regions = sum(len(c.regions) for c in chunks)
    assert total_regions == len(blocks)


# --- property tests -----------------------------------------------------------
# No numbered correctness property in design.md (P1-P39) covers chunk
# construction directly; these are exploratory Hypothesis tests validating
# the task's stated invariants against Requirements 6.6 and 8.2, not a
# formally numbered property. Flagged to the user rather than adding a new
# property to design.md without sign-off.

heading_st = st.one_of(st.none(), st.sampled_from(["Section A", "Section B", "Section C"]))


@st.composite
def _blocks_st(draw):
    n = draw(st.integers(min_value=1, max_value=15))
    blocks = []
    heading = draw(heading_st)
    for i in range(n):
        if draw(st.booleans()) and i > 0:
            heading = draw(heading_st)
        text = draw(st.text(alphabet=st.characters(whitelist_categories=("L", "N")), min_size=1, max_size=80))
        page_no = draw(st.integers(min_value=1, max_value=5))
        blocks.append(_block(text, page_no=page_no, heading_path=heading))
    return blocks


@settings(max_examples=20)
@given(blocks=_blocks_st(), target_tokens=st.integers(min_value=5, max_value=200), overlap_tokens=st.integers(min_value=0, max_value=50))
def test_every_block_contributes_at_least_one_region(blocks, target_tokens, overlap_tokens):
    """Validates: Requirements 6.6, 8.2

    For any list of blocks, every original block's text must appear in at
    least one chunk's text (overlap duplication across a boundary is
    expected and fine; dropping a block is not).
    """
    overlap_tokens = min(overlap_tokens, target_tokens - 1) if target_tokens > 1 else 0
    chunks = build_chunks(blocks, target_tokens=target_tokens, overlap_tokens=overlap_tokens)

    all_text = "\n\n".join(c.text for c in chunks)
    for block in blocks:
        assert block["text"] in all_text


@settings(max_examples=20)
@given(blocks=_blocks_st(), target_tokens=st.integers(min_value=5, max_value=200), overlap_tokens=st.integers(min_value=0, max_value=50))
def test_no_chunk_ever_truncates_a_blocks_text(blocks, target_tokens, overlap_tokens):
    """Validates: Requirements 6.6

    The "never split a block" invariant: every contributing block's full
    text appears intact (not a partial substring cut mid-block) in the
    chunk(s) it contributes to.
    """
    overlap_tokens = min(overlap_tokens, target_tokens - 1) if target_tokens > 1 else 0
    chunks = build_chunks(blocks, target_tokens=target_tokens, overlap_tokens=overlap_tokens)

    for chunk in chunks:
        # Rebuild the expected body from the regions' order is not directly
        # possible since regions don't carry text, so instead assert the
        # chunk's text is a concatenation of whole block texts joined by
        # "\n\n" (plus optional heading prefix) -- i.e. splitting on the
        # separator never yields a fragment that is a strict partial
        # prefix/suffix of an original block without being the whole block.
        body = chunk.text
        if chunk.heading_path:
            assert body.startswith(f"{chunk.heading_path}\n\n")
            body = body[len(chunk.heading_path) + 2 :]
        parts = body.split("\n\n")
        original_texts = {b["text"] for b in blocks}
        for part in parts:
            assert part in original_texts


@settings(max_examples=20)
@given(blocks=_blocks_st(), target_tokens=st.integers(min_value=5, max_value=200), overlap_tokens=st.integers(min_value=0, max_value=50))
def test_chunk_ordinals_are_sequential_from_one(blocks, target_tokens, overlap_tokens):
    overlap_tokens = min(overlap_tokens, target_tokens - 1) if target_tokens > 1 else 0
    chunks = build_chunks(blocks, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
    assert [c.ordinal for c in chunks] == list(range(1, len(chunks) + 1))


@settings(max_examples=20)
@given(blocks=_blocks_st(), target_tokens=st.integers(min_value=5, max_value=200), overlap_tokens=st.integers(min_value=0, max_value=50))
def test_page_from_to_bounds_match_contributing_regions(blocks, target_tokens, overlap_tokens):
    overlap_tokens = min(overlap_tokens, target_tokens - 1) if target_tokens > 1 else 0
    chunks = build_chunks(blocks, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
    for chunk in chunks:
        pages = [r.page_no for r in chunk.regions]
        assert chunk.page_from == min(pages)
        assert chunk.page_to == max(pages)
