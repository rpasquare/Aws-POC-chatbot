"""Tests for the extraction service client.

Stubs httpx's transport so these run without a live extraction service.
Validates: Requirements (Deal Workspace's own Requirement 6.8, unchanged in shape)
"""

from __future__ import annotations

import httpx
import pytest

from app import extraction_client


def _response(json_body, url="https://example.com/extract"):
    """A bare httpx.Response with a request attached, so
    `raise_for_status()` works outside of a real Client round trip.
    """
    response = httpx.Response(200, json=json_body)
    response._request = httpx.Request("GET", url)
    return response


def test_submit_returns_job_id(monkeypatch):
    def fake_post(url, json, timeout):
        return _response({"job_id": "abc123"}, url)

    monkeypatch.setattr(httpx, "post", fake_post)

    job_id = extraction_client.submit("https://example.com/source.pdf")

    assert job_id == "abc123"


def test_poll_until_done_returns_result_on_completion(monkeypatch):
    responses = iter(
        [
            _response({"status": "processing"}),
            _response({"status": "done", "pages": [{"page_no": 1, "width": 1.0, "height": 2.0}], "blocks": []}),
        ]
    )
    monkeypatch.setattr(httpx, "get", lambda url, timeout: next(responses))
    monkeypatch.setattr(extraction_client.time, "sleep", lambda seconds: None)

    result = extraction_client.poll_until_done("abc123")

    assert result == {"pages": [{"page_no": 1, "width": 1.0, "height": 2.0}], "blocks": []}


def test_poll_until_done_raises_on_failure(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _response({"status": "failed", "reason": "bad pdf"}))

    with pytest.raises(extraction_client.ExtractionFailed, match="bad pdf"):
        extraction_client.poll_until_done("abc123")


class _FakeSettings:
    extraction_service_url = "https://example.com"
    extraction_poll_seconds = 0.0
    extraction_poll_timeout_seconds = 0.0


def test_poll_until_done_times_out(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _response({"status": "processing"}))
    monkeypatch.setattr(extraction_client.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(extraction_client, "get_settings", lambda: _FakeSettings())

    with pytest.raises(extraction_client.ExtractionTimedOut):
        extraction_client.poll_until_done("abc123")
