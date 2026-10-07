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

from botocore.exceptions import ClientError
from fastapi import Cookie, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import db, s3
from app.auth import InvalidCredentialsError, TooManyAttemptsError, sign_in, sign_out
from app.authz import NotAMemberError, require_role, role_on_project
from app.config import get_settings
from app.csrf import CSRFMiddleware, generate_csrf_token, set_csrf_cookie
from app.invite_acceptance import InviteNotValidError, PasswordRequiredError, accept_invite
from app.invites import InviteRejectedError, cancel_invite, list_pending_invites, send_invite
from app.session_auth import SESSION_COOKIE_NAME, ResolvedCallerId
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

# Requirement 15.6: double-submit CSRF check for state-changing requests
# that rely on the session cookie. Added before CORS so CORS ends up
# outermost (Starlette runs middleware in reverse of add order) and its
# headers are still applied to the CSRF middleware's own 403 responses.
app.add_middleware(CSRFMiddleware)

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
    caller_id: str = ResolvedCallerId,
) -> UploadResponse:
    """Authorise an upload slot for one document.

    Requirement 7.1/7.2/7.3/7.4 (project-invites spec): the caller is
    resolved via a real session when one is present, so an invited editor
    or viewer is genuinely denied here rather than silently acting as the
    `X-User-Id` placeholder's admin identity. Absent a session, behaviour
    is unchanged (`X-User-Id` still stands in for a resolved session, per
    the original Sprint 4 note). Every other check here — role, content
    type, size — is real either way.
    """
    with db.connection() as conn:
        try:
            slot = authorise_upload(
                conn,
                user_id=caller_id,
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


class InviteRequest(BaseModel):
    email: str
    role: str


class InviteResponse(BaseModel):
    invite_id: str


@app.post("/api/projects/{project_id}/invites")
def create_invite(
    project_id: str,
    body: InviteRequest,
    caller_id: str = ResolvedCallerId,
) -> InviteResponse:
    """Send a project invite. Admin-only. Requirements 1.1, 1.2, 1.4, 1.5
    (project-invites spec). The email itself is sent by the caller of
    `send_invite` separately (task 5.1 wires this once app.email exists);
    this handler only creates the invite record and returns its id.

    Requirement 7.1: resolved via a real session when one is present, so
    the invite's `invited_by` reflects whoever actually sent it rather
    than always the `X-User-Id` placeholder's account.
    """
    with db.connection() as conn:
        try:
            slot = send_invite(
                conn,
                user_id=caller_id,
                project_id=project_id,
                email=body.email,
                role=body.role,
            )
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
        except InviteRejectedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return InviteResponse(invite_id=slot.invite_id)


class PendingInvite(BaseModel):
    id: str
    email: str
    role: str
    invited_by_email: str
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> "PendingInvite":
        return cls(
            id=str(row["id"]),
            email=row["email"],
            role=row["role"],
            invited_by_email=row["invited_by_email"],
            created_at=row["created_at"].isoformat(),
        )


@app.get("/api/projects/{project_id}/invites")
def get_pending_invites(
    project_id: str,
    caller_id: str = ResolvedCallerId,
) -> list[PendingInvite]:
    """List pending invites for a project. Admin-only. Requirement 4.2.

    Requirement 7.1: resolved via a real session when one is present.
    """
    with db.connection() as conn:
        try:
            rows = list_pending_invites(conn, user_id=caller_id, project_id=project_id)
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    return [PendingInvite.from_row(row) for row in rows]


@app.post("/api/projects/{project_id}/invites/{invite_id}/cancel")
def cancel_project_invite(
    project_id: str,
    invite_id: str,
    caller_id: str = ResolvedCallerId,
) -> dict[str, str]:
    """Cancel a pending invite. Admin-only. Requirement 4.3.

    Requirement 7.1: resolved via a real session when one is present.
    """
    with db.connection() as conn:
        try:
            cancel_invite(conn, user_id=caller_id, project_id=project_id, invite_id=invite_id)
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    return {"status": "cancelled"}


class AcceptInviteRequest(BaseModel):
    token: str
    password: str | None = None


class AcceptInviteResponse(BaseModel):
    project_id: str
    first_time: bool


@app.post("/api/invites/accept")
def accept_project_invite(body: AcceptInviteRequest, response: Response) -> AcceptInviteResponse:
    """Accept an invite by token. No prior authentication required — the
    token itself is the credential. Requirements 2.1-2.4, 3.1, 3.3, 4.4.
    """
    with db.connection() as conn:
        try:
            result = accept_invite(conn, raw_token=body.token, password=body.password)
        except InviteNotValidError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except PasswordRequiredError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=result.session_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_expiry_seconds,
    )
    set_csrf_cookie(response, generate_csrf_token())
    return AcceptInviteResponse(project_id=result.project_id, first_time=result.first_time)


class SignInRequest(BaseModel):
    email: str
    password: str


class SignInResponse(BaseModel):
    user_id: str


@app.post("/api/auth/session")
def sign_in_endpoint(body: SignInRequest, response: Response) -> SignInResponse:
    """Sign in with email and password. Requirements 1.1, 1.2, 1.3, 1.6.

    Requirement 1.3/15.5: unknown email and wrong password are
    indistinguishable to the caller -- same status, same message, and
    `sign_in` takes comparable time either way.
    Requirement 1.4/15.4: an email with too many recent failed attempts
    is rejected with 429 instead, independent of whether that email has
    an account at all.
    """
    with db.connection() as conn:
        try:
            result = sign_in(conn, email=body.email, password=body.password)
        except TooManyAttemptsError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=result.session_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_expiry_seconds,
    )
    set_csrf_cookie(response, generate_csrf_token())
    return SignInResponse(user_id=result.user_id)


@app.delete("/api/auth/session")
def sign_out_endpoint(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict[str, str]:
    """Sign out: delete the session row server-side, then clear the
    cookie. Requirement 1.5 -- a signed-out session cannot be replayed by
    re-sending the old cookie value, because the row backing it is gone,
    not just hidden from the browser.
    """
    if session_token is not None:
        with db.connection() as conn:
            sign_out(conn, raw_token=session_token)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    response.delete_cookie(key=get_settings().csrf_cookie_name)
    return {"status": "signed_out"}


class CallerRole(BaseModel):
    role: str | None
    email: str | None


@app.get("/api/projects/{project_id}/me")
def get_my_role(project_id: str, caller_id: str = ResolvedCallerId) -> CallerRole:
    """The resolved caller's role on a project, and the caller's own email,
    for the UI header/role chip. Requirement 7.5 (project-invites spec).
    Resolved the same way as the upload endpoint: a real session takes
    precedence over the `X-User-Id` placeholder when present, so the
    email shown is whoever actually signed in, not the hardcoded
    placeholder identity.
    """
    with db.connection() as conn:
        role = role_on_project(conn, caller_id, project_id)
        user = conn.execute("select email from users where id = %s", (caller_id,)).fetchone()
    return CallerRole(role=role, email=user["email"] if user else None)


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
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404"):
            # The object genuinely isn't there -- a real, distinct 404.
            raise HTTPException(status_code=404, detail="parsed output not found") from exc
        # Any other S3 error (expired/invalid credentials, access denied,
        # wrong bucket, etc.) is not "not found" and should not look like
        # it -- a 503 makes clear this is a server-side problem to fix,
        # not evidence the document is missing.
        log.exception("S3 error reading parsed output key=%s", key)
        raise HTTPException(status_code=503, detail="could not read parsed output; try again shortly") from exc
    return json.loads(payload)
