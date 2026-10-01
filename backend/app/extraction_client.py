"""Client for the standalone PDF extraction service (Pdf-To-Knowledge).

The parse stage no longer runs Docling in-process; it submits the already
-uploaded original (by presigned URL) to the extraction service and polls
for the result, per task 8.2 of the extraction service's cutover plan.
Page image rendering stays local (see app.rendering) to avoid a second PDF
conversion.
"""

from __future__ import annotations

import time

import httpx

from app.config import get_settings


class ExtractionFailed(RuntimeError):
    """The extraction service reported the job as failed."""


class ExtractionTimedOut(RuntimeError):
    """The extraction service did not finish within the configured budget."""


def submit(source_url: str, *, output_url: str | None = None) -> str:
    """POST /extract. Returns the job id."""
    settings = get_settings()
    response = httpx.post(
        f"{settings.extraction_service_url}/extract",
        json={"source_url": source_url, "output_url": output_url},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["job_id"]


def poll_until_done(job_id: str) -> dict:
    """GET /extract/{job_id} until it reaches a terminal state.

    Returns the result payload (`{"pages": [...], "blocks": [...]}`) on
    success. Raises `ExtractionFailed` on a terminal failure and
    `ExtractionTimedOut` if the configured budget elapses first; both are
    caught by the job worker's normal retry/backoff path.
    """
    settings = get_settings()
    deadline = time.monotonic() + settings.extraction_poll_timeout_seconds
    url = f"{settings.extraction_service_url}/extract/{job_id}"

    while True:
        response = httpx.get(url, timeout=30.0)
        response.raise_for_status()
        body = response.json()

        if body["status"] == "done":
            return {"pages": body.get("pages") or [], "blocks": body.get("blocks") or []}
        if body["status"] == "failed":
            raise ExtractionFailed(body.get("reason") or "extraction service reported failure")

        if time.monotonic() >= deadline:
            raise ExtractionTimedOut(f"extraction job {job_id} did not complete in time")
        time.sleep(settings.extraction_poll_seconds)


__all__ = ["ExtractionFailed", "ExtractionTimedOut", "submit", "poll_until_done"]
