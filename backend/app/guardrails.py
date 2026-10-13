"""Bedrock guardrail client — the PII check (Requirement 16).

`check_guardrail` is a standalone check against text, independent of any
specific model invocation (Requirement 16.1). It wraps Bedrock's
`apply_guardrail` API, called with `source="OUTPUT"` since the guardrail
is filtering text that is about to be sent onward to a model, not text
just received from one.

There is exactly one call site shape: pass text in, get back the filtered
text and whether anything was masked. Masking is Bedrock's job — the
guardrail resource (created out-of-band, see Requirement 16.9/16.10 and
`app/config.py`) is configured with an ANONYMIZE action per PII entity
type, and `apply_guardrail`'s response already contains the anonymised
text. No regular-expression redaction is implemented here.

`check_guardrail` never raises for a detection — masking and continuing is
the chosen behaviour (Requirement 16.4/16.5), not blocking. It raises only
when the guardrail itself is unreachable or misconfigured, after the same
bounded exponential-backoff retry on throttling already used in
`app/embeddings.py` and `app/answer_model.py` (Requirement 16.11): a
swallowed guardrail failure would silently send unchecked text onward,
which is exactly what this requirement exists to prevent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings

_THROTTLE_MAX_ATTEMPTS = 5
_THROTTLE_BASE_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class MaskedOccurrence:
    """One PII entity the guardrail masked: the placeholder token it was
    replaced with in `filtered_text`, and the real value it stood for.

    Order matters: `GuardrailResult.occurrences` is built in the same
    order Bedrock's `piiEntities` list comes back in, which in practice
    matches the left-to-right order placeholders appear in `filtered_text`
    (confirmed against the real API: masking three entities in one call
    returns `piiEntities` in the same order the placeholders appear in
    `outputs[0].text`). This ordering is what later lets a caller pair the
    n-th `{EMAIL}` placeholder with the n-th EMAIL occurrence.
    """

    placeholder: str  # e.g. "{EMAIL}", exactly as it appears in filtered_text
    original: str  # the real value Bedrock replaced, e.g. "jane@example.com"


@dataclass(frozen=True)
class GuardrailResult:
    """The outcome of one `check_guardrail` call.

    `filtered_text` equals the input unchanged when nothing was detected.
    `pii_detected` is true exactly when the guardrail intervened.
    `occurrences` is empty when `pii_detected` is False, and otherwise
    holds one `MaskedOccurrence` per entity Bedrock masked, in order
    (Requirement 16.12) -- the basis for reverse-substitution later.
    """

    filtered_text: str
    pii_detected: bool
    occurrences: tuple[MaskedOccurrence, ...] = ()


@lru_cache(maxsize=1)
def client() -> BaseClient:
    settings = get_settings()
    return boto3.client(
        "bedrock-runtime",
        region_name=settings.aws_region,
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


def _is_throttling_error(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code", "") == "ThrottlingException"


def _apply_guardrail_with_throttle_retry(text: str) -> dict:
    """Call `apply_guardrail` once, retrying only `ThrottlingException`
    with exponential backoff. Any other error propagates immediately.
    """
    settings = get_settings()
    attempt = 0
    while True:
        try:
            return client().apply_guardrail(
                guardrailIdentifier=settings.bedrock_guardrail_id,
                guardrailVersion=settings.bedrock_guardrail_version,
                source="OUTPUT",
                content=[{"text": {"text": text}}],
            )
        except ClientError as exc:
            if not _is_throttling_error(exc) or attempt >= _THROTTLE_MAX_ATTEMPTS - 1:
                raise
            time.sleep(_THROTTLE_BASE_DELAY_SECONDS * (2**attempt))
            attempt += 1


def check_guardrail(text: str) -> GuardrailResult:
    """Pass `text` through the configured Bedrock guardrail's PII policy.

    Returns the filtered text (Bedrock's own anonymised text, unchanged if
    nothing was detected) and whether anything was masked. Never raises for
    a detection — only for a configuration or transport failure after
    retries are exhausted (Requirement 16.11).

    When no guardrail id is configured, the text is returned unchanged and
    nothing is reported as masked. Callers then do not store an encrypted
    original. This is the UAT path before the guardrail and KMS key exist.
    """
    if not get_settings().bedrock_guardrail_id:
        return GuardrailResult(filtered_text=text, pii_detected=False)
    response = _apply_guardrail_with_throttle_retry(text)
    pii_detected = response.get("action") == "GUARDRAIL_INTERVENED"
    outputs = response.get("outputs") or []
    filtered_text = outputs[0]["text"] if outputs else text
    occurrences = _extract_occurrences(response) if pii_detected else ()
    return GuardrailResult(filtered_text=filtered_text, pii_detected=pii_detected, occurrences=occurrences)


def _extract_occurrences(response: dict) -> tuple[MaskedOccurrence, ...]:
    """Walk every assessment's `sensitiveInformationPolicy.piiEntities`,
    in order, and build one `MaskedOccurrence` per detected entity.

    Each `piiEntities` item carries `type` (e.g. "NAME") and `match` (the
    real value); the placeholder token Bedrock wrote into `filtered_text`
    for that type is `"{" + type + "}"` -- confirmed against the real API
    (see module docstring on `MaskedOccurrence`). Entries without
    `detected: True` are skipped defensively, though in practice every
    entry returned here is a detection.
    """
    occurrences: list[MaskedOccurrence] = []
    for assessment in response.get("assessments") or []:
        pii_entities = (assessment.get("sensitiveInformationPolicy") or {}).get("piiEntities") or []
        for entity in pii_entities:
            if not entity.get("detected", True):
                continue
            placeholder = "{" + entity["type"] + "}"
            occurrences.append(MaskedOccurrence(placeholder=placeholder, original=entity["match"]))
    return tuple(occurrences)


__all__ = ["GuardrailResult", "MaskedOccurrence", "check_guardrail", "client"]
