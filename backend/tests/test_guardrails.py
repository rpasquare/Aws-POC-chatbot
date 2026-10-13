"""Tests for the Bedrock guardrail client.

Validates: Requirements 16.1, 16.4, 16.5, 16.6, 16.9, 16.10, 16.11
"""

from __future__ import annotations

import re

import pytest
from botocore.exceptions import ClientError
from hypothesis import given
from hypothesis import strategies as st

from app import guardrails
from app.config import get_settings

settings = get_settings()


# --- fake Bedrock client (task 8.4) -------------------------------------

# Simple pattern-based masking per entity type, standing in for Bedrock's
# real ANONYMIZE behaviour so `check_guardrail` and its callers can be
# tested deterministically without the real API.
_ENTITY_PATTERNS: dict[str, re.Pattern[str]] = {
    "NAME": re.compile(r"\bJohn Smith\b"),
    "EMAIL": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "PHONE": re.compile(r"\b\d{3}-\d{3}-\d{4}\b"),
    "ADDRESS": re.compile(r"\b123 Main Street\b"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CREDIT_CARD": re.compile(r"\b4111-1111-1111-1111\b"),
    "BANK_ACCOUNT": re.compile(r"\b\d{9,12}\b"),
}


class _FakeGuardrailClient:
    """Stands in for the boto3 bedrock-runtime client's `apply_guardrail`.

    Masks any configured entity pattern found in the text and reports
    `GUARDRAIL_INTERVENED`/`NONE` accordingly. `always_raise`, if given, is
    raised on every call instead — for testing the exhausted-retry path.
    """

    def __init__(self, *, always_raise: Exception | None = None):
        self._always_raise = always_raise
        self.calls: list[dict] = []

    def apply_guardrail(self, *, guardrailIdentifier, guardrailVersion, source, content):
        self.calls.append(
            {
                "guardrailIdentifier": guardrailIdentifier,
                "guardrailVersion": guardrailVersion,
                "source": source,
                "content": content,
            }
        )
        if self._always_raise is not None:
            raise self._always_raise

        text = content[0]["text"]["text"]
        filtered = text
        intervened = False
        pii_entities = []
        # Walk the text left to right so entities land in the same order
        # the real API returns them (matching placeholder order).
        for entity_type, pattern in _ENTITY_PATTERNS.items():
            for match in pattern.finditer(text):
                intervened = True
                pii_entities.append({"type": entity_type, "match": match.group(0), "detected": True})
        for entity_type, pattern in _ENTITY_PATTERNS.items():
            filtered = pattern.sub(f"{{{entity_type}}}", filtered)

        return {
            "action": "GUARDRAIL_INTERVENED" if intervened else "NONE",
            "outputs": [{"text": filtered}],
            "assessments": [{"sensitiveInformationPolicy": {"piiEntities": pii_entities}}] if intervened else [],
        }


def _throttling_error():
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "ApplyGuardrail",
    )


def _other_client_error():
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad input"}},
        "ApplyGuardrail",
    )


# --- unit tests ----------------------------------------------------------


def test_check_guardrail_masks_detected_pii(monkeypatch):
    fake = _FakeGuardrailClient()
    monkeypatch.setattr(guardrails, "client", lambda: fake)

    result = guardrails.check_guardrail("Contact John Smith at john@example.com")

    assert result.pii_detected is True
    assert "John Smith" not in result.filtered_text
    assert "john@example.com" not in result.filtered_text
    assert fake.calls[0]["guardrailIdentifier"] == settings.bedrock_guardrail_id
    assert fake.calls[0]["guardrailVersion"] == settings.bedrock_guardrail_version
    assert fake.calls[0]["source"] == "OUTPUT"


def test_check_guardrail_passes_through_text_with_no_pii(monkeypatch):
    fake = _FakeGuardrailClient()
    monkeypatch.setattr(guardrails, "client", lambda: fake)

    result = guardrails.check_guardrail("the net asset value is $42.6m")

    assert result.pii_detected is False
    assert result.filtered_text == "the net asset value is $42.6m"


def test_check_guardrail_uses_configured_identifier_and_version(monkeypatch):
    """Proves the guardrail id/version are read from configuration, not
    hard-coded (Requirement 16.9, 16.10)."""
    fake = _FakeGuardrailClient()
    monkeypatch.setattr(guardrails, "client", lambda: fake)

    guardrails.check_guardrail("hello")

    assert fake.calls[0]["guardrailIdentifier"] == settings.bedrock_guardrail_id
    assert fake.calls[0]["guardrailVersion"] == settings.bedrock_guardrail_version


def test_throttling_is_retried_and_eventually_succeeds(monkeypatch):
    calls = {"n": 0}

    class _ThrottleThenSucceed(_FakeGuardrailClient):
        def apply_guardrail(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _throttling_error()
            return super().apply_guardrail(**kwargs)

    fake = _ThrottleThenSucceed()
    monkeypatch.setattr(guardrails, "client", lambda: fake)
    sleeps: list[float] = []
    monkeypatch.setattr(guardrails.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = guardrails.check_guardrail("retried text")

    assert result.filtered_text == "retried text"
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_throttling_exhausts_retries_and_raises(monkeypatch):
    fake = _FakeGuardrailClient(always_raise=_throttling_error())
    monkeypatch.setattr(guardrails, "client", lambda: fake)
    monkeypatch.setattr(guardrails.time, "sleep", lambda seconds: None)

    with pytest.raises(ClientError):
        guardrails.check_guardrail("never succeeds")

    assert len(fake.calls) == guardrails._THROTTLE_MAX_ATTEMPTS


def test_non_throttling_client_error_propagates_without_retry(monkeypatch):
    fake = _FakeGuardrailClient(always_raise=_other_client_error())
    monkeypatch.setattr(guardrails, "client", lambda: fake)
    monkeypatch.setattr(guardrails.time, "sleep", lambda seconds: None)

    with pytest.raises(ClientError) as exc_info:
        guardrails.check_guardrail("bad input text")

    assert exc_info.value.response["Error"]["Code"] == "ValidationException"
    assert len(fake.calls) == 1


# --- config validation (task 8.2) ----------------------------------------


def test_settings_expose_configured_guardrail_id_and_version():
    assert settings.bedrock_guardrail_id
    assert settings.bedrock_guardrail_version


def test_settings_start_when_pii_resources_are_unset(monkeypatch):
    import os

    from app.config import Settings

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("BEDROCK_GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION", "PII_KMS_KEY_ID")
    }
    monkeypatch.setattr(os, "environ", env)

    settings = Settings(_env_file=None)

    assert settings.bedrock_guardrail_id == ""
    assert settings.bedrock_guardrail_version == ""
    assert settings.pii_kms_key_id == ""


def test_check_guardrail_skips_bedrock_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        guardrails,
        "get_settings",
        lambda: type("S", (), {"bedrock_guardrail_id": ""})(),
    )

    result = guardrails.check_guardrail("Contact John Smith at john@example.com")

    assert result.filtered_text == "Contact John Smith at john@example.com"
    assert result.pii_detected is False
    assert result.occurrences == ()


def test_missing_guardrail_id_fails_startup(monkeypatch):
    import os

    from app.config import Settings

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("BEDROCK_GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION")
    }
    env["BEDROCK_GUARDRAIL_VERSION"] = "1"
    monkeypatch.setattr(os, "environ", env)

    with pytest.raises(Exception):
        Settings(_env_file=None)


# --- property-based tests -------------------------------------------------

_entity_samples = {
    "NAME": "John Smith",
    "EMAIL": "jane.doe@example.com",
    "PHONE": "555-123-4567",
    "ADDRESS": "123 Main Street",
    "SSN": "123-45-6789",
    "CREDIT_CARD": "4111-1111-1111-1111",
    "BANK_ACCOUNT": "123456789012",
}

_clean_text = st.text(
    alphabet=st.characters(whitelist_categories=["L", "N"], max_codepoint=0x2764),
    min_size=0,
    max_size=40,
).filter(lambda s: not any(sample in s for sample in _entity_samples.values()))


@given(prefix=_clean_text, suffix=_clean_text)
def test_property_masking_contains_no_raw_pii_when_present(prefix, suffix):
    """P40: for any text containing a known PII sample, the filtered output
    contains no raw occurrence of it, and pii_detected is true.

    **Validates: Requirements 16.4, 16.5**
    """
    fake = _FakeGuardrailClient()
    text = f"{prefix} John Smith can be reached at jane.doe@example.com {suffix}"

    import app.guardrails as guardrails_module

    original_client = guardrails_module.client
    guardrails_module.client = lambda: fake
    try:
        result = guardrails_module.check_guardrail(text)
    finally:
        guardrails_module.client = original_client

    assert result.pii_detected is True
    assert "John Smith" not in result.filtered_text
    assert "jane.doe@example.com" not in result.filtered_text


@given(text=_clean_text)
def test_property_no_pii_passes_through_unchanged(text):
    """P40: for text with no PII, filtered output equals the input and
    pii_detected is false. Never raises either way.

    **Validates: Requirements 16.4, 16.5**
    """
    fake = _FakeGuardrailClient()

    import app.guardrails as guardrails_module

    original_client = guardrails_module.client
    guardrails_module.client = lambda: fake
    try:
        result = guardrails_module.check_guardrail(text)
    finally:
        guardrails_module.client = original_client

    assert result.filtered_text == text
    assert result.pii_detected is False


@given(entity_type=st.sampled_from(sorted(_entity_samples)), prefix=_clean_text, suffix=_clean_text)
def test_property_entity_type_coverage(entity_type, prefix, suffix):
    """P41: for a synthetic instance of each configured entity type
    embedded in surrounding text, the filtered output does not contain
    that raw instance.

    **Validates: Requirements 16.6**
    """
    sample = _entity_samples[entity_type]
    text = f"{prefix} {sample} {suffix}"
    fake = _FakeGuardrailClient()

    import app.guardrails as guardrails_module

    original_client = guardrails_module.client
    guardrails_module.client = lambda: fake
    try:
        result = guardrails_module.check_guardrail(text)
    finally:
        guardrails_module.client = original_client

    assert sample not in result.filtered_text
    assert result.pii_detected is True


# --- occurrences / MaskedOccurrence (task 8c.3) ----------------------------


def test_check_guardrail_populates_occurrences_for_detected_pii(monkeypatch):
    fake = _FakeGuardrailClient()
    monkeypatch.setattr(guardrails, "client", lambda: fake)

    result = guardrails.check_guardrail("Contact John Smith for details.")

    assert result.pii_detected is True
    assert len(result.occurrences) == 1
    assert result.occurrences[0] == guardrails.MaskedOccurrence(placeholder="{NAME}", original="John Smith")


def test_check_guardrail_occurrences_empty_when_no_pii(monkeypatch):
    fake = _FakeGuardrailClient()
    monkeypatch.setattr(guardrails, "client", lambda: fake)

    result = guardrails.check_guardrail("no sensitive data here")

    assert result.pii_detected is False
    assert result.occurrences == ()


def test_check_guardrail_occurrences_order_matches_placeholder_order(monkeypatch):
    """Two distinct emails masked to {EMAIL} -- occurrences must preserve
    the order they appeared in, so a caller can pair the n-th placeholder
    with the n-th occurrence."""

    class _TwoEmailsClient(_FakeGuardrailClient):
        def apply_guardrail(self, **kwargs):
            text = kwargs["content"][0]["text"]["text"]
            filtered = text.replace("first@example.com", "{EMAIL}").replace("second@example.com", "{EMAIL}")
            return {
                "action": "GUARDRAIL_INTERVENED",
                "outputs": [{"text": filtered}],
                "assessments": [
                    {
                        "sensitiveInformationPolicy": {
                            "piiEntities": [
                                {"type": "EMAIL", "match": "first@example.com", "detected": True},
                                {"type": "EMAIL", "match": "second@example.com", "detected": True},
                            ]
                        }
                    }
                ],
            }

    fake = _TwoEmailsClient()
    monkeypatch.setattr(guardrails, "client", lambda: fake)

    result = guardrails.check_guardrail("Reach first@example.com or second@example.com")

    assert result.filtered_text == "Reach {EMAIL} or {EMAIL}"
    assert [o.original for o in result.occurrences] == ["first@example.com", "second@example.com"]
    assert all(o.placeholder == "{EMAIL}" for o in result.occurrences)


def test_real_guardrail_response_shape_produces_correct_occurrences(monkeypatch):
    """Pinned against the actual apply_guardrail response captured from
    the real AWS API (verified live against guardrail a5vd1uzw7ani), so a
    future change to the real response shape would be caught here even
    without live AWS access."""

    class _RealShapeClient:
        def apply_guardrail(self, **kwargs):
            return {
                "action": "GUARDRAIL_INTERVENED",
                "actionReason": "Guardrail blocked.",
                "outputs": [{"text": "Contact {NAME} at {EMAIL} or {PHONE}"}],
                "assessments": [
                    {
                        "sensitiveInformationPolicy": {
                            "piiEntities": [
                                {"match": "John Smith", "type": "NAME", "action": "ANONYMIZED"},
                                {"match": "john.smith@example.com", "type": "EMAIL", "action": "ANONYMIZED"},
                                {"match": "555-123-4567", "type": "PHONE", "action": "ANONYMIZED"},
                            ],
                            "regexes": [],
                        }
                    }
                ],
            }

    monkeypatch.setattr(guardrails, "client", lambda: _RealShapeClient())

    result = guardrails.check_guardrail("Contact John Smith at john.smith@example.com or 555-123-4567")

    assert result.filtered_text == "Contact {NAME} at {EMAIL} or {PHONE}"
    assert result.occurrences == (
        guardrails.MaskedOccurrence(placeholder="{NAME}", original="John Smith"),
        guardrails.MaskedOccurrence(placeholder="{EMAIL}", original="john.smith@example.com"),
        guardrails.MaskedOccurrence(placeholder="{PHONE}", original="555-123-4567"),
    )


# --- property-based test: P45 (task 8c.4) ----------------------------------


@given(entity_type=st.sampled_from(sorted(_entity_samples)), prefix=_clean_text, suffix=_clean_text)
def test_property_mapping_construction_from_real_text(entity_type, prefix, suffix):
    """P45: for any real (pre-mask) text containing synthetic PII,
    recovering the mapping produces one MaskedOccurrence per instance
    pairing the correct placeholder to the correct real value; for text
    with no PII, the mapping is empty.

    **Validates: Requirements 16.12**
    """
    sample = _entity_samples[entity_type]
    text = f"{prefix} {sample} {suffix}"
    fake = _FakeGuardrailClient()

    import app.guardrails as guardrails_module

    original_client = guardrails_module.client
    guardrails_module.client = lambda: fake
    try:
        result = guardrails_module.check_guardrail(text)
    finally:
        guardrails_module.client = original_client

    assert len(result.occurrences) == 1
    assert result.occurrences[0].original == sample
    assert result.occurrences[0].placeholder in result.filtered_text


@given(text=_clean_text)
def test_property_mapping_empty_when_no_pii(text):
    """P45 (second half): for text with no PII, the mapping is empty.

    **Validates: Requirements 16.12**
    """
    fake = _FakeGuardrailClient()

    import app.guardrails as guardrails_module

    original_client = guardrails_module.client
    guardrails_module.client = lambda: fake
    try:
        result = guardrails_module.check_guardrail(text)
    finally:
        guardrails_module.client = original_client

    assert result.occurrences == ()
