"""Tests for the Bedrock answer client (streaming Claude responses).

Validates: Requirements 7.5
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from app import answer_model
from app.config import get_settings

settings = get_settings()


def _text_delta_event(text: str) -> dict:
    body = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    return {"chunk": {"bytes": json.dumps(body).encode("utf-8")}}


def _structural_event(event_type: str) -> dict:
    return {"chunk": {"bytes": json.dumps({"type": event_type}).encode("utf-8")}}


class _FakeEventStream:
    """Stands in for the `EventStream` boto3 returns as `response["body"]`."""

    def __init__(self, events):
        self._events = list(events)

    def __iter__(self):
        return iter(self._events)


class _FakeBedrockClient:
    """Stands in for the boto3 bedrock-runtime client.

    `responses` is a list of either dicts shaped like
    `{"body": _FakeEventStream(...)}` (success) or exceptions to raise,
    consumed in order across successive
    `invoke_model_with_response_stream` calls.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def invoke_model_with_response_stream(self, *, modelId, body, contentType, accept):
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _throttling_error():
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeModelWithResponseStream",
    )


def _other_client_error():
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad input"}},
        "InvokeModelWithResponseStream",
    )


# --- unit tests ---------------------------------------------------------


def test_stream_answer_yields_text_fragments_in_order(monkeypatch):
    events = [
        _structural_event("message_start"),
        _structural_event("content_block_start"),
        _text_delta_event("The fund's "),
        _text_delta_event("NAV is $42.6m [1]."),
        _structural_event("content_block_stop"),
        _structural_event("message_delta"),
        _structural_event("message_stop"),
    ]
    fake = _FakeBedrockClient([{"body": _FakeEventStream(events)}])
    monkeypatch.setattr(answer_model, "client", lambda: fake)

    fragments = list(answer_model.stream_answer("What is the NAV?"))

    assert fragments == ["The fund's ", "NAV is $42.6m [1]."]
    assert "".join(fragments) == "The fund's NAV is $42.6m [1]."
    assert fake.calls[0]["modelId"] == settings.answer_model
    assert fake.calls[0]["body"]["messages"] == [
        {"role": "user", "content": "What is the NAV?"}
    ]


def test_throttling_on_initial_call_is_retried_and_eventually_succeeds(monkeypatch):
    events = [_text_delta_event("answer")]
    fake = _FakeBedrockClient(
        [_throttling_error(), _throttling_error(), {"body": _FakeEventStream(events)}]
    )
    monkeypatch.setattr(answer_model, "client", lambda: fake)
    sleeps: list[float] = []
    monkeypatch.setattr(answer_model.time, "sleep", lambda seconds: sleeps.append(seconds))

    fragments = list(answer_model.stream_answer("question"))

    assert fragments == ["answer"]
    assert len(fake.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_throttling_on_initial_call_exhausts_retries_and_raises(monkeypatch):
    fake = _FakeBedrockClient([_throttling_error() for _ in range(answer_model._THROTTLE_MAX_ATTEMPTS)])
    monkeypatch.setattr(answer_model, "client", lambda: fake)
    monkeypatch.setattr(answer_model.time, "sleep", lambda seconds: None)

    with pytest.raises(ClientError):
        list(answer_model.stream_answer("question"))

    assert len(fake.calls) == answer_model._THROTTLE_MAX_ATTEMPTS


def test_non_throttling_error_on_initial_call_propagates_without_retry(monkeypatch):
    fake = _FakeBedrockClient([_other_client_error(), {"body": _FakeEventStream([])}])
    monkeypatch.setattr(answer_model, "client", lambda: fake)
    monkeypatch.setattr(answer_model.time, "sleep", lambda seconds: None)

    with pytest.raises(ClientError) as exc_info:
        list(answer_model.stream_answer("question"))

    assert exc_info.value.response["Error"]["Code"] == "ValidationException"
    assert len(fake.calls) == 1


def test_mid_stream_failure_raises_streaming_answer_error_without_retry(monkeypatch):
    class _FailingEventStream:
        def __iter__(self):
            def generator():
                yield _text_delta_event("partial ")
                raise _throttling_error()

            return generator()

    fake = _FakeBedrockClient([{"body": _FailingEventStream()}])
    monkeypatch.setattr(answer_model, "client", lambda: fake)
    monkeypatch.setattr(answer_model.time, "sleep", lambda seconds: None)

    collected = []
    with pytest.raises(answer_model.StreamingAnswerError):
        for fragment in answer_model.stream_answer("question"):
            collected.append(fragment)

    # The fragment yielded before the failure was already delivered; the
    # call that opened the stream is not retried a second time.
    assert collected == ["partial "]
    assert len(fake.calls) == 1


def test_malformed_event_raises_streaming_answer_error(monkeypatch):
    malformed_event = {"chunk": {"bytes": b"not valid json"}}
    fake = _FakeBedrockClient([{"body": _FakeEventStream([malformed_event])}])
    monkeypatch.setattr(answer_model, "client", lambda: fake)

    with pytest.raises(answer_model.StreamingAnswerError):
        list(answer_model.stream_answer("question"))


def test_non_text_events_are_skipped(monkeypatch):
    events = [
        _structural_event("message_start"),
        {"chunk": {"bytes": json.dumps({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{}"}}).encode("utf-8")}},
        _text_delta_event("only this"),
        _structural_event("message_stop"),
    ]
    fake = _FakeBedrockClient([{"body": _FakeEventStream(events)}])
    monkeypatch.setattr(answer_model, "client", lambda: fake)

    fragments = list(answer_model.stream_answer("question"))

    assert fragments == ["only this"]


def test_events_without_chunk_key_are_skipped(monkeypatch):
    events = [{"internalServerException": {"message": "ignored for this test"}}, _text_delta_event("ok")]
    fake = _FakeBedrockClient([{"body": _FakeEventStream(events)}])
    monkeypatch.setattr(answer_model, "client", lambda: fake)

    fragments = list(answer_model.stream_answer("question"))

    assert fragments == ["ok"]
