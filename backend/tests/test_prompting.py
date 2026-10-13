"""Tests for prompt assembly, the slot map, and the placeholder mapping.

Validates: Requirements 7.2, 8.8, 16.12, 16.15
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

from app import prompting
from app.guardrails import GuardrailResult, MaskedOccurrence
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
    pii_masked: bool = False,
    original_text_enc: bytes | None = None,
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
        pii_masked=pii_masked,
        original_text_enc=original_text_enc,
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


# --- stored (already-masked) chunk text goes straight into the prompt ----
# (Requirement 16.3, 16.7, 16.8: masking happens once, at index time; this
# module no longer re-masks chunk.text -- it is written into the slot
# exactly as stored.)


def test_already_masked_chunk_text_is_written_unchanged_into_the_prompt():
    chunk = _chunk("chunk-1", text="Call the manager at {PHONE} for details.", pii_masked=True)
    result = assemble_prompt([chunk], "What is the contact number?")

    assert "Call the manager at {PHONE} for details." in result.prompt


# --- placeholder mapping construction (Requirement 16.12; task 8d.1) -----


def test_placeholder_mapping_empty_when_no_chunk_has_pii():
    chunk = _chunk("chunk-1", text="Ticker: AAPL", pii_masked=False)
    result = assemble_prompt([chunk], "Question?")

    assert result.placeholder_mapping.values_by_placeholder == {}


def test_placeholder_mapping_recovered_from_decrypted_original(monkeypatch):
    def _fake_decrypt(blob: bytes) -> str:
        assert blob == b"ENC(John Smith's email is john@example.com)"
        return "John Smith's email is john@example.com"

    def _fake_guardrail(text: str) -> GuardrailResult:
        assert text == "John Smith's email is john@example.com"
        return GuardrailResult(
            filtered_text="{NAME}'s email is {EMAIL}",
            pii_detected=True,
            occurrences=(
                MaskedOccurrence(placeholder="{NAME}", original="John Smith"),
                MaskedOccurrence(placeholder="{EMAIL}", original="john@example.com"),
            ),
        )

    monkeypatch.setattr(prompting, "decrypt_original_text", _fake_decrypt)
    monkeypatch.setattr(prompting, "check_guardrail", _fake_guardrail)

    chunk = _chunk(
        "chunk-1",
        text="{NAME}'s email is {EMAIL}",
        pii_masked=True,
        original_text_enc=b"ENC(John Smith's email is john@example.com)",
    )
    result = assemble_prompt([chunk], "Question?")

    assert result.placeholder_mapping.values_by_placeholder == {
        "{NAME}": ["John Smith"],
        "{EMAIL}": ["john@example.com"],
    }
    # The prompt itself still carries the stored masked text, never the
    # decrypted real text.
    assert "{NAME}'s email is {EMAIL}" in result.prompt
    assert "John Smith" not in result.prompt
    assert "john@example.com" not in result.prompt


def test_placeholder_mapping_skips_chunks_with_no_pii(monkeypatch):
    """A chunk with pii_masked=False never triggers decrypt/guardrail
    calls -- there's nothing to recover and no original_text_enc to read."""

    def _fail(*args, **kwargs):
        raise AssertionError("should not be called for a chunk with no PII")

    monkeypatch.setattr(prompting, "decrypt_original_text", _fail)
    monkeypatch.setattr(prompting, "check_guardrail", _fail)

    chunk = _chunk("chunk-1", text="Ticker: AAPL", pii_masked=False)
    result = assemble_prompt([chunk], "Question?")

    assert result.placeholder_mapping.values_by_placeholder == {}


# --- property-based test: P45-adjacent, repeated-placeholder ordering ----
# (full disambiguation property P47 covered in task 8d.2)

_pii_samples = ["jane.doe@example.com", "555-123-4567", "123-45-6789"]

_clean_text = st.text(
    alphabet=st.characters(whitelist_categories=["L", "N"], max_codepoint=0x2764),
    min_size=0,
    max_size=30,
).filter(lambda s: not any(sample in s for sample in _pii_samples))


def _fake_guardrail_for_sample(sample: str) -> GuardrailResult:
    return GuardrailResult(
        filtered_text=f"contact at {{EMAIL}}" if "@" in sample else "contact at {PHONE}",
        pii_detected=True,
        occurrences=(MaskedOccurrence(placeholder="{EMAIL}" if "@" in sample else "{PHONE}", original=sample),),
    )


@given(
    pii_sample=st.sampled_from(_pii_samples),
    num_other_chunks=st.integers(min_value=0, max_value=3),
)
@hyp_settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_placeholder_mapping_contains_recovered_value_per_pii_chunk(monkeypatch, pii_sample, num_other_chunks):
    """For any chunk flagged pii_masked, the placeholder mapping ends up
    containing that chunk's real value under the right placeholder key;
    chunks with no PII contribute nothing.

    **Validates: Requirements 16.12**
    """
    monkeypatch.setattr(prompting, "decrypt_original_text", lambda blob: pii_sample)
    monkeypatch.setattr(prompting, "check_guardrail", lambda text: _fake_guardrail_for_sample(pii_sample))

    pii_chunk = _chunk("chunk-pii", text="contact at {EMAIL}", pii_masked=True, original_text_enc=b"enc")
    other_chunks = [_chunk(f"chunk-{i}", text=f"clean passage {i}", pii_masked=False) for i in range(num_other_chunks)]
    chunks = [pii_chunk, *other_chunks]

    result = assemble_prompt(chunks, "Question?")

    placeholder = "{EMAIL}" if "@" in pii_sample else "{PHONE}"
    assert pii_sample in result.placeholder_mapping.values_by_placeholder[placeholder]


def test_property_prompt_never_contains_decrypted_real_value(monkeypatch):
    """The prompt sent to Claude is built from chunk.text only -- the
    decrypted original is used solely to populate the mapping, never
    written into the prompt string.

    **Validates: Requirements 16.3, 16.8**
    """
    real_value = "super-secret-real-value@example.com"
    monkeypatch.setattr(prompting, "decrypt_original_text", lambda blob: real_value)
    monkeypatch.setattr(
        prompting,
        "check_guardrail",
        lambda text: GuardrailResult(
            filtered_text="irrelevant",
            pii_detected=True,
            occurrences=(MaskedOccurrence(placeholder="{EMAIL}", original=real_value),),
        ),
    )

    chunk = _chunk("chunk-1", text="contact at {EMAIL}", pii_masked=True, original_text_enc=b"enc")
    result = assemble_prompt([chunk], "Question?")

    assert real_value not in result.prompt


def test_mapping_recovery_failure_propagates(monkeypatch):
    """If decrypting or re-checking a chunk's original text fails, the
    error propagates rather than being swallowed -- a request should not
    silently proceed with an incomplete mapping (mirrors P44's fail-closed
    stance from the index-time guardrail)."""

    class _DecryptionFailed(RuntimeError):
        pass

    def _always_raise(blob: bytes) -> str:
        raise _DecryptionFailed("KMS unreachable")

    monkeypatch.setattr(prompting, "decrypt_original_text", _always_raise)

    chunk = _chunk("chunk-1", text="contact at {EMAIL}", pii_masked=True, original_text_enc=b"enc")

    with pytest.raises(_DecryptionFailed):
        assemble_prompt([chunk], "Question?")


# --- property-based test: P47 (task 8d.2) ----------------------------------


@given(real_values=st.lists(st.emails(), min_size=1, max_size=4, unique=True))
@hyp_settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_repeated_placeholder_disambiguation(monkeypatch, real_values):
    """P47: for any set of chunks each contributing one real value for the
    same placeholder type, the placeholder mapping lists them in prompt
    (slot) order -- the i-th chunk's value lands at the i-th position,
    which is what later lets substitution assign the i-th occurrence of
    the placeholder in the model's response to the i-th chunk's value.

    **Validates: Requirements 16.15**
    """

    def _fake_decrypt_for_chunk(blob: bytes) -> str:
        # Each chunk's "encrypted" blob is just its index as bytes, in
        # this test, so decrypt can look up which real value it owns.
        index = int(blob.decode("utf-8"))
        return f"contact: {real_values[index]}"

    def _fake_guardrail(text: str) -> GuardrailResult:
        # text is "contact: {email}" (the decrypted original); recover
        # exactly that email as the occurrence.
        email = text.removeprefix("contact: ")
        return GuardrailResult(
            filtered_text="contact: {EMAIL}",
            pii_detected=True,
            occurrences=(MaskedOccurrence(placeholder="{EMAIL}", original=email),),
        )

    monkeypatch.setattr(prompting, "decrypt_original_text", _fake_decrypt_for_chunk)
    monkeypatch.setattr(prompting, "check_guardrail", _fake_guardrail)

    chunks = [
        _chunk(
            f"chunk-{i}",
            text="contact: {EMAIL}",
            pii_masked=True,
            original_text_enc=str(i).encode("utf-8"),
        )
        for i in range(len(real_values))
    ]

    result = assemble_prompt(chunks, "Question?")

    assert result.placeholder_mapping.values_by_placeholder["{EMAIL}"] == real_values


# --- substitute_originals (Requirement 16.13, 16.14; task 8d.3) ----------


def test_substitute_originals_replaces_single_placeholder():
    from app.prompting import PlaceholderMapping, substitute_originals

    mapping = PlaceholderMapping(values_by_placeholder={"{NAME}": ["John Smith"]})
    result = substitute_originals("The contact is {NAME}.", mapping)

    assert result == "The contact is John Smith."


def test_substitute_originals_replaces_multiple_distinct_placeholders():
    from app.prompting import PlaceholderMapping, substitute_originals

    mapping = PlaceholderMapping(
        values_by_placeholder={"{NAME}": ["John Smith"], "{EMAIL}": ["john@example.com"]}
    )
    result = substitute_originals("Contact {NAME} at {EMAIL}.", mapping)

    assert result == "Contact John Smith at john@example.com."


def test_substitute_originals_pairs_repeated_placeholder_positionally():
    from app.prompting import PlaceholderMapping, substitute_originals

    mapping = PlaceholderMapping(values_by_placeholder={"{EMAIL}": ["first@example.com", "second@example.com"]})
    result = substitute_originals("Reach {EMAIL} or {EMAIL}.", mapping)

    assert result == "Reach first@example.com or second@example.com."


def test_substitute_originals_leaves_unmapped_placeholder_unchanged():
    from app.prompting import PlaceholderMapping, substitute_originals

    mapping = PlaceholderMapping(values_by_placeholder={})
    result = substitute_originals("The contact is {NAME}.", mapping)

    assert result == "The contact is {NAME}."


def test_substitute_originals_leaves_excess_occurrence_unchanged():
    """More occurrences in the response than real values recorded -- the
    extra occurrence is left as-is, not guessed."""
    from app.prompting import PlaceholderMapping, substitute_originals

    mapping = PlaceholderMapping(values_by_placeholder={"{EMAIL}": ["only@example.com"]})
    result = substitute_originals("Reach {EMAIL} or {EMAIL}.", mapping)

    assert result == "Reach only@example.com or {EMAIL}."


def test_substitute_originals_does_not_raise_on_unmapped_or_excess():
    from app.prompting import PlaceholderMapping, substitute_originals

    mapping = PlaceholderMapping(values_by_placeholder={})
    # Should not raise even though nothing in the mapping can satisfy this.
    substitute_originals("{NAME} {EMAIL} {PHONE} {NAME}", mapping)


def test_substitute_originals_text_with_no_placeholders_is_unchanged():
    from app.prompting import PlaceholderMapping, substitute_originals

    mapping = PlaceholderMapping(values_by_placeholder={"{NAME}": ["John Smith"]})
    result = substitute_originals("No placeholders here at all.", mapping)

    assert result == "No placeholders here at all."


# --- property-based test: P46 (task 8d.4) ----------------------------------


@given(
    placeholder_counts=st.dictionaries(
        keys=st.sampled_from(["{NAME}", "{EMAIL}", "{PHONE}"]),
        values=st.integers(min_value=1, max_value=3),
        min_size=1,
        max_size=3,
    )
)
def test_property_substitution_round_trip(placeholder_counts):
    """P46: substitution replaces every mapped occurrence with its real
    value, leaves every unmapped placeholder-shaped occurrence unchanged,
    never raises, and a second pass over the already-substituted result
    is a no-op (since real values never happen to look like placeholders
    in this test's construction).

    **Validates: Requirements 16.13, 16.14**
    """
    from app.prompting import PlaceholderMapping, substitute_originals

    mapping = PlaceholderMapping(
        values_by_placeholder={
            placeholder: [f"real-value-{placeholder.strip('{}')}-{i}" for i in range(count)]
            for placeholder, count in placeholder_counts.items()
        }
    )

    # Build a response using each placeholder exactly as many times as it
    # has mapped values, plus one unmapped placeholder for variety.
    response_parts: list[str] = []
    for placeholder, count in placeholder_counts.items():
        response_parts.extend([placeholder] * count)
    response_parts.append("{UNMAPPED}")
    response_text = " ".join(response_parts)

    substituted = substitute_originals(response_text, mapping)

    # Every mapped value appears in the result.
    for values in mapping.values_by_placeholder.values():
        for value in values:
            assert value in substituted

    # The unmapped placeholder is untouched.
    assert "{UNMAPPED}" in substituted

    # No raw placeholder from the mapping's own keys remains (all were
    # consumed, since the response used each exactly `count` times).
    for placeholder in mapping.values_by_placeholder:
        assert placeholder not in substituted

    # Idempotent: a second pass changes nothing further, since the real
    # values substituted in don't themselves look like `{WORD}` tokens.
    second_pass = substitute_originals(substituted, mapping)
    assert second_pass == substituted
