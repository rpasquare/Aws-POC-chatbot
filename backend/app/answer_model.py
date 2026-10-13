"""Bedrock answer client (Anthropic Claude, via the streaming API).

Requirement 7.5 requires the answer to begin appearing progressively,
rather than only once the whole response is complete. Bedrock's streaming
entry point for this is `invoke_model_with_response_stream`, not
`invoke_model` (the blocking call `app/embeddings.py` uses for Titan) — the
response is an `EventStream` of chunks rather than one JSON body.

ASSUMPTION, documented because it cannot be verified against a live
Bedrock endpoint in this environment: Claude on Bedrock's streaming
response follows Anthropic's Messages API streaming event shape passed
through unchanged. Each event in the stream is a dict with a `chunk` key
whose `bytes` value is a JSON-encoded object carrying a `type` field.
The event types this module relies on:

  - `content_block_delta`: carries incremental text at
    `delta.text` (delta.type == "text_delta"). This is the only event
    type whose text is yielded.
  - `message_stop` / `content_block_stop` / `message_start` /
    `content_block_start` / `message_delta`: structural events with no
    text payload for our purposes, skipped.

Parsing is defensive: any event whose JSON body does not look like a
`content_block_delta` with a text delta is simply skipped rather than
raising, since the exact set of structural event types is the part most
likely to need correcting once this runs against the real API. If that
assumption turns out wrong, this is the one function to fix.

Throttling (`ThrottlingException`, a `botocore.exceptions.ClientError`) is
retried with exponential backoff, but only around the *initial*
`invoke_model_with_response_stream` call — the one that opens the stream
and can be safely retried as a whole. Once streaming has begun, a
mid-stream failure propagates immediately as a retryable error to the
caller rather than being retried here: re-issuing the call after some
tokens have already been yielded would risk emitting duplicate output,
and the caller (the eventual streaming HTTP endpoint, tasks 6.10/6.12) is
in a better position to decide whether to restart the whole answer from
scratch. This mirrors `app/embeddings.py`'s rule that only the retryable
condition is retried here and every other error, including a throttling
error encountered after the retry budget or mid-stream, propagates
immediately so retry logic does not stack across two layers.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from functools import lru_cache

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings

_THROTTLE_MAX_ATTEMPTS = 5
_THROTTLE_BASE_DELAY_SECONDS = 1.0

_ANTHROPIC_VERSION = "bedrock-2023-05-31"
_DEFAULT_MAX_TOKENS = 1024


class StreamingAnswerError(RuntimeError):
    """A mid-stream failure from Bedrock, raised instead of retried.

    Raised when an error (including a throttling `ClientError`) occurs
    after the response stream has already started yielding events. The
    caller may treat this as retryable — restarting the whole answer from
    the beginning of the prompt — but this module will not do that silently,
    since a silent retry here could duplicate output already sent to a
    client mid-stream.
    """


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


def _build_body(prompt: str, max_tokens: int) -> dict:
    return {
        "anthropic_version": _ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }


def _open_stream_with_throttle_retry(prompt: str, max_tokens: int):
    """Call `invoke_model_with_response_stream` once, retrying only a
    `ThrottlingException` raised by *opening* the stream, with exponential
    backoff. Any other error propagates immediately. Once this call
    returns successfully, retrying is no longer this function's job.
    """
    settings = get_settings()
    body = _build_body(prompt, max_tokens)
    attempt = 0
    while True:
        try:
            return client().invoke_model_with_response_stream(
                modelId=settings.answer_model,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
        except ClientError as exc:
            if not _is_throttling_error(exc) or attempt >= _THROTTLE_MAX_ATTEMPTS - 1:
                raise
            time.sleep(_THROTTLE_BASE_DELAY_SECONDS * (2**attempt))
            attempt += 1


def _extract_text_delta(event_body: dict) -> str | None:
    """Return the text fragment carried by one decoded stream event, or
    `None` if this event carries no text (see the module docstring for the
    documented assumption about which event types carry text).
    """
    if event_body.get("type") != "content_block_delta":
        return None
    delta = event_body.get("delta") or {}
    if delta.get("type") != "text_delta":
        return None
    return delta.get("text")


def stream_answer(prompt: str, *, max_tokens: int = _DEFAULT_MAX_TOKENS) -> Iterator[str]:
    """Stream Claude's answer to `prompt`, yielding text fragments in order
    as they arrive.

    Throttling on the initial call is retried with backoff (same policy as
    `app/embeddings.py`). Once the stream has started, any error — a
    `ClientError` (throttling or otherwise) or a malformed event — is
    raised as `StreamingAnswerError` rather than retried, so the caller
    decides whether and how to restart.
    """
    response = _open_stream_with_throttle_retry(prompt, max_tokens)

    try:
        for event in response["body"]:
            chunk = event.get("chunk")
            if chunk is None:
                continue
            try:
                event_body = json.loads(chunk["bytes"])
            except (KeyError, json.JSONDecodeError) as exc:
                raise StreamingAnswerError(
                    "received a malformed event from the Bedrock response stream"
                ) from exc

            text = _extract_text_delta(event_body)
            if text:
                yield text
    except StreamingAnswerError:
        raise
    except ClientError as exc:
        raise StreamingAnswerError(
            "Bedrock response stream failed after streaming had begun"
        ) from exc


__all__ = ["StreamingAnswerError", "client", "stream_answer"]
