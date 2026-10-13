"""Bedrock embedding client (Amazon Titan Embeddings v2).

Titan v2's synchronous `InvokeModel` API embeds exactly one text per call —
there is no multi-text-per-call batch option on this API. `embed_texts`
hides that from callers: it takes a list of chunk texts and returns a list
of results in the same order, one `InvokeModel` call per text underneath.

Each result carries the model identifier that produced it (`embed_model`),
not just the vector. This is what Requirement 7.7 and design property P19
("embedding models agree") are protecting: the `chunks` table's
`embed_model` column must record exactly what produced each embedding, so a
future model swap is detectable rather than silently producing nonsense.

Throttling (`ThrottlingException`, surfaced as a `botocore.exceptions.
ClientError`) is retried here with exponential backoff. Every other error
propagates immediately rather than being swallowed, so the job-level
retry/backoff already built in `app/jobs.py` handles it instead of two
retry layers silently stacking.
"""

from __future__ import annotations

import json
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


class EmbeddingDimensionMismatch(RuntimeError):
    """Titan returned a vector whose length does not match configuration.

    A defensive check: this catches a misconfiguration between `embed_model`
    and `embed_dimensions` immediately, rather than letting a dimension-
    mismatched vector reach the `chunks` table's `vector(512)` column, where
    it would fail at insert time with a less clear error.
    """


@dataclass(frozen=True)
class EmbeddingResult:
    """One chunk text's embedding, paired with the model that produced it."""

    vector: tuple[float, ...]
    embed_model: str


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


def _invoke_with_throttle_retry(body: dict) -> dict:
    """Call `InvokeModel` once, retrying only `ThrottlingException` with
    exponential backoff. Any other error propagates immediately.
    """
    settings = get_settings()
    attempt = 0
    while True:
        try:
            response = client().invoke_model(
                modelId=settings.embed_model,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            return json.loads(response["body"].read())
        except ClientError as exc:
            if not _is_throttling_error(exc) or attempt >= _THROTTLE_MAX_ATTEMPTS - 1:
                raise
            time.sleep(_THROTTLE_BASE_DELAY_SECONDS * (2**attempt))
            attempt += 1


def embed_text(text: str) -> EmbeddingResult:
    """Embed exactly one text via Titan's `InvokeModel`.

    Raises `EmbeddingDimensionMismatch` if the returned vector's length does
    not match `settings.embed_dimensions`.
    """
    settings = get_settings()
    body = _invoke_with_throttle_retry(
        {
            "inputText": text,
            "dimensions": settings.embed_dimensions,
            "normalize": True,
        }
    )
    vector = tuple(body["embedding"])
    if len(vector) != settings.embed_dimensions:
        raise EmbeddingDimensionMismatch(
            f"Titan returned a {len(vector)}-dimension vector, "
            f"but embed_dimensions is configured as {settings.embed_dimensions}"
        )
    return EmbeddingResult(vector=vector, embed_model=settings.embed_model)


def embed_texts(texts: list[str]) -> list[EmbeddingResult]:
    """Embed a list of chunk texts, in order.

    Structured as a single batch-shaped entry point so callers (the index
    handler, task 4.4) do not need to know that Titan v2 has no multi-text
    `InvokeModel` request — underneath, this is one call per text.
    """
    return [embed_text(text) for text in texts]


__all__ = ["EmbeddingDimensionMismatch", "EmbeddingResult", "client", "embed_text", "embed_texts"]
