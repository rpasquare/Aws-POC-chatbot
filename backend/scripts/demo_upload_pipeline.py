"""Manual end-to-end demo: upload a PDF, run it through the parse pipeline,
and print the extracted text. Not part of the test suite -- this is for
watching the real pipeline work against a live Postgres and S3 bucket.

Usage:
    .venv/bin/python scripts/demo_upload_pipeline.py path/to/file.pdf
"""

from __future__ import annotations

import sys
from pathlib import Path

from app import db, s3
from app.jobs import Job, mark_done
from app.pipeline import run_parse_job
from app.uploads import authorise_upload, complete_upload

HANDLERS = {"parse": run_parse_job}

DEV_USER_ID = "00000000-0000-0000-0000-000000000001"
DEV_PROJECT_ID = "00000000-0000-0000-0000-0000000000a1"


def main(pdf_path: str) -> None:
    path = Path(pdf_path)
    content = path.read_bytes()

    db.open_pool()
    try:
        with db.connection() as conn:
            print(f"1. Authorising upload for {path.name} ({len(content)} bytes)...")
            slot = authorise_upload(
                conn,
                user_id=DEV_USER_ID,
                project_id=DEV_PROJECT_ID,
                filename=path.name,
                content_type="application/pdf",
                byte_size=len(content),
            )
            print(f"   document_id = {slot.document_id}")

        print("2. Uploading bytes directly to S3 (simulating the browser PUT)...")
        row = None
        with db.connection() as conn:
            row = conn.execute(
                "select s3_key_original from documents where id = %s", (slot.document_id,)
            ).fetchone()
        s3.put_bytes(row["s3_key_original"], content, content_type="application/pdf")
        print(f"   uploaded to key {row['s3_key_original']}")

        with db.connection() as conn:
            print("3. Completing the upload (hash, dedupe, enqueue parse job)...")
            result = complete_upload(conn, document_id=slot.document_id)
            print(f"   duplicate={result.duplicate} document_id={result.document_id}")

        if result.duplicate:
            print(
                f"   this file's content already exists as document {result.document_id}; "
                "skipping the pipeline run and jumping to its current state"
            )
        else:
            print("4. Running this document's own parse job (ignoring any other queued work)...")
            with db.connection() as conn:
                job_row = conn.execute(
                    "select id, document_id, stage, attempts, max_attempts "
                    "from ingestion_jobs "
                    "where document_id = %s and stage = 'parse' and status = 'queued' "
                    "for update skip locked",
                    (result.document_id,),
                ).fetchone()
                if job_row is None:
                    print("   no queued parse job found for this document; stopping")
                    return
                conn.execute(
                    "update ingestion_jobs set status = 'running', claimed_at = now(), "
                    "claimed_by = 'demo-script' where id = %s",
                    (job_row["id"],),
                )
                job = Job(
                    id=job_row["id"],
                    document_id=str(job_row["document_id"]),
                    stage=job_row["stage"],
                    attempts=job_row["attempts"],
                    max_attempts=job_row["max_attempts"],
                )
                print(f"   claimed job id={job.id} stage={job.stage}")

            with db.connection() as conn:
                HANDLERS[job.stage](conn, job)
                mark_done(conn, job.id)
            print("   parse complete, document should now be 'indexing'")

        with db.connection() as conn:
            doc = conn.execute(
                "select status, page_count from documents where id = %s", (result.document_id,)
            ).fetchone()
            print(f"5. Document status: {doc['status']}, pages: {doc['page_count']}")

            pages = conn.execute(
                "select page_no, width, height, image_key from document_pages where document_id = %s order by page_no",
                (result.document_id,),
            ).fetchall()
            print(f"6. Page images recorded: {len(pages)}")
            for p in pages:
                print(f"   page {p['page_no']}: {p['width']}x{p['height']} -> {p['image_key']}")

        project_id = DEV_PROJECT_ID
        parsed_key = s3.parsed_key(project_id, result.document_id)
        parsed_bytes = s3.get_bytes(parsed_key)
        print(f"7. parsed.json ({len(parsed_bytes)} bytes) at {parsed_key}:")
        print(parsed_bytes.decode("utf-8")[:2000])
    finally:
        db.close_pool()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1])
