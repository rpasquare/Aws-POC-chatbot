"""Web entry point.

Handles sessions, permissions, upload authorisation, search, answer
generation and citation lookup. Never opens a PDF and never calls OCR.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import db, s3
from app.authz import NotAMemberError, require_role
from app.config import get_settings
from app.uploads import (
    UploadRejectedError,
    authorise_upload,
    complete_upload,
    get_document_status,
    list_documents,
)

log = logging.getLogger("app.api")
NOT_FOUND = "not found"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # fails fast when configuration is incomplete
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.open_pool()
    log.info("api started region=%s bucket=%s", settings.aws_region, settings.s3_bucket)
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="Deal Workspace", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness: dependencies this process cannot serve without."""
    database = db.healthy()
    status = "ok" if database else "degraded"
    return JSONResponse(
        status_code=200 if database else 503,
        content={"status": status, "database": database},
    )


class UploadRequest(BaseModel):
    filename: str
    content_type: str
    byte_size: int


class UploadResponse(BaseModel):
    document_id: str
    upload_url: str


@app.post("/api/projects/{project_id}/documents")
def create_upload(
    project_id: str,
    body: UploadRequest,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> UploadResponse:
    """Authorise an upload slot for one document.

    `X-User-Id` stands in for a resolved session until Sprint 4 wires real
    authentication (task 8). Every other check here — role, content type,
    size — is real.
    """
    with db.connection() as conn:
        try:
            slot = authorise_upload(
                conn,
                user_id=x_user_id,
                project_id=project_id,
                filename=body.filename,
                content_type=body.content_type,
                byte_size=body.byte_size,
            )
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
        except UploadRejectedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return UploadResponse(document_id=slot.document_id, upload_url=slot.upload_url)


class CompletionResponse(BaseModel):
    document_id: str
    duplicate: bool


@app.post("/api/documents/{document_id}/complete")
def complete_document_upload(document_id: str) -> CompletionResponse:
    """Finalise an upload: hash, dedupe, and atomically enqueue parsing.

    Requirements 5.6, 5.8.
    """
    with db.connection() as conn:
        try:
            result = complete_upload(conn, document_id=document_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except UploadRejectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CompletionResponse(document_id=result.document_id, duplicate=result.duplicate)


class DocumentSummary(BaseModel):
    id: str
    filename: str
    status: str
    page_count: int | None
    failure_reason: str | None
    created_at: str
    ready_at: str | None

    @classmethod
    def from_row(cls, row: dict) -> "DocumentSummary":
        return cls(
            id=str(row["id"]),
            filename=row["filename"],
            status=row["status"],
            page_count=row["page_count"],
            failure_reason=row["failure_reason"],
            created_at=row["created_at"].isoformat(),
            ready_at=row["ready_at"].isoformat() if row["ready_at"] else None,
        )


@app.get("/api/projects/{project_id}/documents")
def get_project_documents(
    project_id: str,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> list[DocumentSummary]:
    """List documents visible to the caller's project. Requirement 5.7."""
    with db.connection() as conn:
        try:
            require_role(conn, x_user_id, project_id, "admin", "editor", "viewer")
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
        rows = list_documents(conn, project_id=project_id)
    return [DocumentSummary.from_row(row) for row in rows]


@app.get("/api/documents/{document_id}")
def get_document(document_id: str) -> DocumentSummary:
    """Status, page count and failure reason for polling. Requirement 6.2."""
    with db.connection() as conn:
        row = get_document_status(conn, document_id=document_id)
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return DocumentSummary.from_row(row)


@app.get("/api/documents/{document_id}/extracted-text")
def get_extracted_text(document_id: str) -> dict:
    """Return the parsed blocks for display. Demo/debug endpoint: shows what
    Docling (or Textract, for scanned pages) actually extracted.
    """
    with db.connection() as conn:
        row = conn.execute(
            "select project_id, status from documents where id = %s", (document_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    if row["status"] in ("awaiting_upload", "pending_parse", "parsing"):
        raise HTTPException(status_code=409, detail="parsing not complete yet")

    key = s3.parsed_key(str(row["project_id"]), document_id)
    try:
        payload = s3.get_bytes(key)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean 404 to the client
        raise HTTPException(status_code=404, detail="parsed output not found") from exc
    return json.loads(payload)
