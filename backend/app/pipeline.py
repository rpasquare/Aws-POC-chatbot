"""The parse stage job handler.

Ties together extraction (delegated to the standalone extraction service),
page rendering (app.rendering) and storage: downloads the original PDF for
page image rendering, submits it to the extraction service for text/block
extraction, writes `parsed.json` and page images to S3, persists page
geometry, enqueues the `index` job, and advances the document to
`indexing`. Requirement 6.8.

Task 8.2: this used to call `app.parsing.extract` in-process. Extraction
now happens in the standalone Pdf-To-Knowledge service; this handler
presigns the original for that service to fetch, submits it, and polls for
the result. Page image rendering stays local (app.rendering) to avoid a
second PDF conversion round trip.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app import extraction_client, s3
from app.documents import apply_transition
from app.rendering import persist_page_geometry, render_page_images


def _parsed_json(result: dict) -> bytes:
    return json.dumps({"pages": result["pages"], "blocks": result["blocks"]}).encode("utf-8")


def run_parse_job(conn, job) -> None:
    """Stage handler for `ingestion_jobs.stage = 'parse'`.

    Requirement 6.8: the parse output is retained (`parsed.json`) so
    chunking and embedding can be repeated without reprocessing the
    original file.
    """
    row = conn.execute(
        "select project_id, s3_key_original from documents where id = %s",
        (job.document_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no document with id {job.document_id!r}")
    project_id = str(row["project_id"])

    apply_transition(conn, job.document_id, "parsing")

    source_url = s3.presigned_get(row["s3_key_original"])
    job_id = extraction_client.submit(source_url)
    result = extraction_client.poll_until_done(job_id)

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "original.pdf"
        pdf_path.write_bytes(s3.get_bytes(row["s3_key_original"]))
        page_rows = render_page_images(str(pdf_path), project_id=project_id, document_id=job.document_id)

    persist_page_geometry(conn, job.document_id, page_rows)

    parsed_key = s3.parsed_key(project_id, job.document_id)
    s3.put_bytes(parsed_key, _parsed_json(result), content_type="application/json")

    conn.execute(
        "update documents set page_count = %s where id = %s",
        (len(result["pages"]), job.document_id),
    )
    conn.execute(
        "insert into ingestion_jobs (document_id, stage, status) values (%s, 'index', 'queued')",
        (job.document_id,),
    )
    apply_transition(conn, job.document_id, "indexing")


__all__ = ["run_parse_job"]
