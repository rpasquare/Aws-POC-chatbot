"""Upload authorisation.

`POST /api/projects/{id}/documents` — Requirement 5.1, 5.2, 5.4, 5.5.

Verifies the caller holds `admin` on the project, validates the declared
content type and size, inserts the document row as `awaiting_upload`, and
returns a presigned PUT URL scoped to exactly one S3 key with a short expiry.
The application server never receives the file bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from psycopg import Connection

from app import s3
from app.authz import NotAMemberError, require_role
from app.config import get_settings
from app.documents import validate_transition


class UploadRejectedError(ValueError):
    """Declared content type or size is not acceptable."""


@dataclass(frozen=True)
class UploadSlot:
    document_id: str
    upload_url: str


@dataclass(frozen=True)
class CompletionResult:
    document_id: str
    duplicate: bool


def validate_upload(content_type: str, byte_size: int) -> None:
    """Requirement 5.4 (format) and 5.5 (size). Raises with a message that
    states the accepted formats / the limit, for the handler to surface.
    """
    settings = get_settings()
    if content_type not in settings.allowed_content_types:
        raise UploadRejectedError(
            f"unsupported content type {content_type!r}; "
            f"accepted formats: {', '.join(settings.allowed_content_types)}"
        )
    if byte_size <= 0 or byte_size > settings.max_upload_bytes:
        raise UploadRejectedError(
            f"file size {byte_size} exceeds the maximum of {settings.max_upload_bytes} bytes"
        )


def authorise_upload(
    conn: Connection,
    *,
    user_id: str,
    project_id: str,
    filename: str,
    content_type: str,
    byte_size: int,
) -> UploadSlot:
    """Verify role, validate the declared file, and issue an upload slot.

    Requirement 5.1: role is verified before the upload begins.
    """
    require_role(conn, user_id, project_id, "admin")
    validate_upload(content_type, byte_size)

    row = conn.execute(
        "insert into documents (project_id, filename, status, s3_key_original, uploaded_by) "
        "values (%s, %s, 'awaiting_upload', %s, %s) "
        "returning id",
        (project_id, filename, "", user_id),
    ).fetchone()
    document_id = str(row["id"])

    key = s3.original_key(project_id, document_id)
    conn.execute(
        "update documents set s3_key_original = %s where id = %s",
        (key, document_id),
    )

    upload_url = s3.presigned_put(key, content_type)
    return UploadSlot(document_id=document_id, upload_url=upload_url)


__all__ = [
    "UploadRejectedError",
    "UploadSlot",
    "CompletionResult",
    "authorise_upload",
    "complete_upload",
    "list_documents",
    "get_document_status",
    "validate_upload",
    "NotAMemberError",
]


def complete_upload(conn: Connection, *, document_id: str) -> "CompletionResult":
    """Finalise an upload: hash the object, dedupe, and atomically enqueue parsing.

    Requirement 5.6: the document record and its processing job are created
    as a single atomic operation, so neither can exist without the other —
    achieved by running both statements in one transaction.

    Requirement 5.8: a file identical in content to an existing document in
    the same project is detected and not processed a second time. If the
    hash matches an existing non-deleted document, that document is
    returned unchanged and no job is created for the new row; the new row
    is left as `awaiting_upload` and is never listed (Requirement 5.7).
    """
    doc = conn.execute(
        "select project_id, s3_key_original, status from documents where id = %s",
        (document_id,),
    ).fetchone()
    if doc is None:
        raise ValueError(f"no document with id {document_id!r}")
    if doc["status"] != "awaiting_upload":
        raise UploadRejectedError(f"document {document_id!r} is not awaiting upload")

    project_id = str(doc["project_id"])
    key = doc["s3_key_original"]

    head = s3.head(key)
    byte_size = int(head["ContentLength"])
    body = s3.get_bytes(key)
    content_hash = hashlib.sha256(body).hexdigest()

    existing = conn.execute(
        "select id from documents "
        "where project_id = %s and content_hash = %s and deleted_at is null and id != %s",
        (project_id, content_hash, document_id),
    ).fetchone()
    if existing is not None:
        return CompletionResult(document_id=str(existing["id"]), duplicate=True)

    validate_transition("awaiting_upload", "pending_parse")
    conn.execute(
        "update documents set content_hash = %s, byte_size = %s, status = 'pending_parse' "
        "where id = %s",
        (content_hash, byte_size, document_id),
    )
    conn.execute(
        "insert into ingestion_jobs (document_id, stage, status) values (%s, 'parse', 'queued')",
        (document_id,),
    )
    return CompletionResult(document_id=document_id, duplicate=False)


def list_documents(conn: Connection, *, project_id: str) -> list[dict]:
    """Documents visible to users: excludes `awaiting_upload` and deleted rows.

    Requirement 5.7: a document that was authorised but never completed is
    left in a non-visible pending state. Requirement 6.2: status is exposed
    for polling.
    """
    rows = conn.execute(
        "select id, filename, status, page_count, failure_reason, created_at, ready_at "
        "from documents "
        "where project_id = %s and status != 'awaiting_upload' and deleted_at is null "
        "order by created_at desc",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_document_status(conn: Connection, *, document_id: str) -> dict | None:
    """Status, page count and failure reason for polling. Requirement 6.2, 6.10."""
    row = conn.execute(
        "select id, filename, status, page_count, failure_reason, created_at, ready_at "
        "from documents where id = %s and status != 'awaiting_upload' and deleted_at is null",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None
