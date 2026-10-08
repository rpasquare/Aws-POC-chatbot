"""Tests for prompt assembly and the slot map.

Validates: Requirements 7.2, 8.8
"""

from __future__ import annotations

from app.prompting import assemble_prompt
from app.retrieval import RetrievedChunk


def _chunk(
    chunk_id: str,
    *,
    document_id: str = "doc-1",
    score: float = 1.0,
    filename: str = "report.pdf",
    page_from: int = 1,
    page_to: int = 1,
    heading_path: str | None = "Section One",
    text: str = "The reported NAV is $42.6m.",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        score=score,
        filename=filename,
        page_from=page_from,
        page_to=page_to,
        heading_path=heading_path,
        text=text,
    )


def test_single_passage_numbered_one_with_filename_page_and_heading():
    chunk = _chunk("chunk-1", filename="report.pdf", page_from=3, heading_path="Capital Account")
    result = assemble_prompt([chunk], "What is the NAV?")

    assert "[1] report.pdf, page 3" in result.prompt
    assert "Capital Account" in result.prompt
    assert "The reported NAV is $42.6m." in result.prompt
    assert result.slot_map == {1: "chunk-1"}


def test_two_passages_numbered_in_given_order():
    chunks = [
        _chunk("chunk-a", filename="a.pdf", page_from=1),
        _chunk("chunk-b", filename="b.pdf", page_from=5),
    ]
    result = assemble_prompt(chunks, "Question?")

    assert "[1] a.pdf, page 1" in result.prompt
    assert "[2] b.pdf, page 5" in result.prompt
    # [1] must appear before [2] in the assembled text.
    assert result.prompt.index("[1] a.pdf") < result.prompt.index("[2] b.pdf")
    assert result.slot_map == {1: "chunk-a", 2: "chunk-b"}


def test_three_passages_slot_map_maps_each_number_to_correct_chunk_id():
    chunks = [
        _chunk("chunk-x", filename="x.pdf"),
        _chunk("chunk-y", filename="y.pdf"),
        _chunk("chunk-z", filename="z.pdf"),
    ]
    result = assemble_prompt(chunks, "Question?")

    assert result.slot_map == {1: "chunk-x", 2: "chunk-y", 3: "chunk-z"}
    for number, filename in [(1, "x.pdf"), (2, "y.pdf"), (3, "z.pdf")]:
        assert f"[{number}] {filename}" in result.prompt


def test_passage_without_heading_path_omits_heading_line():
    chunk = _chunk("chunk-1", heading_path=None, text="Some body text.")
    result = assemble_prompt([chunk], "Question?")

    assert "None" not in result.prompt
    # The passage line and the body text should be adjacent (no heading
    # line printed between them).
    lines = result.prompt.splitlines()
    passage_line_index = next(i for i, line in enumerate(lines) if line.startswith("[1]"))
    assert "Some body text." in lines[passage_line_index + 1]


def test_fixed_instruction_block_present():
    result = assemble_prompt([_chunk("chunk-1")], "Question?")

    assert "Answer using ONLY the passages below." in result.prompt
    assert "Cite each fact with [n] matching" in result.prompt
    assert "documents do not cover it." in result.prompt
    assert "Do not estimate." in result.prompt


def test_instructions_tell_the_model_not_to_state_page_numbers():
    # The model's own prose is never trusted for provenance -- only the
    # [n] marker is resolved to a real page via the slot map. Telling the
    # model not to restate a page number in words avoids it writing an
    # unverified (and potentially wrong) page number alongside a correct
    # citation marker.
    result = assemble_prompt([_chunk("chunk-1")], "Question?")

    assert "Do not state a page number" in result.prompt


def test_question_present_at_end():
    result = assemble_prompt([_chunk("chunk-1")], "What is the NAV?")

    assert "Question: What is the NAV?" in result.prompt
    assert result.prompt.rstrip().endswith("Question: What is the NAV?")


def test_empty_chunk_list_still_produces_valid_prompt():
    result = assemble_prompt([], "What is the NAV?")

    assert "Answer using ONLY the passages below." in result.prompt
    assert "Question: What is the NAV?" in result.prompt
    assert "[1]" not in result.prompt
    assert result.slot_map == {}


def test_chunk_spanning_multiple_pages_cited_by_starting_page():
    # page_from != page_to: the design doc's template literally uses
    # page_from, so a chunk spanning pages 4-5 is still cited as "page 4".
    chunk = _chunk("chunk-1", filename="report.pdf", page_from=4, page_to=5)
    result = assemble_prompt([chunk], "Question?")

    assert "[1] report.pdf, page 4" in result.prompt
    assert "page 5" not in result.prompt
