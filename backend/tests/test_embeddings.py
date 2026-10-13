"""Tests for the Bedrock embedding client.

Validates: Requirements 7.7
"""

from __future__ import annotations

import json
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from app import embeddings
from app.config import get_settings

settings = get_settings()


class _FakeBody:
    def __init__(self, payload: dict):
        self._stream = BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self):
        return self._stream.read()


class _FakeBedrockClient:
    """Stands in for the boto3 bedrock-runtime client.

    `responses` is a list of either dict payloads (success) or exceptions
    to raise, consumed in order across successive `invoke_model` calls.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def invoke_model(self, *, modelId, body, contentType, accept):
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"body": _FakeBody(outcome)}


def _throttling_error():
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeModel",
    )


def _other_client_error():
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad input"}},
        "InvokeModel",
    )


def _embedding_payload(dimensions: int | None = None, token_count: int = 5):
    dimensions = dimensions if dimensions is not None else settings.embed_dimensions
    return {"embedding": [0.1] * dimensions, "inputTextTokenCount": token_count}


# --- unit tests ---------------------------------------------------------


def test_embed_text_returns_vector_and_model(monkeypatch):
    fake = _FakeBedrockClient([_embedding_payload()])
    monkeypatch.setattr(embeddings, "client", lambda: fake)
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)

    result = embeddings.embed_text("net asset value is $42.6m")

    assert len(result.vector) == settings.embed_dimensions
    assert result.embed_model == settings.embed_model
    assert fake.calls[0]["modelId"] == settings.embed_model
    assert fake.calls[0]["body"] == {
        "inputText": "net asset value is $42.6m",
        "dimensions": settings.embed_dimensions,
        "normalize": True,
    }


def test_embed_texts_calls_once_per_text_and_preserves_order(monkeypatch):
    fake = _FakeBedrockClient([_embedding_payload(), _embedding_payload(), _embedding_payload()])
    monkeypatch.setattr(embeddings, "client", lambda: fake)

    results = embeddings.embed_texts(["a", "b", "c"])

    assert len(results) == 3
    assert len(fake.calls) == 3
    assert [call["body"]["inputText"] for call in fake.calls] == ["a", "b", "c"]
    assert all(r.embed_model == settings.embed_model for r in results)


def test_throttling_is_retried_and_eventually_succeeds(monkeypatch):
    fake = _FakeBedrockClient([_throttling_error(), _throttling_error(), _embedding_payload()])
    monkeypatch.setattr(embeddings, "client", lambda: fake)
    sleeps: list[float] = []
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = embeddings.embed_text("retried text")

    assert len(result.vector) == settings.embed_dimensions
    assert len(fake.calls) == 3
    # Exponential backoff: increasing delay between attempts.
    assert sleeps == [1.0, 2.0]


def test_throttling_exhausts_retries_and_raises(monkeypatch):
    fake = _FakeBedrockClient([_throttling_error() for _ in range(embeddings._THROTTLE_MAX_ATTEMPTS)])
    monkeypatch.setattr(embeddings, "client", lambda: fake)
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)

    with pytest.raises(ClientError):
        embeddings.embed_text("never succeeds")

    assert len(fake.calls) == embeddings._THROTTLE_MAX_ATTEMPTS


def test_non_throttling_client_error_propagates_without_retry(monkeypatch):
    fake = _FakeBedrockClient([_other_client_error(), _embedding_payload()])
    monkeypatch.setattr(embeddings, "client", lambda: fake)
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)

    with pytest.raises(ClientError) as exc_info:
        embeddings.embed_text("bad input text")

    assert exc_info.value.response["Error"]["Code"] == "ValidationException"
    # No retry: exactly one call was made before the error propagated.
    assert len(fake.calls) == 1


def test_dimension_mismatch_raises_clear_error(monkeypatch):
    fake = _FakeBedrockClient([_embedding_payload(dimensions=settings.embed_dimensions + 1)])
    monkeypatch.setattr(embeddings, "client", lambda: fake)

    with pytest.raises(embeddings.EmbeddingDimensionMismatch):
        embeddings.embed_text("mismatched dimension text")
